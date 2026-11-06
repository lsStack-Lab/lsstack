"""lsstack: full system report. Balance rules, high-impact checks, then aggregated ls* output.

Supports --json for a machine-readable report with stable check IDs.

FUTURE(server-profile): on rack chassis, N/A ATX PSU/ports/80% headroom and keep
inventory + ECC + NUMA. Workstation-lab remains the product until then.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
from datetime import datetime
from typing import Any, TextIO

# Report is the aggregator: it may import subsystem modules for structured data.
from lsstack import gpu as gpu_mod
from lsstack import memory as memory_mod
from lsstack import network as network_mod
from lsstack import nvme as nvme_mod
from lsstack import proc as proc_mod
from lsstack import slots as slots_mod
from lsstack import deps as deps_mod
from lsstack import topology as topology_mod
from lsstack import __version__
from lsstack.common import (
    default_reports_dir,
    load_json,
    read_dmi_id,
    read_file,
    sudo_full_results_note,
    system_binary,
)

SCHEMA_VERSION = 2
_SYS_PCI = "/sys/bus/pci/devices"


def _bin_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


def _check(
    check_id: str,
    category: str,
    status: str,
    message: str,
    *,
    evidence: dict[str, Any] | None = None,
    remedy: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """One check with a stable id (pass|fail|warn|na)."""
    out: dict[str, Any] = {
        "id": check_id,
        "category": category,
        "status": status,
        "message": message,
        "evidence": evidence or {},
    }
    if remedy:
        out["remedy"] = remedy
    if url:
        out["url"] = url
    return out


def _dimm_probe() -> dict[str, Any]:
    """Probe populated DIMM count via first-party SMBIOS Type 17 reader.

    Returns ``{count, reason}`` where reason is None on success, else
    ``needs_sudo`` | ``no_table`` | ``no_dimm_data`` | ``failed``.
    """
    from lsstack.dmi import populated_dimm_count

    count, reason = populated_dimm_count()
    return {"count": count, "reason": reason}


def _dimm_count() -> int | None:
    """Return number of populated DIMMs from DMI Type 17, or None if unavailable."""
    return _dimm_probe().get("count")


def _psu_watts_from_config() -> int | None:
    """Read PSU wattage from ~/.lsstack.conf (psu_watts=N). Returns None if not set."""
    path = os.path.expanduser("~/.lsstack.conf")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().split("#")[0]
                if line.startswith("psu_watts="):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def _vram_gib_from_nvidia_smi_mib(s: str) -> float | None:
    """Parse '8192 MiB' or bare '8192' (nounits) -> GiB."""
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*MiB", s, re.I)
    if m:
        return float(m.group(1)) / 1024.0
    try:
        return float(str(s).strip()) / 1024.0
    except (ValueError, TypeError):
        return None


def _temperature_value(s: str) -> int | None:
    """Parse temperature string to int (e.g. '65 C' -> 65)."""
    if not s:
        return None
    m = re.search(r"(\d+)", str(s))
    return int(m.group(1)) if m else None


def _ram_vram_check_id(label: str) -> str:
    """Stable id from rule label, e.g. '1.5:1' -> 'ram_vram_1_5_1'."""
    safe = re.sub(r"[^0-9A-Za-z]+", "_", str(label).strip()).strip("_").lower()
    return f"ram_vram_{safe}" if safe else "ram_vram"


def _pcie_validity_checks() -> list[dict[str, Any]]:
    """Phase A/B: per-endpoint width/speed + CPU root oversubscription (sysfs)."""
    checks: list[dict[str, Any]] = []
    topo = topology_mod.get_pcie_topology()
    endpoints = topo.get("endpoints") or []
    roots = topo.get("roots") or []

    if not endpoints:
        checks.append(_check(
            "pcie_topology", "pcie", "na",
            "PCIe topology: no GPU/NVMe/NIC endpoints with link width in sysfs",
        ))
        return checks

    # Counters per role for stable ids
    role_idx: dict[str, int] = {}
    for ep in endpoints:
        role = ep.get("role") or "pci"
        role_idx[role] = role_idx.get(role, 0) + 1
        n = role_idx[role]
        bdf = ep.get("bdf") or "?"
        cur_w = ep.get("current_width")
        max_w = ep.get("max_width")
        cur_gt = ep.get("current_gt_s")
        max_gt = ep.get("max_gt_s")
        root = ep.get("cpu_root")
        root_max = None
        root_cur = None
        if root:
            for r in roots:
                if r.get("bdf") == root:
                    root_max = r.get("max_width")
                    root_cur = r.get("current_width")
                    break

        expl = ep.get("explanation")
        research = ep.get("research_hint")
        verdict = ep.get("verdict") or "ok"
        ev = {
            "bdf": bdf,
            "role": role,
            "current_width": cur_w,
            "max_width": max_w,
            "current_gt_s": cur_gt,
            "max_gt_s": max_gt,
            "current_gen": ep.get("current_gen"),
            "max_gen": ep.get("max_gen"),
            "spec_gen": ep.get("spec_gen"),
            "spec_bus_interface": ep.get("spec_bus_interface"),
            "root_max_gen": ep.get("root_max_gen"),
            "path_ceiling_gen": ep.get("path_ceiling_gen"),
            "cpu_root": root,
            "root_max_width": root_max,
            "root_current_width": root_cur,
            "conflicts": ep.get("conflicts") or [],
            "verdict": verdict,
            "explanation": expl,
            "research_hint": research,
        }

        def _remedy(*parts: str) -> str:
            bits = [p for p in parts if p]
            if research:
                bits.append(research)
            return " ".join(bits)

        # --- Phase A: width ---
        wid = f"pcie_width_{role}_{n}"
        if cur_w is None:
            checks.append(_check(
                wid, "pcie", "na",
                f"PCIe width ({role} {bdf}): unreadable",
                evidence=ev,
            ))
        elif max_w is not None and cur_w < max_w:
            checks.append(_check(
                wid, "pcie", "warn",
                f"PCIe width ({role} {bdf}): x{cur_w} < device max x{max_w}",
                evidence=ev,
                remedy=_remedy(
                    "Check slot wiring, bifurcation, and that the card is fully seated.",
                    expl,
                ),
            ))
        else:
            checks.append(_check(
                wid, "pcie", "pass",
                f"PCIe width ({role} {bdf}): x{cur_w}"
                + (f" (max x{max_w})" if max_w is not None else ""),
                evidence=ev,
            ))

        # --- Phase A: speed / generation ---
        sid = f"pcie_speed_{role}_{n}"
        gid = f"pcie_gen_{role}_{n}"
        if cur_gt is None:
            checks.append(_check(
                sid, "pcie", "na",
                f"PCIe speed ({role} {bdf}): unreadable",
                evidence=ev,
            ))
        elif max_gt is not None and cur_gt + 0.05 < max_gt:
            checks.append(_check(
                sid, "pcie", "warn",
                f"PCIe speed ({role} {bdf}): {cur_gt:g} GT/s < max {max_gt:g} GT/s"
                + (f" (now Gen{ep.get('current_gen')}, device Gen{ep.get('max_gen')})"
                   if ep.get("current_gen") is not None else ""),
                evidence=ev,
                remedy=_remedy(expl),
            ))
        else:
            checks.append(_check(
                sid, "pcie", "pass",
                f"PCIe speed ({role} {bdf}): {cur_gt:g} GT/s",
                evidence=ev,
            ))

        # Explicit platform vs card generation (CPU root / board path vs GPU spec)
        if verdict == "gen_mismatch":
            checks.append(_check(
                gid, "pcie", "warn",
                f"PCIe gen ({role} {bdf}): card "
                f"{ep.get('spec_bus_interface') or ('Gen' + str(ep.get('max_gen')))} "
                f"on CPU root max Gen{ep.get('root_max_gen')} — platform caps the link",
                evidence=ev,
                remedy=_remedy(expl),
            ))
        elif ep.get("spec_gen") is not None and ep.get("root_max_gen") is not None:
            checks.append(_check(
                gid, "pcie", "pass",
                f"PCIe gen ({role} {bdf}): spec Gen{ep.get('spec_gen')} within "
                f"root Gen{ep.get('root_max_gen')} ceiling",
                evidence=ev,
            ))
        elif role == "gpu":
            checks.append(_check(
                gid, "pcie", "na",
                f"PCIe gen ({role} {bdf}): N/A (need GPU JSON bus_interface and root max speed)",
                evidence=ev,
            ))

    # --- Phase B: CPU root oversubscription ---
    if not roots:
        checks.append(_check(
            "pcie_root_budget", "pcie", "na",
            "PCIe root budget: no CPU root ports with tracked endpoints",
        ))
    for r in roots:
        rbdf = r.get("bdf") or "?"
        cons = r.get("consumed_width") or 0
        max_w = r.get("max_width")
        cur_w = r.get("current_width")
        cards = r.get("phys_cards") or []
        cid = f"pcie_parent_oversub_{rbdf.replace(':', '_').replace('.', '_')}"
        expl = r.get("explanation")
        research = r.get("research_hint")
        remedy = " ".join(x for x in (expl, research) if x)
        ev = {
            "bdf": rbdf,
            "max_width": max_w,
            "current_width": cur_w,
            "consumed_width": cons,
            "phys_cards": cards,
            "oversubscribed": r.get("oversubscribed"),
            "remaining_width": r.get("remaining_width"),
            "max_gen": r.get("max_gen"),
            "current_gen": r.get("current_gen"),
            "lane_table": r.get("lane_table"),
            "verdict": r.get("verdict"),
            "explanation": expl,
            "research_hint": research,
        }
        if max_w is None:
            checks.append(_check(
                cid, "pcie", "na",
                f"PCIe root {rbdf}: max width unreadable",
                evidence=ev,
            ))
        elif r.get("oversubscribed"):
            checks.append(_check(
                cid, "pcie", "fail",
                f"PCIe root {rbdf}: endpoints use ~x{cons} > root max x{max_w} (oversubscribed)",
                evidence=ev,
                remedy=remedy or "Move a device to another root/slot or reduce bifurcation sharing.",
            ))
        elif (
            cur_w is not None and max_w is not None and cur_w < max_w
            and any(
                ep.get("cpu_root") == rbdf
                and ep.get("role") == "gpu"
                and ep.get("current_width") is not None
                and ep.get("max_width") is not None
                and ep["current_width"] < ep["max_width"]
                for ep in endpoints
            )
        ):
            rem = r.get("remaining_width")
            checks.append(_check(
                cid, "pcie", "warn",
                f"PCIe root {rbdf}: link x{cur_w}/max x{max_w}"
                + (f" ({rem} lanes free)" if rem is not None else "")
                + f" with GPU child below device max",
                evidence=ev,
                remedy=remedy or "GPU root is below its max width — check bifurcation and competing devices.",
            ))
        else:
            checks.append(_check(
                cid, "pcie", "pass",
                f"PCIe root {rbdf}: x{cur_w}/{max_w}, endpoint cards ~x{cons}"
                + (f", {r.get('remaining_width')} free" if r.get("remaining_width") is not None else ""),
                evidence=ev,
            ))

    return checks


def _probe_rebar_for_bdf(bdf: str) -> dict[str, Any]:
    """Sysfs Resizable BAR probe for one PCI device. Returns supported, max_bar_mib, status hint."""
    path = os.path.join(_SYS_PCI, bdf)
    if not os.path.isdir(path):
        return {"bdf": bdf, "supported": False, "max_bar_mib": 0, "resize_files": 0}
    resize_files = [n for n in os.listdir(path) if n.startswith("resource") and n.endswith("_resize")]
    max_bar = 0
    resource = read_file(os.path.join(path, "resource"))
    if resource:
        for line in resource.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                start, end = int(parts[0], 16), int(parts[1], 16)
            except ValueError:
                continue
            if end > start:
                max_bar = max(max_bar, (end - start + 1) // (1024 * 1024))
    return {
        "bdf": bdf,
        "supported": bool(resize_files),
        "max_bar_mib": max_bar,
        "resize_files": len(resize_files),
    }


def _rebar_checks(gpu_list: list[dict]) -> list[dict[str, Any]]:
    """Build ReBAR check(s) from sysfs for each GPU BDF."""
    checks: list[dict[str, Any]] = []
    if not gpu_list:
        checks.append(_check(
            "rebar", "pcie", "na",
            "ReBAR: N/A (no GPU devices)",
        ))
        return checks
    for i, g in enumerate(gpu_list):
        bdf = g.get("bus_id") or ""
        info = _probe_rebar_for_bdf(bdf) if bdf else {"supported": False, "max_bar_mib": 0, "resize_files": 0, "bdf": bdf}
        cid = f"rebar_gpu_{i + 1}" if len(gpu_list) > 1 else "rebar"
        if not info.get("supported"):
            checks.append(_check(
                cid, "pcie", "na",
                f"ReBAR: not advertised on {bdf or 'GPU'} (no resource*_resize in sysfs)",
                evidence=info,
                remedy="Enable Above 4G Decoding / Resizable BAR in BIOS if the GPU and board support it.",
            ))
        elif info.get("max_bar_mib", 0) > 256:
            checks.append(_check(
                cid, "pcie", "pass",
                f"ReBAR: enabled on {bdf} (largest BAR {info['max_bar_mib']} MiB)",
                evidence=info,
            ))
        else:
            checks.append(_check(
                cid, "pcie", "warn",
                f"ReBAR: capable on {bdf} but largest BAR is {info['max_bar_mib']} MiB (often 256 without full resize)",
                evidence=info,
                remedy="Enable Resizable BAR / Above 4G Decoding in BIOS; confirm largest GPU BAR exceeds 256 MiB.",
            ))
    return checks


def _os_release() -> dict[str, str | None]:
    """Parse /etc/os-release into a small dict."""
    raw = read_file("/etc/os-release") or ""
    data: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip().strip('"')
    return {
        "id": data.get("ID"),
        "name": data.get("NAME"),
        "version": data.get("VERSION_ID") or data.get("VERSION"),
        "pretty_name": data.get("PRETTY_NAME"),
    }


def _collect_identity() -> dict[str, Any]:
    """Stable join keys: serials, UUID, board, OS."""
    dmi = read_dmi_id(
        "product_uuid", "product_serial", "product_name", "product_version",
        "sys_vendor", "board_vendor", "board_name", "board_version",
        "board_serial", "board_asset_tag",
        "chassis_serial", "chassis_asset_tag", "chassis_vendor", "chassis_type",
        "bios_vendor", "bios_version", "bios_date",
    )
    asset = dmi.get("board_asset_tag") or dmi.get("chassis_asset_tag")
    if asset and asset.lower() in ("default string", "none", "not specified", "n/a"):
        asset = None
    return {
        "hostname": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "uname": " ".join(platform.uname()),
        "arch": platform.machine(),
        "product_uuid": dmi.get("product_uuid"),
        "product_serial": dmi.get("product_serial"),
        "product_name": dmi.get("product_name"),
        "sys_vendor": dmi.get("sys_vendor"),
        "board_vendor": dmi.get("board_vendor"),
        "board_name": dmi.get("board_name"),
        "board_version": dmi.get("board_version"),
        "board_serial": dmi.get("board_serial"),
        "chassis_serial": dmi.get("chassis_serial"),
        "asset_tag": asset,
        "bios_vendor": dmi.get("bios_vendor"),
        "bios_version": dmi.get("bios_version"),
        "bios_date": dmi.get("bios_date"),
        "os": _os_release(),
    }


def _component(
    ctype: str,
    *,
    slot: str | None = None,
    bdf: str | None = None,
    vendor: str | None = None,
    model: str | None = None,
    serial: str | None = None,
    part_number: str | None = None,
    firmware: str | None = None,
    capacity: str | None = None,
    uuid: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": ctype,
        "slot": slot,
        "bdf": bdf,
        "vendor": vendor,
        "model": model,
        "serial": serial,
        "part_number": part_number,
        "firmware": firmware,
        "capacity": capacity,
        "uuid": uuid,
    }
    if extra:
        row["extra"] = extra
    return row


def _collect_components(cpu_info: dict, gpu_list: list[dict]) -> list[dict[str, Any]]:
    """FRU-style components (CPU, DIMM, GPU, NVMe, NIC)."""
    comps: list[dict[str, Any]] = []

    model = cpu_info.get("model_name") or cpu_info.get("model")
    comps.append(_component(
        "cpu",
        vendor=(cpu_info.get("vendor") or cpu_info.get("vendor_id")),
        model=model,
        extra={
            "sockets": cpu_info.get("sockets"),
            "cores": cpu_info.get("cores") or cpu_info.get("cpu_cores"),
            "threads": cpu_info.get("threads") or cpu_info.get("cpus"),
            "memory_channels": cpu_info.get("memory_channels"),
        },
    ))

    for dimm in memory_mod.get_dimm_devices():
        size = dimm.get("size") or "0"
        if size in ("0", "0 GiB", "0 MiB"):
            continue
        comps.append(_component(
            "dimm",
            slot=dimm.get("slot") or dimm.get("label"),
            vendor=dimm.get("manufacturer"),
            model=dimm.get("description"),
            serial=dimm.get("serial"),
            part_number=dimm.get("part_number"),
            capacity=size,
            extra={
                "type": dimm.get("type"),
                "rated_mts": dimm.get("rated_mts"),
                "running_mts": dimm.get("running_mts"),
            },
        ))

    for g in gpu_list:
        ns = g.get("nvidia_smi") or {}
        if ns.get("_error"):
            ns = {}
        specs = g.get("specs") or {}
        cap = None
        if specs.get("size_gib") is not None:
            cap = f"{specs['size_gib']} GiB"
        elif ns.get("memory_total"):
            cap = f"{ns['memory_total']} MiB"
        comps.append(_component(
            "gpu",
            slot=g.get("bus_id"),
            bdf=g.get("bus_id"),
            vendor="NVIDIA" if (g.get("vendor") or "").lower() in ("10de", "nvidia") else g.get("vendor"),
            model=ns.get("name") or specs.get("model") or g.get("lspci_name"),
            serial=ns.get("serial"),
            part_number=ns.get("board_part_number") or ns.get("gpu_part_number"),
            firmware=ns.get("vbios_version"),
            capacity=cap,
            uuid=ns.get("uuid"),
            extra={
                "gpu_part_number": ns.get("gpu_part_number"),
                "board_part_number": ns.get("board_part_number"),
                "driver_version": ns.get("driver_version"),
                "pci_vendor": g.get("vendor"),
                "pci_device": g.get("device"),
            },
        ))

    for n in nvme_mod.get_nvme_info():
        for ns in n.get("namespaces") or [None]:
            cap = None
            slot = n.get("controller")
            if ns:
                slot = ns.get("name") or slot
                if ns.get("size_gib"):
                    cap = f"{ns['size_gib']} GiB"
            comps.append(_component(
                "nvme",
                slot=slot,
                bdf=n.get("pci_bdf"),
                vendor=None,
                model=n.get("model"),
                serial=n.get("serial"),
                firmware=n.get("firmware"),
                capacity=cap,
                extra={
                    "controller": n.get("controller"),
                    "state": n.get("state"),
                    "pcie_link_speed": n.get("pcie_link_speed"),
                    "pcie_link_width": n.get("pcie_link_width"),
                    "temp_c": n.get("temp_c"),
                },
            ))

    for nic in network_mod.get_nic_devices():
        iface_names = nic.get("ifaces") or []
        comps.append(_component(
            "nic",
            slot=iface_names[0] if len(iface_names) == 1 else (nic.get("bdf")),
            bdf=nic.get("bdf"),
            vendor=nic.get("vendor"),
            model=nic.get("model"),
            serial=None,
            capacity=f"{nic['speed_mbps']} Mbps" if nic.get("speed_mbps") else None,
            extra={
                "subclass": nic.get("subclass"),
                "driver": nic.get("driver"),
                "ifaces": iface_names,
                "mac": nic.get("mac"),
                "permanent_mac": nic.get("permanent_mac"),
                "current_mac": nic.get("current_mac"),
                "speed_mbps": nic.get("speed_mbps"),
                "pcie_link_speed": nic.get("pcie_link_speed"),
                "pcie_link_width": nic.get("pcie_link_width"),
                "add_on": nic.get("add_on"),
                "subsystem_vendor": nic.get("subsystem_vendor"),
                "subsystem_device": nic.get("subsystem_device"),
                "pci_device": nic.get("device"),
            },
        ))

    return comps


def _collect_interfaces() -> list[dict[str, Any]]:
    """Network interfaces (name, MAC, state, PCI, addresses)."""
    rows: list[dict[str, Any]] = []
    for i in network_mod.get_network_info():
        if i.get("is_loopback"):
            continue
        rows.append({
            "name": i.get("name"),
            "mac": i.get("mac"),
            "permanent_mac": i.get("permanent_mac"),
            "operstate": i.get("operstate"),
            "speed_mbps": i.get("speed_mbps"),
            "duplex": i.get("duplex"),
            "mtu": i.get("mtu"),
            "pci_bdf": i.get("pci_bdf"),
            "driver": i.get("driver"),
            "addrs": i.get("addrs") or [],
        })
    return rows


def collect_report() -> dict[str, Any]:
    """Gather balance + high-impact checks into a structured report dict."""
    cpu_info = proc_mod.get_cpu_info()
    gpu_list, _ = gpu_mod.get_gpu_info()
    mem_info = memory_mod.get_memory_info()
    identity = _collect_identity()
    components = _collect_components(cpu_info, gpu_list)
    interfaces = _collect_interfaces()

    total_kb = mem_info.get("total_kb") or 0
    system_ram_gib = total_kb / (1024 * 1024) if total_kb else 0.0
    total_vram_gib = 0.0
    for g in gpu_list:
        if g.get("specs") and g["specs"].get("size_gib") is not None:
            total_vram_gib += float(g["specs"]["size_gib"])
        elif g.get("nvidia_smi") and not g["nvidia_smi"].get("_error") and g["nvidia_smi"].get("memory_total"):
            v = _vram_gib_from_nvidia_smi_mib(g["nvidia_smi"]["memory_total"])
            if v is not None:
                total_vram_gib += v

    from lsstack.budget import estimate_system_budget

    budget = estimate_system_budget(cpu_info=cpu_info, gpu_list=gpu_list)
    cpu_tdp = int(budget.get("cpu_tdp_w") or 0)
    gpu_tdp = int(budget.get("gpu_tdp_w") or 0)
    estimated_tdp = int(budget.get("total_w") or 0)  # full design budget (spec + est)

    checks: list[dict[str, Any]] = []

    # RAM vs VRAM
    rules = load_json("balance_rules.json") or {}
    ram_vram = rules.get("ram_vram_rules", [])
    if not ram_vram:
        ram_vram = [
            {
                "label": "1:1",
                "ratio": 1.0,
                "url": "",
                "note": "Typical adequate floor: system RAM ≥ total VRAM",
            },
            {
                "label": "1.5:1",
                "ratio": 1.5,
                "url": "",
                "note": "Comfortable headroom when the CPU stages large training batches",
            },
            {
                "label": "2:1",
                "ratio": 2.0,
                "url": "",
                "note": "Certified multi-GPU server BOM guidance — not a home/lab workstation floor",
                "on_fail": "na",
            },
        ]
    ram_vram_footnote = (rules.get("ram_vram_footnote") or "").strip() or (
        "RAM:VRAM rules move with the source and the job: inference and light fine-tune "
        "often sit near 1:1; CPU-side dataloading and offload push higher; certified-server "
        "guides still list 2× as a multi-GPU BOM minimum."
    )
    for r in ram_vram:
        ratio = float(r.get("ratio", 1.0))
        label = r.get("label", str(ratio))
        url = r.get("url") or None
        note = r.get("note", "")
        on_fail = (r.get("on_fail") or "fail").strip().lower()
        if on_fail not in ("fail", "warn", "na"):
            on_fail = "fail"
        cid = _ram_vram_check_id(label)
        evidence = {
            "system_ram_gib": round(system_ram_gib, 2),
            "total_vram_gib": round(total_vram_gib, 2),
            "ratio": ratio,
            "label": label,
            "on_fail": on_fail,
            "footnote": ram_vram_footnote,
        }
        if total_vram_gib <= 0:
            checks.append(_check(
                cid, "balance", "na",
                f"RAM vs VRAM {label}: N/A (no VRAM) — {note}",
                evidence=evidence, url=url,
            ))
        elif system_ram_gib >= ratio * total_vram_gib:
            checks.append(_check(
                cid, "balance", "pass",
                f"RAM vs VRAM {label}: Pass — {note}",
                evidence=evidence, url=url,
            ))
        else:
            status_word = {"fail": "Fail", "warn": "Warn", "na": "N/A"}[on_fail]
            remedy = None
            if on_fail == "fail":
                remedy = (
                    "Add system RAM so RAM ≥ ratio × total VRAM, or reduce GPU VRAM footprint."
                )
            elif on_fail == "warn":
                remedy = (
                    "Optional headroom only — fine for many lab workloads below this ratio; "
                    "raise RAM if CPU dataloading or multi-GPU staging stalls the GPUs."
                )
            checks.append(_check(
                cid, "balance", on_fail,
                f"RAM vs VRAM {label}: {status_word} — {note}",
                evidence=evidence, url=url,
                remedy=remedy,
            ))

    # DIMMs vs channels
    try:
        ch = int(cpu_info.get("memory_channels", 0)) or None
    except (TypeError, ValueError):
        ch = None
    dimm_probe = _dimm_probe()
    dimm_count = dimm_probe.get("count")
    dimm_ev = {"dimms": dimm_count, "channels": ch, "probe_reason": dimm_probe.get("reason")}
    if ch is not None and dimm_count is not None:
        if dimm_count in (ch, 2 * ch):
            checks.append(_check(
                "dimms_vs_channels", "balance", "pass",
                f"DIMMs vs channels: Pass (DIMMs={dimm_count}, channels={ch})",
                evidence=dimm_ev,
            ))
        else:
            checks.append(_check(
                "dimms_vs_channels", "balance", "fail",
                f"DIMMs vs channels: Fail (DIMMs={dimm_count}, expected {ch} or {2 * ch})",
                evidence=dimm_ev,
                remedy=f"Populate {ch} or {2 * ch} DIMMs for full channel utilization.",
            ))
    else:
        reason = dimm_probe.get("reason")
        if ch is None and reason is None:
            msg = "DIMMs vs channels: N/A (CPU channels not in spec catalog)"
            remedy = "CPU not in lab catalog (cpu_specs_wkstn); live model still in lsproc."
        elif reason == "needs_sudo":
            msg = "DIMMs vs channels: N/A (DMI table needs sudo for SMBIOS/DIMM SPD)"
            remedy = "This field needs sudo to read /sys/firmware/dmi/tables/DMI (Type 17)"
        elif reason == "no_table":
            msg = "DIMMs vs channels: N/A (no SMBIOS DMI table in sysfs)"
            remedy = "Unexpected on bare metal; VMs may omit DMI. Sudo does not create a missing table."
        elif ch is None:
            msg = "DIMMs vs channels: N/A (CPU channels not in spec catalog)"
            remedy = "CPU not in lab catalog (cpu_specs_wkstn); live model still in lsproc."
        else:
            msg = "DIMMs vs channels: N/A (no populated DIMM rows in DMI Type 17)"
            remedy = "Inspect Type 17 with lsram (sudo if that row notes DMI access)"
        checks.append(_check(
            "dimms_vs_channels", "balance", "na",
            msg,
            evidence=dimm_ev,
            remedy=remedy,
        ))

    # PCIe slot balance (GPU vs mobo_slots max lanes)
    slot_info = slots_mod.get_slot_info()
    slot_ok = []
    slot_warn = []
    for s in slot_info:
        if s.get("max_lanes") is not None and s.get("current_width") is not None:
            bdf = s.get("bdf", "unknown")
            if s.get("warn_undersized"):
                slot_warn.append(f"{bdf}: {s.get('pcie_lanes')}-lane GPU in {s['max_lanes']}-lane slot")
            elif s["max_lanes"] == s["current_width"]:
                slot_ok.append(f"{bdf} x{s['current_width']} in x{s['max_lanes']} slot")
            else:
                slot_warn.append(f"{bdf} x{s['current_width']} in x{s['max_lanes']} slot (below max)")
    if slot_ok or slot_warn:
        for x in slot_ok:
            checks.append(_check(
                "pcie_slot_balance", "balance", "pass",
                f"PCIe slot: {x} — OK",
                evidence={"slots": slot_info},
            ))
        for x in slot_warn:
            checks.append(_check(
                "pcie_slot_undersized", "balance", "warn",
                f"PCIe slot: {x}",
                evidence={"slots": slot_info},
                remedy="Move GPU to a higher-lane slot or update the mobo_slots catalog if the slot map is wrong.",
            ))
    elif slot_info:
        # GPUs seen, but board/slot not in mobo_slots.json (or DMI designation missing).
        from lsstack.dmi import probe_table, system_slots

        dmi_probe = probe_table()
        if not dmi_probe.get("ok"):
            msg = (
                "PCIe slot balance: N/A (DMI Type 9 needs sudo for slot designations)"
            )
            remedy = (
                "This field needs sudo for DMI Type 9 designations; then add the board "
                "to the mobo_slots catalog. Lane train → lstopology."
            )
        elif not system_slots():
            msg = (
                "PCIe slot balance: N/A (no DMI Type 9 slots decoded; "
                "board/slot may be missing from mobo_slots catalog)"
            )
            remedy = (
                "Add this board to the mobo_slots catalog (lab overlay). "
                "Lane train → lstopology."
            )
        else:
            msg = (
                "PCIe slot balance: N/A (GPU present, but this board/slot is not in "
                "mobo_slots catalog)"
            )
            remedy = (
                "Add this board to the mobo_slots catalog (lab overlay). "
                "Lane train → lstopology."
            )
        checks.append(_check(
            "pcie_slot_balance", "balance", "na",
            msg,
            evidence={"slots": slot_info},
            remedy=remedy,
        ))
    else:
        checks.append(_check(
            "pcie_slot_balance", "balance", "na",
            "PCIe slot balance: N/A (no GPU endpoints found)",
            remedy="Confirm a discrete GPU is visible to the OS (lsgpu).",
        ))

    # GPU temperature (NVIDIA nvidia-smi or AMD amdgpu sysfs)
    for i, g in enumerate(gpu_list):
        temp = None
        ns = g.get("nvidia_smi")
        amd = g.get("amd_live") or {}
        if ns and not ns.get("_error") and ns.get("temperature_gpu"):
            temp = _temperature_value(ns["temperature_gpu"])
        elif amd.get("temperature_gpu") is not None:
            raw = amd["temperature_gpu"]
            temp = raw if isinstance(raw, int) else _temperature_value(str(raw))
        name = (
            (ns or {}).get("name")
            or (g.get("specs") or {}).get("model")
            or g.get("lspci_name")
            or g.get("bus_id", "")
        )
        cid = f"gpu_temp_{i + 1}"
        if temp is not None:
            if temp > 85:
                checks.append(_check(
                    cid, "thermal", "warn",
                    f"GPU {i + 1} temperature: {temp}°C (high)",
                    evidence={"gpu": name, "temp_c": temp, "bus_id": g.get("bus_id")},
                    remedy="Improve cooling / airflow; check fan curve and case intake.",
                ))
            else:
                checks.append(_check(
                    cid, "thermal", "pass",
                    f"GPU {i + 1} temperature: {temp}°C",
                    evidence={"gpu": name, "temp_c": temp, "bus_id": g.get("bus_id")},
                ))
        else:
            vendor = (g.get("vendor") or "").lower().replace("0x", "")
            if vendor == "1002" or amd:
                na_msg = f"GPU {i + 1} temperature: N/A (amdgpu sysfs temp unread)"
                na_remedy = "Confirm amdgpu is loaded (lsdriver); live fields → lsgpu."
            elif vendor == "10de" or (ns and not ns.get("_error")):
                na_msg = f"GPU {i + 1} temperature: N/A (nvidia-smi for live temp)"
                na_remedy = (
                    "Fix NVIDIA driver/NVML (reboot after driver align) so nvidia-smi "
                    "reports temperature."
                )
            else:
                na_msg = f"GPU {i + 1} temperature: N/A (no live sensor)"
                na_remedy = "Live temp → lsgpu (NVIDIA nvidia-smi or AMD sysfs)."
            checks.append(_check(
                cid, "thermal", "na",
                na_msg,
                evidence={"gpu": name, "bus_id": g.get("bus_id")},
                remedy=na_remedy,
            ))

    # PSU sizing — design budget (spec CPU/GPU + class estimates for DRAM/storage/NIC/fans/platform)
    psu_watts = budget.get("psu_watts")
    if psu_watts is None:
        psu_watts = _psu_watts_from_config()
    psu_ev = {
        "estimated_budget_w": estimated_tdp,
        "estimated_tdp_w": estimated_tdp,  # alias: full budget, not CPU+GPU only
        "spec_w": budget.get("spec_w"),
        "est_w": budget.get("est_w"),
        "psu_watts": psu_watts,
        "cpu_tdp_w": cpu_tdp,
        "gpu_tdp_w": gpu_tdp,
        "budget_lines": budget.get("lines"),
        "dimm_count": budget.get("dimm_count"),
        "fan_count": budget.get("fan_count"),
    }
    if psu_watts is not None:
        pct = (100.0 * estimated_tdp / psu_watts) if psu_watts else 0
        if pct > 80:
            checks.append(_check(
                "psu_headroom", "power", "warn",
                f"PSU: {estimated_tdp} W design budget "
                f"({budget.get('spec_w', 0)} W spec + {budget.get('est_w', 0)} W est) "
                f"vs {psu_watts} W PSU — may be undersized ({pct:.0f}%)",
                evidence={**psu_ev, "pct_of_psu": round(pct, 1)},
                remedy="Use a higher-wattage PSU or reduce load; confirm psu_watts / platform_watts in ~/.lsstack.conf.",
            ))
        else:
            checks.append(_check(
                "psu_headroom", "power", "pass",
                f"PSU: {estimated_tdp} W design budget "
                f"({budget.get('spec_w', 0)} W spec + {budget.get('est_w', 0)} W est) "
                f"vs {psu_watts} W PSU — OK",
                evidence={**psu_ev, "pct_of_psu": round(pct, 1)},
            ))
    else:
        checks.append(_check(
            "psu_headroom", "power", "na",
            f"PSU: Design budget {estimated_tdp} W "
            f"({budget.get('spec_w', 0)} W spec + {budget.get('est_w', 0)} W est); "
            f"set psu_watts in ~/.lsstack.conf to check headroom",
            evidence=psu_ev,
            remedy="Add psu_watts=N to ~/.lsstack.conf.",
        ))

    # NUMA
    numa = cpu_info.get("sockets", "1")
    has_nvidia = any(
        (g.get("nvidia_smi") or {}) and not (g.get("nvidia_smi") or {}).get("_error")
        for g in gpu_list
    )
    if has_nvidia:
        numa_msg = f"NUMA: {numa} socket(s) — nvidia-smi topo -m for GPU affinity"
        numa_remedy = "On multi-socket systems run nvidia-smi topo -m and pin jobs to local GPUs."
    else:
        numa_msg = f"NUMA: {numa} socket(s) — pin jobs to local CPUs/devices (lstopology)"
        numa_remedy = (
            "On multi-socket systems check node locality with lscpu -e / lstopology "
            "and pin jobs to local devices."
        )
    checks.append(_check(
        "numa_sockets", "topology", "na",
        numa_msg,
        evidence={"sockets": numa},
        remedy=numa_remedy,
    ))

    # ECC — system DRAM (EDAC) + GPU (nvidia-smi)
    edac = memory_mod.get_edac_info()
    if not edac.get("available"):
        checks.append(_check(
            "ecc_system", "reliability", "na",
            "System ECC: no EDAC memory controllers (desktop/non-ECC RAM, or EDAC not loaded)",
            evidence=edac,
            remedy="Server/workstation boards with ECC DIMMs expose /sys/devices/system/edac/mc/mc*.",
        ))
    else:
        for mc in edac.get("controllers") or []:
            mid = mc.get("id") or "mc"
            ue = mc.get("ue_count")
            ce = mc.get("ce_count")
            if ue is not None and ue > 0:
                checks.append(_check(
                    f"ecc_uncorrected_{mid}", "reliability", "fail",
                    f"System ECC: {mid} uncorrected={ue} corrected={ce}",
                    evidence=mc,
                    remedy="Uncorrected DRAM ECC errors indicate failing DIMMs or board issues; schedule hardware check.",
                ))
            elif ce is not None and ce > 0:
                checks.append(_check(
                    f"ecc_corrected_{mid}", "reliability", "warn",
                    f"System ECC: {mid} corrected={ce} uncorrected={ue if ue is not None else '—'}",
                    evidence=mc,
                    remedy="Rising corrected ECC counts can precede DIMM failure; monitor and reseat/replace if climbing.",
                ))
            else:
                checks.append(_check(
                    f"ecc_system_{mid}", "reliability", "pass",
                    f"System ECC: {mid} clean (CE={ce if ce is not None else '—'}, UE={ue if ue is not None else '—'})",
                    evidence=mc,
                ))

    nvidia_smi = system_binary("nvidia-smi")
    try:
        if not nvidia_smi:
            raise FileNotFoundError("nvidia-smi not found")
        out = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=ecc.mode.current,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            for i, line in enumerate(out.stdout.strip().splitlines()):
                parts = [p.strip() for p in line.split(",")]
                mode = parts[0] if parts else ""
                ce_g = parts[1] if len(parts) > 1 else ""
                ue_g = parts[2] if len(parts) > 2 else ""
                ev = {"mode": mode, "corrected": ce_g, "uncorrected": ue_g, "gpu_index": i + 1}
                na_vals = {"N/A", "[N/A]", "", "Disabled", "Unknown"}
                if mode in ("Disabled", "N/A", "[N/A]", "Unknown", "") and ue_g in na_vals:
                    checks.append(_check(
                        f"ecc_gpu_{i + 1}", "reliability", "na",
                        f"GPU {i + 1} ECC: {mode or 'N/A'} (consumer GPUs often lack ECC)",
                        evidence=ev,
                    ))
                elif ue_g not in ("0", "N/A", "[N/A]", ""):
                    checks.append(_check(
                        f"ecc_uncorrected_gpu_{i + 1}", "reliability", "warn",
                        f"GPU {i + 1} ECC uncorrected: {ue_g} (mode={mode})",
                        evidence=ev,
                        remedy="Investigate GPU memory; non-zero uncorrected ECC is a hardware concern.",
                    ))
                elif ce_g not in ("0", "N/A", "[N/A]", "") and ce_g.isdigit() and int(ce_g) > 0:
                    checks.append(_check(
                        f"ecc_corrected_gpu_{i + 1}", "reliability", "warn",
                        f"GPU {i + 1} ECC corrected: {ce_g} (mode={mode})",
                        evidence=ev,
                        remedy="Monitor corrected GPU ECC; rising counts can mean failing VRAM.",
                    ))
                else:
                    checks.append(_check(
                        f"ecc_gpu_{i + 1}", "reliability", "pass",
                        f"GPU {i + 1} ECC: mode={mode}, CE={ce_g}, UE={ue_g}",
                        evidence=ev,
                    ))
        else:
            checks.append(_check(
                "ecc_gpu", "reliability", "na",
                "GPU ECC: N/A (nvidia-smi query failed or no GPUs)",
            ))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks.append(_check(
            "ecc_gpu", "reliability", "na",
            "GPU ECC: N/A (nvidia-smi not available)",
        ))

    # Driver / CUDA
    driver = None
    cuda = None
    if gpu_list:
        ns0 = gpu_list[0].get("nvidia_smi") or {}
        if not ns0.get("_error"):
            driver = ns0.get("driver_version")
            cuda = ns0.get("cuda_version")
    if driver:
        msg = f"Driver: {driver}"
        if cuda:
            msg += f" — CUDA {cuda}"
        else:
            msg += " — CUDA version see nvidia-smi or nvcc --version"
        checks.append(_check(
            "driver_cuda", "software", "pass",
            msg,
            evidence={"driver_version": driver, "cuda_version": cuda},
        ))
    else:
        checks.append(_check(
            "driver_cuda", "software", "na",
            "Driver/CUDA: N/A (no NVIDIA driver or nvidia-smi)",
            remedy="Install/align NVIDIA driver; see lsgpu Live / mismatch guidance.",
        ))

    checks.extend(_rebar_checks(gpu_list))
    checks.extend(_pcie_validity_checks())

    dependencies = deps_mod.probe_dependencies()

    from lsstack.expansion import compute_expansion

    topo_info = topology_mod.get_topology_info()
    dmi_slots = slots_mod._type9_slots()
    populated = slots_mod.get_populated_endpoints()
    expansion = compute_expansion(
        budget=budget,
        topology=topo_info,
        dmi_slots=dmi_slots,
        populated=populated,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "lsstack",
        "version": __version__,
        "host": {
            "hostname": identity["hostname"],
            "fqdn": identity["fqdn"],
            "uname": identity["uname"],
        },
        "identity": identity,
        "components": components,
        "interfaces": interfaces,
        "dependencies": {
            "missing": dependencies.get("missing") or [],
            "apt_packages": dependencies.get("apt_packages") or [],
            "apt_install_command": dependencies.get("apt_install_command"),
            "vendor_hints": dependencies.get("vendor_hints") or [],
            "items": dependencies.get("items") or [],
        },
        "summary": {
            "system_ram_gib": round(system_ram_gib, 2),
            "total_vram_gib": round(total_vram_gib, 2),
            "estimated_tdp_w": estimated_tdp,
            "estimated_budget_w": estimated_tdp,
            "budget_spec_w": budget.get("spec_w"),
            "budget_est_w": budget.get("est_w"),
            "psu_watts": budget.get("psu_watts"),
            "expansion_free_w": (expansion.get("power") or {}).get("free_w"),
            "expansion_free_lanes": (expansion.get("lanes") or {}).get("free_lanes_sum"),
            "expansion_open_slots": (expansion.get("slots") or {}).get("open_count"),
            "gpu_count": len(gpu_list),
            "component_count": len(components),
            "interface_count": len(interfaces),
            "check_counts": {
                "pass": sum(1 for c in checks if c["status"] == "pass"),
                "fail": sum(1 for c in checks if c["status"] == "fail"),
                "warn": sum(1 for c in checks if c["status"] == "warn"),
                "na": sum(1 for c in checks if c["status"] == "na"),
            },
        },
        "expansion": expansion,
        "checks": checks,
    }


def _split_balance_message(c: dict[str, Any]) -> tuple[str, str]:
    """Return (title, detail) for Balance / High-impact row layout."""
    msg = (c.get("message") or "").strip()
    status = (c.get("status") or "").lower()
    m = re.match(
        r"^(.*?):\s*(Pass|Fail|N/A|Warn|OK)\b\s*(.*)$",
        msg,
        re.I,
    )
    if m:
        title = m.group(1).strip()
        detail = (m.group(3) or "").strip()
        detail = detail.lstrip("—–- ").strip()
        if detail.startswith("(") and detail.endswith(")"):
            detail = detail[1:-1].strip()
        return title, detail or status
    # PCIe width/speed/gen (role bdf): keep BDF colons inside the title
    m = re.match(r"^(.*\)):\s*(.*)$", msg)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # PCIe root 0000:00:03.1: …
    m = re.match(
        r"^(PCIe root \d{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]):\s*(.*)$",
        msg,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if ":" in msg:
        title, detail = msg.split(":", 1)
        return title.strip(), detail.strip()
    return msg, status


def _print_balance(checks: list[dict[str, Any]], *, color: bool) -> None:
    from lsstack.color import looks_like_privilege_note, privilege_note, verdict_bracket

    rows: list[tuple[str, str, str, str | None]] = []
    footnote = ""
    for c in checks:
        if c.get("category") != "balance":
            continue
        title, detail = _split_balance_message(c)
        rows.append((title, c.get("status") or "na", detail, c.get("remedy")))
        if not footnote and c.get("id", "").startswith("ram_vram_"):
            footnote = ((c.get("evidence") or {}).get("footnote") or "").strip()
    if not rows:
        return
    w = max(18, *(len(t) for t, _, _, _ in rows))
    print("\n" + "=" * 60)
    print("  Balance")
    print("=" * 60)
    for title, status, detail, remedy in rows:
        st = verdict_bracket(status, enabled=color)
        shown = (
            privilege_note(detail, enabled=color)
            if looks_like_privilege_note(detail)
            else detail
        )
        print(f"  {title:>{w}}  {st}  {shown}")
        if status in ("na", "warn") and remedy:
            use_amber = looks_like_privilege_note(remedy) or (
                status == "na" and "apt install" in remedy.lower()
            )
            hint = privilege_note(remedy, enabled=color) if use_amber else remedy
            for wrapped in _wrap_words(f"→ {hint}", _NOTE_WRAP):
                print(f"  {'':>{w}}      {wrapped}")
    if footnote:
        print()
        for wrapped in _wrap_words(f"Note: {footnote}", _NOTE_WRAP):
            print(f"  {wrapped}")


def _print_human_from_report(report: dict[str, Any], *, color: bool = False) -> None:
    """Print Balance / Expansion / High-impact sections from structured report."""
    _print_balance(report.get("checks") or [], color=color)

    from lsstack.expansion import print_expansion_human

    print_expansion_human(report.get("expansion"), color=color)

    print("\n" + "=" * 60)
    print("  High-impact checks")
    print("=" * 60)
    _print_high_impact(report.get("checks") or [], color=color)

    sudo_note = sudo_full_results_note("lsstack")
    if sudo_note:
        from lsstack.color import privilege_note

        print()
        print(f"  {privilege_note(sudo_note, enabled=color)}")

    footer = deps_mod.missing_enricher_footer()
    if footer:
        print("\n" + "=" * 60)
        print("  Dependencies")
        print("=" * 60)
        print(f"  {footer}")


_NOTE_WRAP = 100
# Why/Prompt body width ≈ a long one-line check detail (e.g. ReBAR BAR note = 82 chars).
_TEACH_BODY_WRAP = 82


def _wrap_words(text: str, width: int = _NOTE_WRAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if width < 20:
        width = 20
    lines: list[str] = []
    words = text.split()
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


_STATUS_COL = 6  # visible width of [pass]/[fail]/[warn]


def _status_cell(status: str, *, color: bool) -> str:
    """Colored [status] left-padded/truncated to a fixed column width (ANSI-safe)."""
    from lsstack.color import verdict_bracket

    raw = f"[{(status or '?').strip()}]"
    token = verdict_bracket(status, enabled=color)
    return token + (" " * max(0, _STATUS_COL - len(raw)))


def _print_title_detail_block(label: str, text: str, *, title_w: int) -> None:
    """Print label in the title column; body spans status + detail columns."""
    text = (text or "").strip()
    if not text:
        return
    # "  {label:>w}  {body…}" — body starts where [status] would
    prefix_len = 2 + title_w + 2
    lines = _wrap_words(text, _TEACH_BODY_WRAP)
    if not lines:
        return
    print(f"  {label:>{title_w}}  {lines[0]}")
    cont = " " * prefix_len
    for line in lines[1:]:
        print(f"{cont}{line}")


def _print_high_impact(checks: list[dict[str, Any]], *, color: bool = False) -> None:
    """Print high-impact rows in Balance grammar: title [status] detail + Why/Prompt."""
    from lsstack.color import looks_like_privilege_note, privilege_note

    rows: list[dict[str, Any]] = []
    for c in checks:
        if c.get("category") == "balance":
            continue
        title, detail = _split_balance_message(c)
        rows.append(
            {
                "title": title,
                "status": (c.get("status") or "na").lower(),
                "detail": detail,
                "message": c.get("message") or "",
                "evidence": c.get("evidence") or {},
            }
        )
    if not rows:
        return
    w = max(22, *(len(r["title"]) for r in rows), len("Prompt"))
    seen_teach: set[tuple[str, str]] = set()
    seen_tables: set[str] = set()
    for r in rows:
        st = _status_cell(r["status"], color=color)
        detail = r["detail"]
        if looks_like_privilege_note(detail):
            detail = privilege_note(detail, enabled=color)
        print(f"  {r['title']:>{w}}  {st}  {detail}")
        if r["status"] not in ("warn", "fail"):
            continue
        ev = r["evidence"]
        expl = (ev.get("explanation") or "").strip()
        prompt = (ev.get("research_hint") or "").strip()
        teach_key = (expl, prompt)
        if teach_key != ("", "") and teach_key not in seen_teach:
            seen_teach.add(teach_key)
            if expl and expl not in r["message"] and expl != r["detail"]:
                _print_title_detail_block("Why", expl, title_w=w)
            if prompt:
                _print_title_detail_block("Prompt", prompt, title_w=w)
        table = ev.get("lane_table")
        if table:
            fp = json.dumps(table, sort_keys=True, default=str)
            if fp not in seen_tables:
                seen_tables.add(fp)
                blank = " " * _STATUS_COL
                print(f"  {'':>{w}}  {blank}  {'Item':<26} {'Lanes':>6} {'Gen':>5}")
                for row in table:
                    lanes = row.get("lanes")
                    ls = f"x{lanes}" if lanes is not None else "—"
                    gen = row.get("gen")
                    gs = f"Gen {gen}" if gen is not None else "—"
                    print(
                        f"  {'':>{w}}  {blank}  "
                        f"{str(row.get('item') or ''):<26} {ls:>6} {gs:>5}"
                    )


def _run_subsystem_outputs() -> None:
    """Aggregated ls* output (ensure bin scripts are executable)."""
    bin_dir = _bin_dir()
    if not os.path.isdir(bin_dir):
        print("lsstack: bin directory not found", file=sys.stderr)
        sys.exit(1)

    commands = ["lsproc", "lsgpu", "lsram", "lsslots", "lsdisk", "lsthermal", "lstopology"]
    notes_cmds = {"lsproc", "lsram", "lsslots", "lsdisk", "lsthermal", "lstopology"}
    for cmd in commands:
        path = os.path.join(bin_dir, cmd)
        if not os.path.isfile(path):
            continue
        if not os.access(path, os.X_OK):
            try:
                os.chmod(path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            except OSError:
                continue
        print(f"\n{'=' * 60}")
        print(f"  Output: {cmd}")
        print("=" * 60)
        argv = [path]
        if cmd in notes_cmds:
            argv.append("--no-notes")
        try:
            out = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if out.stdout:
                print(out.stdout)
            if out.stderr:
                print(out.stderr, file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("  (timed out)")
        except Exception as e:
            print(f"  (error: {e})")


def _prompt_yes(msg: str, *, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"{msg} [{d}]: ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes")


def _preflight_lab_and_enrichers(*, force_skip: bool = False) -> None:
    """First-run welcome when PSU is missing; otherwise stay quiet and report.

    Skipped for --json, non-TTY, or --no-preflight. If PSU watts are already
    on file for this host, returns immediately (full report, no prompts).
    """
    if force_skip:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return

    from lsstack.lab_inventory import resolve_lab_power

    lab = resolve_lab_power()
    if lab.get("psu_watts") is not None:
        return

    is_root = False
    try:
        is_root = os.geteuid() == 0
    except AttributeError:
        is_root = False

    print()
    print("Welcome to lsStack.")
    print()
    print(
        "This machine has no PSU catalogued yet. Cataloguing it improves "
        "power and headroom numbers."
    )
    if not is_root:
        print(
            "Running with sudo is not required, but the report will be more "
            "complete if you do."
        )
    print()

    if not _prompt_yes("Catalog PSU now?", default=False):
        print("OK — running the report. Power / headroom may stay N/A until you set a PSU.")
        print()
        return

    # Enroll creates the host + PSU; set-psu alone needs an enrolled host.
    if lab.get("enrolled"):
        from lsstack.lslab import cmd_set_psu

        code = cmd_set_psu([])
    else:
        from lsstack.lslab import cmd_enroll

        code = cmd_enroll([])

    if code not in (0, None):
        print(
            "PSU setup did not finish; continuing with the report anyway.",
            file=sys.stderr,
        )
    print()


def _host_slug() -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname().strip())
    slug = slug.strip("-.") or "host"
    return slug[:64]


def _default_output_path(*, json_mode: bool) -> str:
    ext = ".json" if json_mode else ".txt"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(default_reports_dir(), f"{_host_slug()}-{stamp}{ext}")


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while n <= 99:
        cand = f"{root}-{n}{ext}"
        if not os.path.exists(cand):
            return cand
        n += 1
    raise FileExistsError(path)


def _resolve_output_path(spec: str, *, json_mode: bool) -> str:
    """Empty spec → ~/.lsstack/reports/<host>-<stamp>. Explicit path must not exist."""
    if spec == "":
        path = _unique_path(_default_output_path(json_mode=json_mode))
    else:
        path = os.path.abspath(os.path.expanduser(spec))
        if os.path.exists(path):
            raise FileExistsError(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


class _StdoutTee:
    """TTY keeps ANSI; file gets strip_ansi. isatty follows the TTY."""

    def __init__(self, dest: TextIO, tty: TextIO) -> None:
        self._dest = dest
        self._tty = tty

    def write(self, s: str) -> int:
        self._tty.write(s)
        from lsstack.color import strip_ansi

        self._dest.write(strip_ansi(s))
        return len(s)

    def flush(self) -> None:
        self._tty.flush()
        self._dest.flush()

    def isatty(self) -> bool:
        try:
            return self._tty.isatty()
        except Exception:
            return False


def _help_text() -> str:
    return """lsstack — full system report for lab / workstation builds

