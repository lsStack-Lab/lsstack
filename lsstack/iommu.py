"""lsiommu: IOMMU groups from sysfs (VFIO / GPU passthrough)."""

from __future__ import annotations

import json
import os
import sys

from lsstack.color import color_enabled, green, red, yellow
from lsstack.common import read_file

_IOMMU_GROUPS = "/sys/kernel/iommu_groups"
_PCI = "/sys/bus/pci/devices"

# Exact class, then subclass (prog-if masked). Names a builder can read in --all.
_CLASS_LABELS = {
    0x030000: "VGA",
    0x030200: "3D",
    0x040100: "Audio",
    0x040300: "Audio",
    0x020000: "Ethernet",
    0x010400: "RAID",
    0x010601: "SATA",
    0x010800: "NVMe",
    0x010802: "NVMe",
    0x060000: "Host bridge",
    0x060100: "ISA bridge",
    0x060400: "PCI bridge",
    0x0c0300: "USB",
    0x0c0500: "SMBus",
    0x108000: "Crypto",
    0x130000: "Coprocessor",
}


def _cmdline_iommu() -> dict:
    raw = read_file("/proc/cmdline") or ""
    flags = {}
    for token in raw.split():
        if token.startswith(("intel_iommu=", "amd_iommu=", "iommu=")):
            k, _, v = token.partition("=")
            flags[k] = v
    return {"cmdline": raw, "flags": flags}


def _iommu_hardware() -> list[str]:
    path = "/sys/class/iommu"
    if not os.path.isdir(path):
        return []
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _pci_class_int(bdf: str) -> int | None:
    raw = read_file(os.path.join(_PCI, bdf, "class"))
    if not raw:
        return None
    try:
        return int(raw.strip(), 16)
    except ValueError:
        return None


def _class_label(cls: int | None) -> str:
    if cls is None:
        return "?"
    if cls in _CLASS_LABELS:
        return _CLASS_LABELS[cls]
    subclass = cls & 0xFFFF00
    if subclass in _CLASS_LABELS:
        return _CLASS_LABELS[subclass]
    base = cls & 0xFF0000
    if base == 0x030000:
        return "Display"
    if base == 0x020000:
        return "Net"
    if base == 0x010000:
        return "Storage"
    if base == 0x040000:
        return "Multimedia"
    if base == 0x060000:
        return "Bridge"
    if base == 0x0c0000:
        return "Serial bus"
    if base == 0x100000:
        return "Crypto"
    if base == 0x120000:
        return "Accelerator"
    if base == 0x130000:
        return "Coprocessor"
    return f"class={cls:#08x}"


def _group_devices(group_id: str) -> list[dict]:
    base = os.path.join(_IOMMU_GROUPS, group_id, "devices")
    out: list[dict] = []
    if not os.path.isdir(base):
        return out
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for bdf in sorted(names):
        cls = _pci_class_int(bdf)
        vendor = read_file(os.path.join(_PCI, bdf, "vendor"))
        device = read_file(os.path.join(_PCI, bdf, "device"))
        out.append({
            "bdf": bdf,
            "class": cls,
            "class_label": _class_label(cls),
            "vendor": vendor,
            "device": device,
            "is_gpu": bool(cls is not None and (cls & 0xFF0000) == 0x030000),
            "is_audio": bool(cls is not None and (cls & 0xFFFF00) == 0x040300),
        })
    return out


def _passthrough_verdict(g: dict) -> tuple[str, str]:
    """Return (verdict, note) for a GPU-containing group."""
    n = g.get("device_count") or 0
    devices = g.get("devices") or []
    gpu_n = sum(1 for d in devices if d.get("is_gpu"))
    audio_n = sum(1 for d in devices if d.get("is_audio"))
    if n == 1 and gpu_n == 1:
        return ("alone", "GPU alone — good for VFIO")
    if n == 2 and gpu_n == 1 and audio_n == 1:
        return ("gpu_audio", "GPU + audio function only — usually OK")
    if gpu_n:
        return ("shared", "shared with other devices — ACS/passthrough may need care")
    return ("other", "no GPU")


