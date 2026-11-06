"""lsvirt: hardware provision brief for KVM passthrough and K8s node capacity.

Does not install libvirt or Kubernetes. Does not bind VFIO or write domain XML.
Facts come from the same live sysfs / lab config the other ls* commands use.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack import __version__
from lsstack.color import color_enabled, green, red, yellow
from lsstack.common import dmi_or_none, read_dmi_id, read_file, system_binary, want_notes
from lsstack.disk import get_disk_info
from lsstack.gpu import get_gpu_info
from lsstack.iommu import get_iommu_info
from lsstack.lab_inventory import resolve_lab_power
from lsstack.memory import get_memory_info
from lsstack.network import get_nic_devices
from lsstack.proc import get_cpu_info
from lsstack.topology import get_numa_nodes

_PCI = "/sys/bus/pci/devices"
_HP_ROOT = "/sys/kernel/mm/hugepages"
SCHEMA_VERSION = 1
_KIB_PER_GIB = 1024 * 1024
_WHY_WRAP = 100
_KEEP_RAM_HEADLESS_MIN_GIB = 4
_KEEP_RAM_DESKTOP_MIN_GIB = 8
_KEEP_DISK_MIN_GIB = 32
_KEEP_DISK_PCT = 0.15
_GUEST_RAM_GPU_FLOOR_GIB = 8
_GUEST_RAM_FLOOR_GIB = 2
_GUEST_DISK_FLOOR_GIB = 32


def _int_file(path: str) -> int | None:
    raw = read_file(path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _pci_driver(bdf: str) -> str | None:
    link = os.path.join(_PCI, bdf, "driver")
    if not os.path.exists(link):
        return None
    try:
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return None


def _pci_numa(bdf: str) -> int | None:
    n = _int_file(os.path.join(_PCI, bdf, "numa_node"))
    if n is None or n < 0:
        return None
    return n


def _kvm_device() -> dict:
    path = "/dev/kvm"
    present = os.path.exists(path)
    return {
        "path": path,
        "present": present,
        "writable": bool(present and os.access(path, os.W_OK)),
    }


def _hugepages() -> dict:
    sizes: list[dict] = []
    if os.path.isdir(_HP_ROOT):
        try:
            names = os.listdir(_HP_ROOT)
        except OSError:
            names = []
        for name in sorted(names):
            if not name.startswith("hugepages-") or not name.endswith("kB"):
                continue
            try:
                kb = int(name[len("hugepages-"):-2])
            except ValueError:
                continue
            base = os.path.join(_HP_ROOT, name)
            if kb >= 1048576:
                label = f"{kb // 1048576} GiB"
            else:
                label = f"{kb // 1024} MiB"
            sizes.append({
                "size_kib": kb,
                "size_label": label,
                "nr": _int_file(os.path.join(base, "nr_hugepages")),
                "free": _int_file(os.path.join(base, "free_hugepages")),
            })
    return {"sizes": sizes}


def _gpu_label(g: dict) -> str:
    ns = g.get("nvidia_smi") or {}
    if ns.get("_error"):
        ns = {}
    return (
        ns.get("name")
        or (g.get("specs") or {}).get("model")
        or g.get("lspci_name")
        or g.get("bus_id")
        or "?"
    )


def _group_kind(group: dict | None) -> str:
    if not group:
        return "no_group"
    devices = group.get("devices") or []
    n = len(devices)
    gpu_n = sum(1 for d in devices if d.get("is_gpu"))
    audio_n = sum(1 for d in devices if d.get("is_audio"))
    nic_n = sum(
        1
        for d in devices
        if d.get("class") is not None and (d["class"] & 0xFF0000) == 0x020000
    )
    if gpu_n == 1 and n == 1:
        return "gpu_alone"
    if gpu_n == 1 and n == 2 and audio_n == 1:
        return "gpu_audio"
    if gpu_n:
        return "gpu_shared"
    if nic_n == 1 and n == 1:
        return "nic_alone"
    if nic_n and nic_n == n:
        return "nic_only"
    return "other"


def _libvirt_pci(bdf: str) -> str:
    return "pci_" + bdf.replace(":", "_").replace(".", "_")


def _numa_record(node_id: int | None, nodes: list[dict]) -> dict | None:
    if node_id is None:
        return None
    want = f"node{node_id}"
    for n in nodes:
        if n.get("node") == want:
            return n
    return None


def _resolve_numa(bdf: str, nodes: list[dict]) -> tuple[int | None, dict | None]:
    """PCI numa_node, or the only node on a single-node box (sysfs often says -1)."""
    numa_id = _pci_numa(bdf) if bdf else None
    rec = _numa_record(numa_id, nodes)
    if rec is not None:
        return numa_id, rec
    if len(nodes) == 1:
        rec = nodes[0]
        name = rec.get("node") or ""
        if name.startswith("node") and name[4:].isdigit():
            return int(name[4:]), rec
    return None, None


def _bind_rows(group: dict) -> list[dict]:
    rows = []
    for d in group.get("devices") or []:
        bdf = d.get("bdf") or ""
        rows.append({
            "bdf": bdf,
            "class_label": d.get("class_label"),
            "is_gpu": bool(d.get("is_gpu")),
            "is_audio": bool(d.get("is_audio")),
            "driver": _pci_driver(bdf) if bdf else None,
            "libvirt": _libvirt_pci(bdf) if bdf else None,
        })
    return rows


def _gpu_targets(gpus: list[dict], groups: list[dict], nodes: list[dict]) -> list[dict]:
    by_bdf: dict[str, dict] = {}
    for g in groups:
        for d in g.get("devices") or []:
            if d.get("bdf"):
                by_bdf[d["bdf"]] = g
    out = []
    for i, gpu in enumerate(gpus):
        bdf = gpu.get("bus_id") or ""
        group = by_bdf.get(bdf)
        kind = _group_kind(group)
        numa_id, numa = _resolve_numa(bdf, nodes)
        software = bool(gpu.get("software_renderer"))
        clean = kind in ("gpu_alone", "gpu_audio") and not software
        bind = _bind_rows(group) if group else []
        out.append({
            "index": i,
            "bdf": bdf,
            "label": _gpu_label(gpu),
            "software_renderer": software,
            "kind": kind,
            "passthrough_ok": clean,
            "group_id": (group or {}).get("id"),
            "numa_node": numa_id,
            "numa_cpulist": (numa or {}).get("cpulist") or None,
            "bind": bind,
            "already_vfio": any(r.get("driver") == "vfio-pci" for r in bind),
        })
    return out


def _nic_targets(nics: list[dict], groups: list[dict]) -> list[dict]:
    by_bdf: dict[str, dict] = {}
    for g in groups:
        for d in g.get("devices") or []:
            if d.get("bdf"):
                by_bdf[d["bdf"]] = g
    out = []
    for nic in nics:
        bdf = nic.get("bdf") or ""
        group = by_bdf.get(bdf)
        kind = _group_kind(group)
        if kind not in ("nic_alone", "nic_only"):
            continue
        out.append({
            "bdf": bdf,
            "model": nic.get("model"),
            "ifaces": nic.get("ifaces") or [],
            "speed_mbps": nic.get("speed_mbps"),
            "driver": nic.get("driver"),
            "kind": kind,
            "group_id": (group or {}).get("id"),
            "bind": _bind_rows(group) if group else [],
        })
    return out


def _parse_int(value) -> int | None:
    if value is None:
        return None
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _kb_to_gib(kb: int | None) -> int | None:
    if kb is None or kb < 0:
        return None
    return kb // _KIB_PER_GIB


def _fmt_gib(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n} GiB"


def _block_bytes(device: str) -> int | None:
    raw = read_file(f"/sys/block/{device}/size")
    if raw and raw.strip().isdigit():
        return int(raw.strip()) * 512
    return None


def _parent_disk(devnode: str | None) -> str | None:
    """/dev/nvme0n1p2 → nvme0n1; /dev/sda1 → sda."""
    if not devnode:
        return None
    name = devnode.rsplit("/", 1)[-1]
    m = re.match(r"^(nvme\d+n\d+)", name)
    if m:
        return m.group(1)
    m = re.match(r"^(mmcblk\d+)", name)
    if m:
        return m.group(1)
    m = re.match(r"^([a-z]+)", name)
    return m.group(1) if m else name


def _root_fs() -> dict:
    out: dict = {"device": None, "total_kb": None, "avail_kb": None, "mount": "/"}
    exe = system_binary("df")
    if not exe:
        return out
    try:
        r = subprocess.run([exe, "-P", "/"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return out
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return out
    parts = lines[-1].split()
    if len(parts) >= 6:
        out["device"] = parts[0]
        try:
            out["total_kb"] = int(parts[1])
            out["avail_kb"] = int(parts[3])
        except ValueError:
            pass
        out["mount"] = parts[5]
    return out


def _disk_inventory(disks: list[dict], root: dict) -> list[dict]:
    boot_parent = _parent_disk(root.get("device"))
    rows = []
    for d in disks:
        device = d.get("device") or ""
        parts = d.get("partitions") or []
        mounts = [
            p.get("mount")
            for p in parts
            if p.get("mount") and p.get("mount") != "Not Mounted"
        ]
        is_boot = ("/" in mounts) or (boot_parent == device)
        size_b = _block_bytes(device) if device else None
        rows.append({
            "device": device,
            "model": d.get("model") if d.get("model") != "N/A" else None,
            "type": d.get("type"),
            "size": d.get("size"),
            "size_bytes": size_b,
            "is_boot": is_boot,
            "mounts": mounts,
            "passthrough_ok": bool(device) and not is_boot and not mounts,
        })
    return rows


def _physical_gpus(targets: list[dict]) -> list[dict]:
    return [t for t in targets if not t.get("software_renderer")]


def _host_keeps_gpu(targets: list[dict]) -> bool:
    """True when the host is expected to keep a physical GPU (desktop keep).

    IOMMU-clean is not "will pass". Passing every physical GPU leaves no
    display device; default keep assumes the host keeps one.
    """
    return bool(_physical_gpus(targets))


def _sketch_gpu_split(targets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Clean GPUs to bind vs the last display GPU held for the host."""
    real = _physical_gpus(targets)
    clean = [t for t in real if t.get("passthrough_ok")]
    if clean and len(clean) == len(real):
        return clean[:-1], [clean[-1]]
    return clean, []


