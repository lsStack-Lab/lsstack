"""lstopology: NUMA + PCIe link topology from sysfs; optional nvidia-smi topo.

PCIe facts only (no balance judgments). Report applies width/oversub checks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.color import attention_note, color_enabled, verdict_bracket
from lsstack.common import load_json, lookup_gpu_specs, read_file, system_binary
from lsstack.findings import render_verdict

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_NODE = "/sys/devices/system/node"
_CPU = "/sys/devices/system/cpu"
_PCI = "/sys/bus/pci/devices"

# Endpoint roles we care about for lane validity
_ROLE_BY_BASE = {
    0x03: "gpu",       # Display (VGA / 3D); audio function filtered below
    0x02: "nic",       # Network
    0x01: "storage",   # Mass storage (NVMe subclass refined)
}

# GT/s → PCIe generation (approximate; encoding overhead ignored for labeling)
_GT_TO_GEN = (
    (64.0, 6),
    (32.0, 5),
    (16.0, 4),
    (8.0, 3),
    (5.0, 2),
    (2.5, 1),
)


def get_numa_nodes() -> list[dict]:
    """NUMA nodes: id, cpulist, meminfo MemTotal when present."""
    out: list[dict] = []
    if not os.path.isdir(_NODE):
        return out
    for name in sorted(os.listdir(_NODE)):
        if not name.startswith("node") or not name[4:].isdigit():
            continue
        path = os.path.join(_NODE, name)
        cpulist = (read_file(os.path.join(path, "cpulist")) or "").strip()
        mem_kb = None
        meminfo = read_file(os.path.join(path, "meminfo"))
        if meminfo:
            for line in meminfo.splitlines():
                if "MemTotal" in line:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p.isdigit():
                            mem_kb = int(p)
                            break
                    break
        out.append({
            "node": name,
            "cpulist": cpulist,
            "mem_total_kib": mem_kb,
        })
    return out


def get_nvidia_topo() -> str | None:
    """nvidia-smi topo -m when available."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "topo", "-m"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _int_sysfs(path: str) -> int | None:
    raw = read_file(path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _parse_gt_s(speed: str | None) -> float | None:
    if not speed:
        return None
    m = re.search(r"([\d.]+)\s*GT", speed, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _gen_from_gt_s(gt: float | None) -> int | None:
    if gt is None:
        return None
    for threshold, gen in _GT_TO_GEN:
        if gt + 0.05 >= threshold:
            return gen
    return None


def _gen_label(gen: int | None) -> str:
    return f"Gen{gen}" if gen is not None else "Gen?"


def _bus_interface_to_gen(bus_interface: str | None) -> int | None:
    if not bus_interface:
        return None
    m = re.search(r"PCIe\s*(\d+)", str(bus_interface), re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _gpu_spec_bus_interface(vendor_hex: str | None, device_hex: str | None) -> str | None:
    """Lookup bus_interface from gpu_pci_id.json when possible."""
    row = lookup_gpu_specs(load_json("gpu_pci_id.json"), vendor_hex, device_hex)
    if isinstance(row, dict):
        return row.get("bus_interface")
    return None


def _explain_endpoint(ep: dict, root: dict | None) -> dict:
    """Attach gen labels, conflicts, explanation, research_hint for one endpoint."""
    cur_gt = ep.get("current_gt_s")
    max_gt = ep.get("max_gt_s")
    cur_gen = _gen_from_gt_s(cur_gt)
    max_gen = _gen_from_gt_s(max_gt)
    root_max_gt = (root or {}).get("max_gt_s")
    root_cur_gt = (root or {}).get("current_gt_s")
    root_max_gen = _gen_from_gt_s(root_max_gt)
    root_cur_gen = _gen_from_gt_s(root_cur_gt)
    root_max_w = (root or {}).get("max_width")
    root_cur_w = (root or {}).get("current_width")

    spec_bus = None
    spec_gen = None
    if ep.get("role") == "gpu":
        spec_bus = _gpu_spec_bus_interface(ep.get("vendor"), ep.get("device"))
        spec_gen = _bus_interface_to_gen(spec_bus)

    # Effective ceiling for this path = min(device max gen, root max gen) when both known
    path_gens = [g for g in (max_gen, root_max_gen) if g is not None]
    path_ceiling_gen = min(path_gens) if path_gens else None

    conflicts: list[str] = []
    if spec_gen is not None and root_max_gen is not None and spec_gen > root_max_gen:
        conflicts.append("spec_gt_platform")
    if max_gen is not None and root_max_gen is not None and max_gen > root_max_gen:
        conflicts.append("device_gt_platform")
    if (
        cur_gen is not None
        and path_ceiling_gen is not None
        and cur_gen < path_ceiling_gen
    ):
        conflicts.append("trained_below_path")
    if (
        ep.get("current_width") is not None
        and root_max_w is not None
        and ep["current_width"] < root_max_w
        and ep.get("role") == "gpu"
    ):
        conflicts.append("width_below_root")

    bits: list[str] = []
    if spec_bus:
        bits.append(f"GPU spec {spec_bus} ({_gen_label(spec_gen)})")
    if max_gen is not None:
        bits.append(f"device link max {_gen_label(max_gen)} ({max_gt:g} GT/s)" if max_gt else f"device max {_gen_label(max_gen)}")
    if root_max_gen is not None:
        bits.append(
            f"CPU root {ep.get('cpu_root')} max {_gen_label(root_max_gen)}"
            + (f" ({root_max_gt:g} GT/s)" if root_max_gt else "")
        )
    if cur_gen is not None:
        bits.append(
            f"now {_gen_label(cur_gen)} x{ep.get('current_width')}"
            + (f" @ {cur_gt:g} GT/s" if cur_gt else "")
        )

    if "spec_gt_platform" in conflicts or "device_gt_platform" in conflicts:
        verdict = "gen_mismatch"
    elif "trained_below_path" in conflicts:
        verdict = "trained_low"
    elif "width_below_root" in conflicts:
        verdict = "width_limited"
    else:
        verdict = "ok"

    ctx = {
        "spec_gen_label": _gen_label(spec_gen or max_gen),
        "spec_bus": spec_bus or "",
        "spec_bus_suffix": f" ({spec_bus})" if spec_bus else "",
        "root_max_gen_label": _gen_label(root_max_gen),
        "cur_gen_label": _gen_label(cur_gen),
        "cur_width": ep.get("current_width"),
        "cur_gt_suffix": f" @ {cur_gt:g} GT/s" if cur_gt else "",
        "path_ceiling_gen_label": _gen_label(path_ceiling_gen),
        "root_max_w": root_max_w,
        "max_gen_label": _gen_label(max_gen),
        "bdf": ep.get("bdf") or "",
    }
    card = render_verdict(verdict, root=False, **ctx)
    what = (card or {}).get("what") or ""
    why = (card or {}).get("why") or ""
    research = (card or {}).get("prompt") or ""
    if not card:
        # Catalog missing: keep detection usable
        why = {
            "gen_mismatch": (
                "Platform/root PCIe generation is older than the card’s rating — "
                "the link cannot exceed the CPU root’s max gen even if the GPU is newer."
            ),
            "trained_low": (
                "Link trained below what this path allows "
                f"(path ceiling {_gen_label(path_ceiling_gen)}). "
                "Often idle power saving, ASPM, riser/cable, or BIOS gen cap — confirm under load."
            ),
            "width_limited": (
                f"Width x{ep.get('current_width')} is below CPU root max x{root_max_w} — "
                "bifurcation, shared lanes, or slot wiring may be limiting the GPU."
            ),
        }.get(verdict, "Link width/generation look consistent with this CPU root’s capability.")
        what = why
        research = ""

    facts = ("; ".join(bits) + ". ") if bits else ""
    explanation = facts + (why or what)

    ep_out = {
        **ep,
        "current_gen": cur_gen,
        "max_gen": max_gen,
        "root_max_gen": root_max_gen,
        "root_current_gen": root_cur_gen,
        "spec_bus_interface": spec_bus,
        "spec_gen": spec_gen,
        "path_ceiling_gen": path_ceiling_gen,
        "conflicts": conflicts,
        "verdict": verdict,
        "finding_id": (card or {}).get("id"),
        "severity": (card or {}).get("severity"),
        "what": what,
        "why": why,
        "explanation": explanation,
        "research_hint": research,
    }
    return ep_out


def _explain_root(root: dict) -> dict:
    """Lane budget table + explanation for one CPU root port."""
    max_w = root.get("max_width")
    cur_w = root.get("current_width")
    remaining = (max_w - cur_w) if (max_w is not None and cur_w is not None) else None
    max_gen = _gen_from_gt_s(root.get("max_gt_s"))
    cur_gen = _gen_from_gt_s(root.get("current_gt_s"))

    table: list[dict] = [
        {
            "item": "CPU root (available)",
            "bdf": root.get("bdf"),
            "lanes": max_w,
            "gen": max_gen,
            "note": f"max link {_gen_label(max_gen)}",
        },
        {
            "item": "CPU root (in use)",
            "bdf": root.get("bdf"),
            "lanes": cur_w,
            "gen": cur_gen,
            "note": f"trained {_gen_label(cur_gen)}",
        },
    ]
    for card in root.get("phys_cards") or []:
        # find matching direct child for gen
        match = next(
            (c for c in (root.get("direct_children") or []) if c.get("phys_id") == card.get("phys_id")),
            None,
        )
        table.append({
            "item": f"direct {card.get('role')}",
            "bdf": card.get("phys_id"),
            "lanes": card.get("current_width"),
            "gen": (match or {}).get("current_gen"),
            "note": "uses lanes on this root",
        })
    for ds in root.get("downstream_endpoints") or []:
        table.append({
            "item": f"via switch {ds.get('role')}",
            "bdf": ds.get("bdf"),
            "lanes": ds.get("current_width"),
            "gen": ds.get("current_gen"),
            "note": "not counted against root lane pool",
        })
    table.append({
        "item": "remaining on root",
        "bdf": root.get("bdf"),
        "lanes": remaining,
        "gen": max_gen,
        "note": "max − current link width",
    })

    if root.get("oversubscribed"):
        verdict = "oversub"
    elif remaining is not None and remaining > 0 and any(
        (c.get("role") == "gpu") for c in (root.get("phys_cards") or [])
    ):
        verdict = "gpu_headroom"
    elif cur_gen is not None and max_gen is not None and cur_gen < max_gen:
        verdict = "root_gen_low"
    else:
        verdict = "ok"

    ctx = {
        "bdf": root.get("bdf") or "",
        "consumed_width": root.get("consumed_width"),
        "max_w": max_w,
        "cur_w": cur_w,
        "remaining": remaining,
        "max_gen_label": _gen_label(max_gen),
        "cur_gen_label": _gen_label(cur_gen),
    }
    card = render_verdict(verdict, root=True, **ctx)
    what = (card or {}).get("what") or ""
    why = (card or {}).get("why") or ""
    research = (card or {}).get("prompt") or ""
    if not card:
        what = why = {
            "oversub": (
                f"Direct cards sum to ~x{root.get('consumed_width')} but root max is x{max_w} — "
                "lane oversubscription on this CPU root."
            ),
            "gpu_headroom": (
                f"Root has x{remaining} unused vs max x{max_w} while a GPU is attached at x{cur_w}. "
                "Often bifurcation or a lower-wired slot — GPU may be leaving bandwidth on the table."
            ),
            "root_gen_low": (
                f"Root trained at {_gen_label(cur_gen)} but supports {_gen_label(max_gen)}. "
                "Child device, BIOS gen cap, or power saving may be holding the link down."
            ),
        }.get(
            verdict,
            f"Root {root.get('bdf')}: x{cur_w}/{max_w} at {_gen_label(cur_gen)} — "
            "lane use looks consistent with this port.",
        )
        research = ""

    explanation = why or what

    # Child gen vs platform notes
    child_notes = []
    for ch in root.get("direct_children") or []:
        if ch.get("verdict") in ("gen_mismatch", "trained_low", "width_limited"):
            child_notes.append(f"{ch.get('bdf')}: {ch.get('verdict')}")
    if child_notes:
        explanation += " Children: " + "; ".join(child_notes) + "."

    return {
        **root,
        "max_gen": max_gen,
        "current_gen": cur_gen,
        "remaining_width": remaining,
        "lane_table": table,
        "verdict": verdict,
        "finding_id": (card or {}).get("id"),
        "severity": (card or {}).get("severity"),
        "what": what,
        "why": why,
        "explanation": explanation,
        "research_hint": research,
    }


def _class_int(bdf: str) -> int | None:
    raw = read_file(os.path.join(_PCI, bdf, "class"))
    if not raw:
        return None
    try:
        return int(raw.strip(), 16)
    except ValueError:
        return None


def _role_for_class(cls: int) -> str | None:
    base = (cls >> 16) & 0xFF
    sub = (cls >> 8) & 0xFF
    if base == 0x06:
        return None  # bridge
    if base == 0x04:
        return None  # multimedia (GPU audio) — not a lane consumer for budget
    if base == 0x03:
        return "gpu"
    if base == 0x02:
        return "nic"
    if base == 0x01:
        if sub == 0x08:
            return "nvme"
        return "storage"
    return None


def _phys_id(bdf: str) -> str:
    """Collapse multi-function to one physical card: domain:bus:dev."""
    m = re.match(r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})\.[0-9a-fA-F]$", bdf)
    return m.group(1).lower() if m else bdf.lower()


def _is_cpu_root_port(bdf: str) -> bool:
    """True for bridges directly under the host bridge (…/pci0000:00/0000:00:XX.Y)."""
    m = re.match(r"^([0-9a-fA-F]{4}):00:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", bdf)
    if not m:
        return False
    cls = _class_int(bdf)
    return cls is not None and ((cls >> 16) & 0xFF) == 0x06 and ((cls >> 8) & 0xFF) == 0x04


def _ancestor_bdfs(bdf: str) -> list[str]:
    """Walk sysfs realpath parents; return BDFs from device up to (not including) pciNNNN host."""
    path = os.path.join(_PCI, bdf)
    if not os.path.exists(path):
        return []
    try:
        cur = os.path.realpath(path)
    except OSError:
        return []
    out: list[str] = []
    for _ in range(12):
        name = os.path.basename(cur)
        if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", name):
            if not out or out[-1] != name:
                out.append(name)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        base = os.path.basename(parent)
        if base.startswith("pci") and ":" in base:
            break
        cur = parent
    return out


def _cpu_root_for(bdf: str) -> str | None:
    """Nearest CPU root port (0000:00:XX.Y bridge) above this device."""
    for anc in _ancestor_bdfs(bdf)[1:]:  # skip self
        if _is_cpu_root_port(anc):
            return anc
    return None


def _immediate_parent_bdf(bdf: str) -> str | None:
    """PCI parent BDF one hop up, or None."""
    ancs = _ancestor_bdfs(bdf)
    return ancs[1] if len(ancs) > 1 else None


def _link_fields(bdf: str) -> dict:
    base = os.path.join(_PCI, bdf)
    cur_w = _int_sysfs(os.path.join(base, "current_link_width"))
    max_w = _int_sysfs(os.path.join(base, "max_link_width"))
    cur_s = (read_file(os.path.join(base, "current_link_speed")) or "").strip() or None
    max_s = (read_file(os.path.join(base, "max_link_speed")) or "").strip() or None
    return {
        "current_width": cur_w,
        "max_width": max_w,
        "current_speed": cur_s,
        "max_speed": max_s,
        "current_gt_s": _parse_gt_s(cur_s),
        "max_gt_s": _parse_gt_s(max_s),
    }


def get_pcie_topology() -> dict:
    """Endpoints of interest + CPU root ports with child lane consumers (sysfs only)."""
    endpoints: list[dict] = []
    if not os.path.isdir(_PCI):
        return {"endpoints": [], "roots": []}

    try:
        names = os.listdir(_PCI)
    except OSError:
        return {"endpoints": [], "roots": []}

    for bdf in sorted(names):
        cls = _class_int(bdf)
        if cls is None:
            continue
        role = _role_for_class(cls)
        if not role:
            continue
        link = _link_fields(bdf)
        # Skip if no link width at all (unlikely for real endpoints)
        if link["current_width"] is None and link["max_width"] is None:
            continue
        root = _cpu_root_for(bdf)
        parent = _immediate_parent_bdf(bdf)
        vendor = (read_file(os.path.join(_PCI, bdf, "vendor")) or "").strip() or None
        device = (read_file(os.path.join(_PCI, bdf, "device")) or "").strip() or None
        endpoints.append({
            "bdf": bdf,
            "phys_id": _phys_id(bdf),
            "role": role,
            "class": cls,
            "vendor": vendor,
            "device": device,
            "cpu_root": root,
            "parent_bdf": parent,
            "direct_on_cpu_root": bool(root and parent == root),
            **link,
        })

    # Provisional root link fields for endpoint explanations
    root_links: dict[str, dict] = {}
    for ep in endpoints:
        rb = ep.get("cpu_root")
        if rb and rb not in root_links:
            root_links[rb] = {"bdf": rb, **_link_fields(rb)}

    endpoints = [
        _explain_endpoint(ep, root_links.get(ep.get("cpu_root") or ""))
        for ep in endpoints
    ]

    # Group by CPU root
    by_root: dict[str, list[dict]] = {}
    for ep in endpoints:
        root = ep.get("cpu_root")
        if not root:
            continue
        by_root.setdefault(root, []).append(ep)

    roots: list[dict] = []
    for root_bdf in sorted(by_root.keys()):
        rlink = root_links.get(root_bdf) or {"bdf": root_bdf, **_link_fields(root_bdf)}
        children = by_root[root_bdf]
        # Oversub only sums devices plugged directly on the CPU root.
        # Downstream chipset/switch leaves must not be added (false oversub).
        direct = [ch for ch in children if ch.get("direct_on_cpu_root")]
        phys_widths: dict[str, int] = {}
        phys_roles: dict[str, str] = {}
        for ch in direct:
            pid = ch["phys_id"]
            w = ch.get("current_width") or 0
            if w <= 0:
                continue
            prev = phys_widths.get(pid, 0)
            if w >= prev:
                phys_widths[pid] = w
                phys_roles[pid] = ch["role"]
        consumed = sum(phys_widths.values())
        max_w = rlink.get("max_width")
        oversub = bool(max_w is not None and max_w > 0 and consumed > max_w)
        headroom = None
        if max_w is not None and rlink.get("current_width") is not None:
            headroom = max_w - rlink["current_width"]
        root_raw = {
            "bdf": root_bdf,
            "is_cpu_root": True,
            **{k: v for k, v in rlink.items() if k != "bdf"},
            "children": children,
            "direct_children": direct,
            "phys_cards": [
                {"phys_id": pid, "role": phys_roles[pid], "current_width": w}
                for pid, w in sorted(phys_widths.items())
            ],
            "downstream_endpoints": [
                {
                    "bdf": ch["bdf"],
                    "role": ch["role"],
                    "current_width": ch.get("current_width"),
                    "current_gen": ch.get("current_gen"),
                    "verdict": ch.get("verdict"),
                }
                for ch in children
                if not ch.get("direct_on_cpu_root")
            ],
            "consumed_width": consumed,
            "oversubscribed": oversub,
            "headroom_width": headroom,
        }
        roots.append(_explain_root(root_raw))

    return {"endpoints": endpoints, "roots": roots}


def get_topology_info() -> dict:
    """Bundle NUMA + PCIe + optional nvidia topo for human/--json."""
    nodes = get_numa_nodes()
    pcie = get_pcie_topology()
    nvidia = get_nvidia_topo()
    if nvidia:
        nvidia = _ANSI_RE.sub("", nvidia)
    return {
        "numa": nodes,
        "roots": pcie.get("roots") or [],
        "endpoints": pcie.get("endpoints") or [],
        "nvidia_topo": nvidia,
    }


# Why/Research wrap target (~ one educational sentence as on GPU headroom).
_WHY_WRAP = 100


def _wrap_words(text: str, width: int) -> list[str]:
    """Word-wrap text to at most width characters per line."""
    text = (text or "").strip()
    if not text:
        return []
    if width < 20:
        width = 20
    lines: list[str] = []
    for para in text.splitlines() or [text]:
        para = para.strip()
        if not para:
            continue
        words = para.split()
        cur: list[str] = []
        cur_len = 0
        for word in words:
            add = len(word) + (1 if cur else 0)
            if cur and cur_len + add > width:
                lines.append(" ".join(cur))
                cur = [word]
                cur_len = len(word)
            else:
                cur.append(word)
                cur_len += add
        if cur:
            lines.append(" ".join(cur))
    return lines or [text]


def _kv(label: str, value: str | None, width: int = 14, *, wrap: int | None = None) -> None:
    if value is None or value == "":
        return
    if wrap is None:
        print(f"  {label:>{width}}  {value}")
        return
    lines = _wrap_words(str(value), wrap)
    if not lines:
        return
    print(f"  {label:>{width}}  {lines[0]}")
    pad = " " * (2 + width + 2)
    for line in lines[1:]:
        print(f"{pad}{line}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _fmt_mem_gib(kib: int | None) -> str:
    if kib is None or kib <= 0:
        return "—"
    return f"{kib / (1024 * 1024):.1f} GiB"


def _root_summary_line(r: dict, *, color: bool) -> str:
    max_w = r.get("max_width")
    cur_w = r.get("current_width")
    rem = r.get("remaining_width")
    roles = []
    for c in r.get("phys_cards") or []:
        roles.append(f"{c.get('role')} x{c.get('current_width')}")
    for d in r.get("downstream_endpoints") or []:
        roles.append(f"{d.get('role')}(sw) x{d.get('current_width')}")
    role_s = ", ".join(roles) if roles else "—"
    return (
        f"{verdict_bracket(r.get('verdict'), enabled=color)} "
        f"x{cur_w}/{max_w} {_gen_label(r.get('current_gen'))} "
        f"(remain x{rem})  {role_s}"
    )


def print_topology_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_topology_info()
    nodes = info.get("numa") or []
    roots = info.get("roots") or []
    eps = info.get("endpoints") or []
    nvidia = info.get("nvidia_topo")

    interesting = [r for r in roots if r.get("verdict") and r.get("verdict") != "ok"]
    ok_roots = [r for r in roots if r.get("verdict") == "ok"]

    labels = [
        "NUMA nodes", "CPU online", "Note", "Root", "Available", "In use",
        "Remaining", "Direct", "Via switch", "Why", "Prompt",
        "Endpoints", "GPU affinity",
    ]
    labels.extend(n.get("node") or "" for n in nodes)
    labels.extend(r.get("bdf") or "" for r in roots)
    labels.extend(ep.get("bdf") or "" for ep in eps)
    w = _label_width(*labels)

    print("\nTopology")

    _section("Summary")
    if not nodes:
        online = (read_file(os.path.join(_CPU, "online")) or "").strip()
        _kv("NUMA nodes", "none under /sys (single-node or unavailable)", w)
        if online:
            _kv("CPU online", online, w)
    else:
        _kv("NUMA nodes", str(len(nodes)), w)
        for n in nodes:
            mem = _fmt_mem_gib(n.get("mem_total_kib"))
            if mem == "—":
                mem = "— (node meminfo unavailable)"
            _kv(n["node"], f"cpus={n.get('cpulist') or '—'}  mem={mem}", w)

    if not roots:
        _kv("Note", "no GPU/NVMe/storage/NIC endpoints with link width", w)
    else:
        if interesting:
            note = attention_note(
                f"{len(roots)} CPU root(s) with endpoints; "
                f"{len(interesting)} need attention",
                enabled=color,
            )
        else:
            note = f"{len(roots)} CPU root(s) with endpoints (all ok)"
        _kv("Note", note, w)
        for r in interesting:
            _kv(r.get("bdf") or "Root", _root_summary_line(r, color=color), w)
        if ok_roots and interesting:
            _kv(
                "Note",
                f"{len(ok_roots)} other root(s) ok — see Roots below",
                w,
            )
        elif ok_roots and not interesting:
            for r in ok_roots:
                _kv(r.get("bdf") or "Root", _root_summary_line(r, color=color), w)

    _section("Roots")
    if not roots:
        _kv("Note", "none with tracked endpoints", w)
    else:
        # Attention roots first, then ok
        ordered = interesting + [r for r in roots if r not in interesting]
        for r in ordered:
            verdict = r.get("verdict") or "?"
            _kv(
                "Root",
                f"{r.get('bdf')}  {verdict_bracket(verdict, enabled=color)}",
                w,
            )
            max_w, cur_w = r.get("max_width"), r.get("current_width")
            _kv(
                "Available",
                f"x{max_w} {_gen_label(r.get('max_gen'))}",
                w,
            )
            _kv(
                "In use",
                f"x{cur_w} {_gen_label(r.get('current_gen'))}",
                w,
            )
            rem = r.get("remaining_width")
            if rem is not None:
                _kv("Remaining", f"x{rem}", w)
            directs = []
            for c in r.get("phys_cards") or []:
                directs.append(f"{c.get('role')} {c.get('phys_id')} x{c.get('current_width')}")
            if directs:
                _kv("Direct", "; ".join(directs), w)
            via = []
            for d in r.get("downstream_endpoints") or []:
                via.append(
                    f"{d.get('role')} {d.get('bdf')} x{d.get('current_width')}"
                )
            if via:
                _kv("Via switch", "; ".join(via), w)
            if verdict != "ok":
                if r.get("what"):
                    _kv("What", r.get("what"), w, wrap=_WHY_WRAP)
                _kv("Why", r.get("why") or r.get("explanation"), w, wrap=_WHY_WRAP)
                if r.get("research_hint"):
                    _kv("Prompt", r.get("research_hint"), w, wrap=_WHY_WRAP)
            print()

    _section("Endpoints")
    if not eps:
        _kv("Note", "none found", w)
    else:
        for ep in eps:
            cw = ep.get("current_width")
            cs = ep.get("current_gt_s")
            wstr = f"x{cw}" if cw is not None else "x?"
            sstr = f"{cs:g} GT/s" if cs is not None else "—"
            val = (
                f"{ep.get('role')}  {wstr}  {sstr}  "
                f"{_gen_label(ep.get('current_gen'))}  "
                f"{verdict_bracket(ep.get('verdict'), enabled=color)}"
            )
            _kv(ep["bdf"], val, w)
            if ep.get("verdict") and ep.get("verdict") != "ok":
                if ep.get("what"):
                    _kv("What", ep.get("what"), w, wrap=_WHY_WRAP)
                _kv("Why", ep.get("why") or ep.get("explanation"), w, wrap=_WHY_WRAP)
                if ep.get("research_hint"):
                    _kv("Prompt", ep.get("research_hint"), w, wrap=_WHY_WRAP)

    _section("GPU affinity")
    if nvidia:
        # Prefer short affinity lines; skip empty Legend padding somewhat
        lines = [ln.rstrip() for ln in nvidia.splitlines() if ln.strip()]
        # Drop legend block for human brevity — keep matrix header + GPU rows
        show: list[str] = []
        for ln in lines:
            if ln.startswith("Legend:"):
                break
            show.append(ln)
        if not show:
            show = lines[:12]
        for ln in show:
            _kv("nvidia-smi", ln, w)
        _kv("Note", "full matrix: nvidia-smi topo -m", w)
    else:
        _kv("Note", "nvidia-smi topo unavailable", w)

    _section("Notes")
    _kv("Note", "board slots / populated → lsslots", w)
    _kv("Note", "device health → lsgpu / lsnvme / lsdisk", w)
    _kv("Note", "Prompt = short text to paste into an LLM", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.

    info = get_topology_info()

    if "--json" in args or "-j" in args:
        # Drop bulky lane_table from JSON roots optional? keep for completeness
        print(json.dumps(info, indent=2, default=str))
        return

    print_topology_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