What
  Balance (RAM:VRAM, DIMMs, slots), expansion headroom (power / lanes),
  high-impact checks (GPU temp, PSU, PCIe, ECC, driver), then aggregated
  ls* output (or --checks-only for the checks).

Why
  One place to see whether the box is balanced before adding a GPU, cloning
  a lab image, or chasing a PCIe/thermal surprise in the individual commands.

How
  Read Balance first, then Expansion, then High-impact (Why/Prompt on warns).
  Fields that need sudo are amber on that row (DMI DIMMs/slots, SMART).
  PCIe width is judged against the card, not empty root lanes → lstopology.
  Lean data → --json. First-run PSU prompt → --no-preflight to skip.
  Save a copy → -o (plain text; TTY can still be colored).

Usage: lsstack [options]
  --json           JSON (schema, host, summary, checks); no ls* dump
  --checks-only    Balance + Expansion + High-impact only
  -o, --output [FILE]
                   Write the report to FILE. Omit FILE for
                   ~/.lsstack/reports/<host>-<timestamp>.txt
                   (--json default uses .json). TTY keeps color; the file does not.
                   Existing FILE is an error.
  --deps           Probe enrichers; print apt/vendor hints
  --deps-install   apt-install missing enrichers (root; not NVIDIA/CUDA)
  --no-preflight   Skip interactive PSU welcome
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(_help_text())
        return
    ap = argparse.ArgumentParser(
        prog="lsstack",
        description="lsstack full system report (balance + high-impact + ls* output).",
        add_help=False,
    )
    ap.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"lsStack {__version__}",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (schema_version, host, summary, checks). Skips human ls* dump.",
    )
    ap.add_argument(
        "--checks-only",
        action="store_true",
        help="Human output: Balance + High-impact only (no aggregated ls* sections).",
    )
    ap.add_argument(
        "--deps",
        action="store_true",
        help="Probe external tools (full paths), print status and apt/vendor install hints; then exit.",
    )
    ap.add_argument(
        "--deps-install",
        action="store_true",
        help="Install missing apt-backed enrichers via apt (requires root). Does not install NVIDIA/CUDA.",
    )
    ap.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip interactive first-run PSU welcome (human mode).",
    )
    ap.add_argument(
        "--no-color",
        "-nc",
        action="store_true",
        help="Disable ANSI color on Balance status tokens.",
    )
    ap.add_argument(
        "-o",
        "--output",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="Write report to FILE (plain text). Omit FILE for ~/.lsstack/reports/<host>-<stamp>.txt",
    )
    args = ap.parse_args(argv)

    if args.output is not None and (args.deps or args.deps_install):
        print("lsstack: -o/--output cannot be used with --deps", file=sys.stderr)
        sys.exit(2)

    if args.deps or args.deps_install:
        deps = deps_mod.probe_dependencies()
        print(deps_mod.format_deps_report(deps))
        if args.deps_install:
            code = deps_mod.run_apt_install(deps)
            # Re-probe after install
            print(deps_mod.format_deps_report())
            sys.exit(code)
        sys.exit(0)

    if not args.json:
        _preflight_lab_and_enrichers(force_skip=args.no_preflight)

    out_path = None
    if args.output is not None:
        try:
            out_path = _resolve_output_path(args.output, json_mode=args.json)
        except FileExistsError as e:
            print(f"lsstack: file exists: {e}", file=sys.stderr)
            sys.exit(2)
        except OSError as e:
            print(f"lsstack: cannot write output: {e}", file=sys.stderr)
            sys.exit(2)

    report = collect_report()
    if args.json:
        text = json.dumps(report, indent=2)
        print(text)
        if out_path:
            try:
                with open(out_path, "x", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.write("\n")
            except OSError as e:
                print(f"lsstack: cannot write {out_path}: {e}", file=sys.stderr)
                sys.exit(2)
            print(f"saved {out_path}", file=sys.stderr)
        return

    from lsstack.color import color_enabled

    color_args = ["--no-color"] if args.no_color else []
    use_color = color_enabled(color_args)
    if out_path:
        try:
            fh = open(out_path, "x", encoding="utf-8")
        except OSError as e:
            print(f"lsstack: cannot write {out_path}: {e}", file=sys.stderr)
            sys.exit(2)
        old_out = sys.stdout
        sys.stdout = _StdoutTee(fh, old_out)
        try:
            _print_human_from_report(report, color=use_color)
            if not args.checks_only:
                _run_subsystem_outputs()
        finally:
            sys.stdout = old_out
            fh.close()
        print(f"saved {out_path}", file=sys.stderr)
        return

    _print_human_from_report(report, color=use_color)
    if not args.checks_only:
        _run_subsystem_outputs()


if __name__ == "__main__":
    main()