def _resource_budget(mem: dict, targets: list[dict], disks: list[dict]) -> dict:
    """Host-keep vs guest/worker allotment. Named rule, not a guest-OS table."""
    total_kb = mem.get("total_kb")
    avail_kb = mem.get("available_kb")
    total_gib = _kb_to_gib(total_kb)
    desktop = _host_keeps_gpu(targets)
    if desktop:
        profile = "desktop"
        min_keep = _KEEP_RAM_DESKTOP_MIN_GIB
        frac = 4
        keep_rule = f"max({min_keep} GiB, 1/{frac} of total); host keeps a GPU"
    else:
        profile = "headless"
        min_keep = _KEEP_RAM_HEADLESS_MIN_GIB
        frac = 8
        keep_rule = f"max({min_keep} GiB, 1/{frac} of total); headless (no physical GPU)"
    keep_gib = None
    guest_gib = None
    if total_gib is not None:
        keep_gib = max(min_keep, total_gib // frac)
        guest_gib = max(0, total_gib - keep_gib)
    bind_for_floor, _held = _sketch_gpu_split(targets)
    ram_floor = _GUEST_RAM_GPU_FLOOR_GIB if bind_for_floor else _GUEST_RAM_FLOOR_GIB
    ram_ok = guest_gib is not None and guest_gib >= ram_floor

    root = _root_fs()
    disk_rows = _disk_inventory(disks, root)
    root_total = root.get("total_kb")
    root_avail = root.get("avail_kb")
    disk_keep_gib = None
    disk_guest_gib = None
    disk_below_keep = False
    if root_total:
        pct_kb = int(_KEEP_DISK_PCT * root_total)
        keep_kb = max(_KEEP_DISK_MIN_GIB * _KIB_PER_GIB, pct_kb)
        disk_keep_gib = keep_kb // _KIB_PER_GIB
        if root_avail is not None:
            disk_below_keep = root_avail < keep_kb
            leftover = root_avail - keep_kb
            disk_guest_gib = max(0, leftover // _KIB_PER_GIB)
    disk_ok = disk_guest_gib is not None and disk_guest_gib >= _GUEST_DISK_FLOOR_GIB
    pt = [r["device"] for r in disk_rows if r.get("passthrough_ok")]
    boot = [r["device"] for r in disk_rows if r.get("is_boot")]

    ram_why = (
        "Keep covers kernel, qemu (~1 GiB), ssh, and logs. QEMU overhead sits "
        "inside the keep, not on top of the guest. MemAvailable is page cache; "
        "it moves, so it is not the budget. Hugepages allocated for a guest "
        "come out of host RAM too."
    )
    if desktop:
        ram_why += (
            " A physical GPU on the host is treated as a desktop session; "
            "passing the last display GPU is opt-in, not the keep default."
        )
    else:
        ram_why += (
            " No physical GPU in inventory; smaller keep is for a headless node."
        )
    disk_why = (
        f"Keep max({_KEEP_DISK_MIN_GIB} GiB, {int(_KEEP_DISK_PCT * 100)}% of the "
        "filesystem that holds /) so logs, package cache, and qcow2 growth do "
        "not fill the boot disk. Never passthrough the boot disk: that yanks "
        "the host root. "
        f"{_GUEST_DISK_FLOOR_GIB} GiB is a Linux lab image floor, not a Windows size."
    )

    return {
        "ram": {
            "total_kb": total_kb,
            "available_kb": avail_kb,
            "total_human": mem.get("total_human"),
            "available_human": mem.get("available_human"),
            "total_gib": total_gib,
            "profile": profile,
            "keep_gib": keep_gib,
            "keep_rule": keep_rule,
            "guest_gib": guest_gib,
            "guest_floor_gib": ram_floor,
            "guest_ok": ram_ok,
            "why": ram_why,
        },
        "disk": {
            "root": root,
            "disks": disk_rows,
            "boot_devices": boot,
            "passthrough_candidates": pt,
            "keep_gib": disk_keep_gib,
            "keep_rule": (
                f"max({_KEEP_DISK_MIN_GIB} GiB, {int(_KEEP_DISK_PCT * 100)}% of /)"
            ),
            "guest_gib": disk_guest_gib,
            "guest_floor_gib": _GUEST_DISK_FLOOR_GIB,
            "guest_ok": disk_ok,
            "below_keep": disk_below_keep,
            "why": disk_why,
        },
    }


def _virt_install_sketch(
    targets: list[dict], cpu: dict, budget: dict, kvm: dict | None = None
) -> str | None:
    ram = (budget or {}).get("ram") or {}
    disk = (budget or {}).get("disk") or {}
    if not (kvm or {}).get("writable"):
        return None
    if not ram.get("guest_ok"):
        return None
    bind_targets, held = _sketch_gpu_split(targets)
    cpus = _parse_int(cpu.get("cpus")) or 4
    vcpus = min(8, max(2, cpus // 2 if cpus >= 4 else cpus))
    guest_gib = ram.get("guest_gib") or 8
    mem_mib = guest_gib * 1024
    hostdevs = []
    binds = []
    for t in bind_targets:
        for row in t.get("bind") or []:
            if row.get("bdf"):
                binds.append(row["bdf"])
            if row.get("libvirt"):
                hostdevs.append(f"  --host-device {row['libvirt']} \\")
    bind_line = " ".join(binds)
    disk_bits: list[str] = []
    if disk.get("guest_ok") and disk.get("guest_gib"):
        disk_bits.append(f"  --disk size={disk['guest_gib']} \\")
    pt = disk.get("passthrough_candidates") or []
    lines = [
        "# Not executed. Review IOMMU groups before binding.",
    ]
    if bind_line:
        lines.append(f"# Bind to vfio-pci: {bind_line}")
        lines.append("# Drop --host-device lines you are not passing through.")
    if held:
        held_bd = " ".join(t.get("bdf") or "?" for t in held)
        lines.append(f"# Display GPU held for the host (not auto-bound): {held_bd}")
        lines.append(
            "# Add --host-device only if another GPU or AST/IPMI stays on the host."
        )
    lines.append(
        f"# Memory is guest allotment ({guest_gib} GiB = total − host keep), not half of RAM."
    )
    if pt:
        lines.append(
            "# Disk passthrough (not the boot disk): "
            + " ".join(f"/dev/{d}" for d in pt)
        )
    if disk.get("below_keep"):
        lines.append("# Host / is already below the keep floor; do not add a qcow2 there.")
    lines.extend([
        "virt-install --name guest \\",
        "  --cpu host-passthrough \\",
        f"  --vcpus {vcpus} --memory {mem_mib} \\",
    ])
    lines.extend(disk_bits)
    lines.extend(hostdevs)
    if lines[-1].endswith(" \\"):
        lines[-1] = lines[-1][:-2]
    return "\n".join(lines)


def _k8s_role(targets: list[dict]) -> str:
    real = [t for t in targets if not t.get("software_renderer")]
    return "gpu_worker" if real else "general_worker"


def _k8s_sharing(targets: list[dict]) -> str:
    real = [t for t in targets if not t.get("software_renderer")]
    if not real:
        return "no GPU; general node capacity only"
    clean = sum(1 for t in real if t.get("passthrough_ok"))
    dirty = len(real) - clean
    bits = []
    if clean:
        bits.append(
            f"{clean} IOMMU-clean GPU(s) can PCI-passthrough (see KVM)"
        )
    if dirty:
        bits.append(
            f"{dirty} shared-group GPU(s) stay on the host "
            "(device plugin / time-slice, not VFIO)"
        )
    return "; ".join(bits)


def get_virt_info() -> dict:
    """Bundle live + lab facts for a hardware provision brief."""
    dmi = read_dmi_id(
        "sys_vendor", "product_name", "board_vendor", "board_name",
    )
    cpu = get_cpu_info()
    mem = get_memory_info()
    gpus, _ = get_gpu_info()
    iommu = get_iommu_info()
    nodes = get_numa_nodes()
    nics = get_nic_devices()
    lab = resolve_lab_power()
    groups = iommu.get("groups") or []
    targets = _gpu_targets(gpus, groups, nodes)
    nic_pt = _nic_targets(nics, groups)
    kvm = _kvm_device()
    hugepages = _hugepages()
    disks = get_disk_info()
    budget = _resource_budget(mem, targets, disks)
    sketch = _virt_install_sketch(targets, cpu, budget, kvm)

    psu = lab.get("psu") or {}
    lab_brief = {
        "enrolled": bool(lab.get("enrolled")),
        "build": lab.get("build"),
        "psu_watts": lab.get("psu_watts"),
        "psu_model": psu.get("model") if isinstance(psu, dict) else None,
        "psu_source": lab.get("psu_source"),
    }

    real_gpus = [t for t in targets if not t.get("software_renderer")]
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "lsvirt",
        "version": __version__,
        "host": {
            "sys_vendor": dmi_or_none(dmi.get("sys_vendor")),
            "product_name": dmi_or_none(dmi.get("product_name")),
            "board_vendor": dmi_or_none(dmi.get("board_vendor")),
            "board_name": dmi_or_none(dmi.get("board_name")),
            "cpu_model": cpu.get("model_name"),
            "cpus": cpu.get("cpus"),
            "sockets": cpu.get("sockets"),
            "cores_per_socket": cpu.get("cores_per_socket"),
            "virtualization": cpu.get("virtualization"),
            "ram": mem.get("total_human"),
            "ram_kb": mem.get("total_kb"),
            "ram_available": mem.get("available_human"),
            "ram_available_kb": mem.get("available_kb"),
            "numa": nodes,
        },
        "resources": budget,
        "kvm": {
            "device": kvm,
            "iommu_enabled": bool(iommu.get("enabled")),
            "iommu_groups": iommu.get("group_count") or 0,
            "cmdline_flags": iommu.get("cmdline_flags") or {},
            "gpus": targets,
            "nic_passthrough": nic_pt,
            "hugepages": hugepages,
            "virt_install_sketch": sketch,
        },
        "k8s": {
            "role": _k8s_role(targets),
            "control_plane": False,
            "gpu_count": len(real_gpus),
            "passthrough_clean": sum(1 for t in real_gpus if t.get("passthrough_ok")),
            "shared_group": sum(1 for t in real_gpus if not t.get("passthrough_ok")),
            "sharing": _k8s_sharing(targets),
            "hugepages": hugepages,
            "allocatable_ram_gib": (budget.get("ram") or {}).get("guest_gib"),
            "allocatable_disk_gib": (budget.get("disk") or {}).get("guest_gib"),
            "nics": [
                {
                    "bdf": n.get("bdf"),
                    "model": n.get("model"),
                    "ifaces": n.get("ifaces") or [],
                    "speed_mbps": n.get("speed_mbps"),
                    "driver": n.get("driver"),
                }
                for n in nics
            ],
        },
        "lab": lab_brief,
    }


def _wrap_words(text: str, width: int) -> list[str]:
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


def _kind_display(kind: str, *, color: bool) -> str:
    if kind == "gpu_alone":
        return green("GPU alone, VFIO OK", enabled=color)
    if kind == "gpu_audio":
        return yellow("GPU + audio, usually OK", enabled=color)
    if kind == "gpu_shared":
        return red("shared group, do not bind whole group", enabled=color)
    if kind == "no_group":
        return yellow("not in an IOMMU group", enabled=color)
    if kind == "nic_alone":
        return green("NIC alone, VFIO OK", enabled=color)
    if kind == "nic_only":
        return yellow("NIC functions only", enabled=color)
    return kind


def _virt_display(cpu: dict, kvm: dict, *, color: bool) -> str:
    virt = (cpu.get("virtualization") or "").strip()
    if virt and virt.upper() != "N/A":
        cpu_bit = virt
    else:
        cpu_bit = "lscpu saw no VT-x/AMD-V"
    if kvm.get("writable"):
        kvm_bit = "/dev/kvm writable"
        text = f"{cpu_bit}; {kvm_bit}"
        return green(text, enabled=color) if virt and virt.upper() != "N/A" else yellow(text, enabled=color)
    if kvm.get("present"):
        return yellow(f"{cpu_bit}; /dev/kvm present, not writable", enabled=color)
    return yellow(f"{cpu_bit}; /dev/kvm missing", enabled=color)


def _hp_line(hp: dict) -> str:
    sizes = hp.get("sizes") or []
    if not sizes:
        return "none advertised in sysfs"
    parts = []
    for s in sizes:
        nr = s.get("nr")
        free = s.get("free")
        parts.append(f"{s.get('size_label')}: {nr or 0} allocated ({free or 0} free)")
    return "; ".join(parts)


def _role_line(info: dict) -> str:
    k8s = info.get("k8s") or {}
    role = k8s.get("role")
    if role == "gpu_worker":
        return "GPU worker candidate (not a control-plane recommendation)"
    return "general node candidate (not a control-plane recommendation)"


def _ram_keep_line(ram: dict, *, color: bool) -> str:
    keep = ram.get("keep_gib")
    rule = ram.get("keep_rule") or ""
    text = f"{_fmt_gib(keep)}"
    if rule:
        text = f"{text}  ({rule})"
    return text


def _ram_guest_line(ram: dict, *, color: bool) -> str:
    guest = ram.get("guest_gib")
    floor = ram.get("guest_floor_gib")
    text = f"{_fmt_gib(guest)}  (total − keep; {floor} GiB floor for this role)"
    if ram.get("guest_ok"):
        return green(text, enabled=color)
    return yellow(text, enabled=color)


def print_virt_human(info: dict | None = None, *, color: bool = False, notes: bool = True) -> None:
    info = info if info is not None else get_virt_info()
    host = info.get("host") or {}
    kvm = info.get("kvm") or {}
    k8s = info.get("k8s") or {}
    lab = info.get("lab") or {}
    budget = info.get("resources") or {}
    ram_b = budget.get("ram") or {}
    disk_b = budget.get("disk") or {}
    targets = kvm.get("gpus") or []
    nic_pt = kvm.get("nic_passthrough") or []
    sketch = kvm.get("virt_install_sketch")

    labels = [
        "Product", "Board", "CPU", "RAM", "Available", "Keep RAM", "Guest RAM",
        "Virt", "Role", "IOMMU", "Groups",
        "GPU", "Bind", "NUMA", "NIC PT", "Hugepages", "Note", "Why", "Sharing",
        "NICs", "PSU", "Build", "Sketch", "Keep disk", "Guest disk",
        "Allocatable", "Disks",
    ]
    for t in targets:
        labels.append(t.get("label") or "")
        labels.append(t.get("bdf") or "")
    for d in disk_b.get("disks") or []:
        labels.append(d.get("device") or "")
    w = _label_width(*labels)

    print("\nHardware provision")

    _section("Host")
    product = " ".join(p for p in (host.get("sys_vendor"), host.get("product_name")) if p) or "—"
    board = " ".join(p for p in (host.get("board_vendor"), host.get("board_name")) if p) or "—"
    _kv("Product", product, w)
    _kv("Board", board, w)
    cpu_bits = [host.get("cpu_model") or "—"]
    if host.get("cpus") and str(host.get("cpus")).upper() != "N/A":
        cpu_bits.append(f"{host.get('cpus')} CPUs")
    _kv("CPU", ", ".join(cpu_bits), w)
    _kv("RAM", ram_b.get("total_human") or host.get("ram") or "—", w)
    avail = ram_b.get("available_human")
    if avail:
        _kv("Available", f"{avail}  (page cache; not the budget)", w)
    _kv("Keep RAM", _ram_keep_line(ram_b, color=color), w)
    _kv("Guest RAM", _ram_guest_line(ram_b, color=color), w)
    _kv("Why", ram_b.get("why"), w, wrap=_WHY_WRAP)
    _kv("Virt", _virt_display(host, kvm.get("device") or {}, color=color), w)
    _kv("Role", _role_line(info), w)

    _section("Disks")
    disk_rows = disk_b.get("disks") or []
    if not disk_rows:
        _kv("Note", "no block devices (type=disk) found", w)
    else:
        for d in disk_rows:
            bits = [d.get("size") or "—"]
            if d.get("model"):
                bits.append(d["model"])
            if d.get("is_boot"):
                bits.append("boot (/)")
                root_avail = (disk_b.get("root") or {}).get("avail_kb")
                if root_avail is not None:
                    bits.append(f"{_fmt_gib(_kb_to_gib(root_avail))} free")
            elif d.get("mounts"):
                bits.append("mounted " + ",".join(d["mounts"]))
            elif d.get("passthrough_ok"):
                bits.append("unmounted, passthrough candidate")
            else:
                bits.append("in use")
            _kv(d.get("device") or "?", "  ".join(bits), w)
    keep_disk = disk_b.get("keep_gib")
    keep_txt = f"leave {_fmt_gib(keep_disk)} free on /  ({disk_b.get('keep_rule')})"
    if disk_b.get("below_keep"):
        keep_txt = yellow(keep_txt + "  host already below keep", enabled=color)
    _kv("Keep disk", keep_txt, w)
    gdisk = disk_b.get("guest_gib")
    gfloor = disk_b.get("guest_floor_gib")
    if disk_b.get("below_keep"):
        guest_txt = yellow(
            "0 GiB file-backed on /  (host free is below keep)",
            enabled=color,
        )
    elif disk_b.get("guest_ok"):
        guest_txt = green(
            f"{_fmt_gib(gdisk)} file-backed on /  (free − keep; {gfloor} GiB Linux lab floor)",
            enabled=color,
        )
    else:
        guest_txt = yellow(
            f"{_fmt_gib(gdisk)} file-backed on /  (below {gfloor} GiB Linux lab floor)",
            enabled=color,
        )
    _kv("Guest disk", guest_txt, w)
    pt = disk_b.get("passthrough_candidates") or []
    if pt:
        _kv(
            "Passthrough",
            "  ".join(f"/dev/{x}" for x in pt) + "  (not the boot disk)",
            w,
        )
    else:
        _kv("Passthrough", "none (boot disk is never a candidate)", w)
    _kv("Why", disk_b.get("why"), w, wrap=_WHY_WRAP)

    _section("KVM")
    if kvm.get("iommu_enabled"):
        iommu_s = green("active", enabled=color)
    else:
        iommu_s = yellow("not detected (passthrough blocked)", enabled=color)
    flags = kvm.get("cmdline_flags") or {}
    if flags:
        iommu_s = f"{iommu_s}; " + " ".join(f"{k}={v}" for k, v in flags.items())
    _kv("IOMMU", iommu_s, w)
    _kv("Groups", str(kvm.get("iommu_groups") or 0), w)

    if not targets:
        _kv("GPU", "none found", w)
    else:
        for t in targets:
            tag = f"GPU {t.get('index', 0)}"
            loc = t.get("bdf") or "?"
            if t.get("group_id") is not None:
                loc = f"{loc}  group {t['group_id']}"
            if t.get("software_renderer"):
                verdict = yellow("software renderer / VM, not a physical GPU", enabled=color)
            else:
                verdict = _kind_display(t.get("kind") or "other", color=color)
            extra = t.get("label") or ""
            _kv(tag, f"{loc}  {extra}  {verdict}", w)
            bind = t.get("bind") or []
            if bind:
                bits = []
                for row in bind:
                    piece = row.get("bdf") or "?"
                    lab_c = row.get("class_label")
                    drv = row.get("driver") or "unbound"
                    if lab_c:
                        piece = f"{piece} {lab_c}"
                    piece = f"{piece} ({drv})"
                    bits.append(piece)
                _kv("Bind", "  ".join(bits), w)
            if t.get("numa_node") is not None:
                cpus = t.get("numa_cpulist") or "—"
                _kv("NUMA", f"node {t['numa_node']}  cpus {cpus}", w)
            elif t.get("bdf"):
                _kv("NUMA", "unknown", w)

    if nic_pt:
        for n in nic_pt:
            ifaces = ",".join(n.get("ifaces") or []) or "no netdev"
            _kv(
                "NIC PT",
                f"{n.get('bdf')}  group {n.get('group_id')}  {n.get('model') or 'NIC'}  "
                f"{ifaces}  {_kind_display(n.get('kind') or '', color=color)}",
                w,
            )

    _kv("Hugepages", _hp_line(kvm.get("hugepages") or {}), w)
    hp_g = ram_b.get("guest_gib") or 16
    _kv(
        "Note",
        f"hugepage counts are not applied; a {hp_g} GiB guest on 1 GiB pages needs {hp_g} pages",
        w,
    )

    real_gpus = [t for t in targets if not t.get("software_renderer")]
    if real_gpus and all(t.get("passthrough_ok") for t in real_gpus):
        _kv(
            "Note",
            "last physical GPU is held for the host; passing it needs AST/IPMI or another GPU",
            w,
        )

    kvm_dev = kvm.get("device") or {}
    if sketch:
        _section("Sketch")
        print()
        for line in sketch.splitlines():
            print(f"    {line}")
    else:
        _section("Sketch")
        if not kvm_dev.get("writable"):
            if kvm_dev.get("present"):
                _kv(
                    "Note",
                    "no sketch; /dev/kvm present but not writable (add this user to group kvm)",
                    w,
                )
            else:
                _kv(
                    "Note",
                    "no sketch; /dev/kvm missing (enable SVM/VT-x, then load kvm)",
                    w,
                )
        elif ram_b and not ram_b.get("guest_ok"):
            _kv(
                "Note",
                f"no sketch; guest RAM {_fmt_gib(ram_b.get('guest_gib'))} is below the "
                f"{ram_b.get('guest_floor_gib')} GiB floor",
                w,
            )
        elif targets and not kvm.get("iommu_enabled"):
            _kv(
                "Note",
                "no sketch; turn on VT-d / AMD-Vi in firmware (see lsiommu)",
                w,
            )
        else:
            _kv("Note", "no sketch", w)

    _section("K8s")
    _kv("Role", _role_line(info), w)
    _kv(
        "GPUs",
        f"{k8s.get('gpu_count') or 0}  "
        f"(passthrough-clean: {k8s.get('passthrough_clean') or 0}, "
        f"shared-group: {k8s.get('shared_group') or 0})",
        w,
    )
    _kv("Sharing", k8s.get("sharing"), w)
    alloc_ram = k8s.get("allocatable_ram_gib")
    alloc_disk = k8s.get("allocatable_disk_gib")
    _kv(
        "Allocatable",
        f"RAM {_fmt_gib(alloc_ram)}  disk {_fmt_gib(alloc_disk)} file-backed  "
        "(same keep as KVM; node capacity, not kube-reserved flags)",
        w,
    )
    _kv("Hugepages", "same as KVM (optional for VMs in pods)", w)
    nics = k8s.get("nics") or []
    if not nics:
        _kv("NICs", "none", w)
    else:
        for n in nics:
            ifaces = ",".join(n.get("ifaces") or []) or "no netdev"
            speed = f"{n['speed_mbps']} Mbps" if n.get("speed_mbps") else "speed n/a"
            _kv(
                "NICs",
                f"{n.get('bdf') or '?'}  {n.get('model') or 'NIC'}  {ifaces}  {speed}",
                w,
            )
    _kv(
        "Why",
        "kubelet, containerd, and eviction sit in the host keep. Remaining RAM and "
        "/ free space are what a GPU worker can actually give pods. This is not a "
        "cluster install.",
        w,
        wrap=_WHY_WRAP,
    )

    _section("Lab")
    if lab.get("psu_watts") is not None:
        model = lab.get("psu_model")
        src = lab.get("psu_source")
        extra = []
        if model:
            extra.append(str(model))
        if src:
            extra.append(f"from {src}")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        _kv("PSU", f"{lab['psu_watts']} W{suffix}", w)
    else:
        _kv("PSU", "not catalogued (lslab enroll)", w)
    if lab.get("build"):
        _kv("Build", str(lab["build"]), w)
    elif not lab.get("enrolled"):
        _kv("Note", "this host is not in the lab book yet", w)

    if notes:
        _section("Notes")
        _kv(
            "Note",
            "Keep RAM is for the host. Guest RAM is what to put in virt-install --memory "
            "(or kubelet allocatable). Do not budget from MemAvailable.",
            w,
            wrap=_WHY_WRAP,
        )
        _kv(
            "Note",
            "Keep disk is free space that must stay on /. File-backed guests consume "
            "the leftover; passthrough is a spare whole disk only.",
            w,
            wrap=_WHY_WRAP,
        )
        _kv(
            "Note",
            "Hugepage pages you allocate for a guest disappear from host RAM; "
            "count them inside the keep.",
            w,
            wrap=_WHY_WRAP,
        )
        _kv("Note", "IOMMU groups → lsiommu; disks → lsdisk; GPU inventory → lsgpu; NUMA / PCIe → lstopology", w)
        _kv("Note", "does not bind VFIO, write XML, or install kubeadm", w)
        _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def _help_text() -> str:
    return """lsvirt — hardware provision brief for KVM and K8s (lab / workstation)

What
  Reports whether this box can isolate GPUs (and some NICs) for a VM, RAM and
  disk to keep on the host vs allot to a guest, and the same facts as
  Kubernetes node capacity (not a cluster install).

Why
  Builders isolating a GPU for a VM, or sizing a GPU worker, need IOMMU riders,
  NUMA, and a host-keep vs guest split in one place. Installing libvirt or
  kubeadm does not answer that.

How
  Read Host first (RAM/disk keep vs guest). Then KVM: Alone = VFIO OK;
  GPU+audio = usually OK; shared = do not bind the whole group. Sketch needs a
  writable /dev/kvm, does not auto-bind the last display GPU, comments only,
  never executed. K8s is this box as a node. Lean data → --json.

Usage: lsvirt [options]
  --json, -j       JSON
  --no-notes       Hide the Notes section
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_virt_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_virt_human(
        info,
        color=color_enabled(args),
        notes=want_notes(args),
    )


if __name__ == "__main__":
    main()