def get_iommu_info() -> dict:
    """All IOMMU groups; gpu_groups highlights groups that contain a VGA/3D device."""
    hw = _iommu_hardware()
    cmd = _cmdline_iommu()
    groups: list[dict] = []
    if os.path.isdir(_IOMMU_GROUPS):
        try:
            gids = [g for g in os.listdir(_IOMMU_GROUPS) if g.isdigit()]
        except OSError:
            gids = []
        for gid in sorted(gids, key=int):
            devices = _group_devices(gid)
            rec = {
                "id": gid,
                "devices": devices,
                "has_gpu": any(d.get("is_gpu") for d in devices),
                "device_count": len(devices),
            }
            if rec["has_gpu"]:
                verdict, note = _passthrough_verdict(rec)
                rec["passthrough"] = verdict
                rec["passthrough_note"] = note
            groups.append(rec)
    gpu_groups = [g for g in groups if g.get("has_gpu")]
    enabled = bool(hw) or bool(groups)
    sizes: dict[int, int] = {}
    for g in groups:
        n = g["device_count"]
        sizes[n] = sizes.get(n, 0) + 1
    return {
        "enabled": enabled,
        "hardware": hw,
        "cmdline_flags": cmd["flags"],
        "group_count": len(groups),
        "groups": groups,
        "gpu_groups": gpu_groups,
        "size_mix": sizes,
    }


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _status_display(enabled: bool, *, color: bool) -> str:
    if enabled:
        return green("active", enabled=color)
    return yellow("not detected", enabled=color)


def _passthrough_display(verdict: str, note: str, *, color: bool) -> str:
    if verdict == "alone":
        return green(note, enabled=color)
    if verdict == "gpu_audio":
        return yellow(note, enabled=color)
    if verdict == "shared":
        return red(note, enabled=color)
    return note


def _help_text() -> str:
    return """lsiommu — IOMMU groups for VFIO / GPU passthrough (lab / workstation)

What
  Reports whether IOMMU is active, cmdline/firmware hints, group count, and
  which groups contain a GPU (passthrough candidates).

Why
  Home/lab builders isolating a GPU for a VM need the GPU (and usually its
  audio function) alone in an IOMMU group. Shared groups break clean VFIO.

How
  Read Summary, then GPU groups. Alone = best; GPU+audio = usually OK; shared =
  check ACS / motherboard. Full group list → --all. Topology → lstopology.
  Lean data → --json.

Usage: lsiommu [options]
  --json, -j       JSON (all groups)
  --all            List every IOMMU group (default: GPU-related only)
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def print_iommu_human(
    info: dict | None = None,
    *,
    color: bool = False,
    show_all: bool = False,
) -> None:
    info = info if info is not None else get_iommu_info()
    gpu_groups = info.get("gpu_groups") or []
    groups = info.get("groups") or []
    flags = info.get("cmdline_flags") or {}
    sizes = info.get("size_mix") or {}

    labels = [
        "Status", "Hardware", "Cmdline", "Groups", "GPU groups", "Size mix",
        "Note", "Group",
    ]
    for g in (groups if show_all else gpu_groups):
        labels.append(f"Group {g.get('id')}")
        for d in g.get("devices") or []:
            labels.append(d.get("bdf") or "")
    w = _label_width(*labels)

    print("\nIOMMU")

    _section("Summary")
    _kv("Status", _status_display(bool(info.get("enabled")), color=color), w)
    if info.get("hardware"):
        _kv("Hardware", ", ".join(info["hardware"]), w)
    else:
        _kv("Hardware", "—", w)
    if flags:
        _kv("Cmdline", " ".join(f"{k}={v}" for k, v in flags.items()), w)
    else:
        _kv(
            "Cmdline",
            "no intel_iommu=/amd_iommu= tokens (may still be on via firmware)",
            w,
        )
    _kv("Groups", str(info.get("group_count") or 0), w)
    _kv("GPU groups", str(len(gpu_groups)), w)
    if sizes:
        hist = ", ".join(f"{n}-dev×{c}" for n, c in sorted(sizes.items()))
        _kv("Size mix", hist, w)

    _section("GPU groups")
    if not gpu_groups:
        _kv("Note", "none (no VGA/3D in IOMMU groups)", w)
    else:
        for g in gpu_groups:
            verdict = g.get("passthrough") or _passthrough_verdict(g)[0]
            note = g.get("passthrough_note") or _passthrough_verdict(g)[1]
            _kv(
                f"Group {g['id']}",
                _passthrough_display(verdict, note, color=color),
                w,
            )
            for d in g.get("devices") or []:
                _kv(d["bdf"], d.get("class_label") or "?", w)

    if show_all:
        _section("All groups")
        others = [g for g in groups if not g.get("has_gpu")]
        if not others:
            _kv("Note", "only GPU groups (listed above)", w)
        else:
            for g in others:
                bits = ", ".join(
                    f"{d['bdf']} {d.get('class_label')}" for d in (g.get("devices") or [])
                )
                _kv(f"Group {g['id']}", bits or "—", w)

    _section("Notes")
    _kv("Note", "GPU inventory → lsgpu", w)
    _kv("Note", "PCIe path / NUMA → lstopology", w)
    if not show_all:
        _kv("Note", "list every group: lsiommu --all", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_iommu_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_iommu_human(
        info,
        color=color_enabled(args),
        show_all=("--all" in args),
    )


if __name__ == "__main__":
    main()
