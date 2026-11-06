"""lsnvme: NVMe controllers and namespaces from sysfs (no smartctl required)."""

from __future__ import annotations

import json
import os
import re
import sys

from lsstack.color import color_enabled, green, red, yellow
from lsstack.common import read_file

_NVME = "/sys/class/nvme"
# Lab first-look when the drive publishes no temp1_max / temp1_crit.
# Same NVMe band as lsthermal; never the CPU/GPU 80/90.
_NVME_TEMP_YELLOW_C = 70
_NVME_TEMP_RED_C = 80


def _milli_c(raw: str | None) -> int | None:
    if not raw:
        return None
    s = raw.strip()
    if not s.lstrip("-").isdigit():
        return None
    return int(s) // 1000


def _pci_link(ctrl_path: str) -> dict:
    from lsstack.dmi import normalize_bdf

    cur = os.path.realpath(os.path.join(ctrl_path, "device"))
    pci_bdf = normalize_bdf(os.path.basename(cur))
    link_speed = None
    link_width = None
    max_speed = None
    max_width = None
    walk = cur
    for _ in range(6):
        speed = read_file(os.path.join(walk, "current_link_speed"))
        width = read_file(os.path.join(walk, "current_link_width"))
        if speed or width:
            link_speed = (speed or "").strip() or None
            link_width = (width or "").strip() or None
            max_speed = (read_file(os.path.join(walk, "max_link_speed")) or "").strip() or None
            max_width = (read_file(os.path.join(walk, "max_link_width")) or "").strip() or None
            break
        parent = os.path.dirname(walk)
        if parent == walk:
            break
        walk = parent
    out: dict = {}
    if pci_bdf:
        out["pci_bdf"] = pci_bdf
    if link_speed or link_width:
        out["link_speed"] = link_speed
        out["link_width"] = link_width
    if max_speed or max_width:
        out["max_speed"] = max_speed
        out["max_width"] = max_width
    return out


def _hwmon_temp(ctrl: str) -> dict:
    """Composite temp plus the drive's own max/crit when hwmon publishes them."""
    base = os.path.join(_NVME, ctrl)
    candidates = []
    h0 = os.path.join(base, "hwmon0")
    if os.path.isdir(h0):
        candidates.append(h0)
    dh = os.path.join(base, "device", "hwmon")
    if os.path.isdir(dh):
        try:
            for name in sorted(os.listdir(dh)):
                candidates.append(os.path.join(dh, name))
        except OSError:
            pass
    picked: dict | None = None
    for hdir in candidates:
        try:
            names = os.listdir(hdir)
        except OSError:
            continue
        inputs = sorted(n for n in names if re.match(r"temp\d+_input$", n))
        for inp in inputs:
            prefix = inp[: -len("_input")]
            temp_c = _milli_c(read_file(os.path.join(hdir, inp)))
            if temp_c is None:
                continue
            label = (read_file(os.path.join(hdir, prefix + "_label")) or "").strip()
            row = {
                "temp_c": temp_c,
                "temp_max_c": _milli_c(read_file(os.path.join(hdir, prefix + "_max"))),
                "temp_crit_c": _milli_c(read_file(os.path.join(hdir, prefix + "_crit"))),
                "temp_label": label or None,
            }
            if (label or "").lower() == "composite":
                return row
            if picked is None:
                picked = row
    return picked or {}


def _namespaces(ctrl: str) -> list[dict]:
    """Namespaces under controller (nvme0n1, …)."""
    out: list[dict] = []
    base = os.path.join(_NVME, ctrl)
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in sorted(names):
        if not re.match(r"nvme\d+n\d+$", name):
            continue
        size_path = f"/sys/block/{name}/size"
        sectors = read_file(size_path)
        bytes_val = int(sectors) * 512 if sectors and sectors.isdigit() else 0
        out.append({
            "name": name,
            "size_bytes": bytes_val,
            "size_gib": round(bytes_val / (1024 ** 3), 2) if bytes_val else 0,
        })
    return out


def get_nvme_info() -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(_NVME):
        return out
    for ctrl in sorted(os.listdir(_NVME)):
        if not re.match(r"nvme\d+$", ctrl):
            continue
        base = os.path.join(_NVME, ctrl)
        link = _pci_link(base)
        hw = _hwmon_temp(ctrl)
        out.append({
            "controller": ctrl,
            "model": (read_file(os.path.join(base, "model")) or "").strip() or None,
            "serial": (read_file(os.path.join(base, "serial")) or "").strip() or None,
            "firmware": (read_file(os.path.join(base, "firmware_rev")) or "").strip() or None,
            "state": (read_file(os.path.join(base, "state")) or "").strip() or None,
            "temp_c": hw.get("temp_c"),
            "temp_max_c": hw.get("temp_max_c"),
            "temp_crit_c": hw.get("temp_crit_c"),
            "temp_label": hw.get("temp_label"),
            "pcie_link_speed": link.get("link_speed"),
            "pcie_link_width": link.get("link_width"),
            "pcie_max_speed": link.get("max_speed"),
            "pcie_max_width": link.get("max_width"),
            "pci_bdf": link.get("pci_bdf"),
            "namespaces": _namespaces(ctrl),
        })
    return out


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _state_display(state: str | None, *, color: bool) -> str:
    s = (state or "unknown").strip() or "unknown"
    low = s.lower()
    if low == "live":
        return green(s, enabled=color)
    if low in ("dead", "deleting", "new"):
        return red(s, enabled=color)
    if low in ("connecting", "resetting", "reconnecting"):
        return yellow(s, enabled=color)
    return s


def _temp_display(d: dict, *, color: bool) -> str | None:
    temp_c = d.get("temp_c")
    if temp_c is None:
        return None
    text = f"{temp_c}°C"
    yellow_at = d.get("temp_max_c")
    red_at = d.get("temp_crit_c")
    if yellow_at is None and red_at is None:
        yellow_at, red_at = _NVME_TEMP_YELLOW_C, _NVME_TEMP_RED_C
    else:
        if yellow_at is None:
            yellow_at = _NVME_TEMP_YELLOW_C
        if red_at is None:
            red_at = _NVME_TEMP_RED_C
    if temp_c >= red_at:
        return red(text, enabled=color)
    if temp_c >= yellow_at:
        return yellow(text, enabled=color)
    return text


def _width_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().lstrip("x")
    if s.isdigit():
        return int(s)
    return None


def _pcie_link_line(d: dict) -> str | None:
    speed = d.get("pcie_link_speed")
    width = d.get("pcie_link_width")
    max_speed = d.get("pcie_max_speed")
    max_width = d.get("pcie_max_width")
    if not speed and not width and not max_speed and not max_width:
        return None
    w = f"x{width}" if width and not str(width).startswith("x") else (width or "—")
    bits = [f"{w}, {speed or '—'}"]
    max_bits = []
    if max_width:
        mw = f"x{max_width}" if not str(max_width).startswith("x") else str(max_width)
        max_bits.append(mw)
    if max_speed:
        max_bits.append(max_speed.replace(" PCIe", ""))
    if max_bits:
        bits.append(f"(max {' '.join(max_bits)})")
    return " ".join(bits)


def _link_below_max(d: dict) -> bool:
    cur_w = _width_int(d.get("pcie_link_width"))
    max_w = _width_int(d.get("pcie_max_width"))
    if cur_w is not None and max_w is not None and cur_w < max_w:
        return True
    cur_s = d.get("pcie_link_speed") or ""
    max_s = d.get("pcie_max_speed") or ""
    return bool(cur_s and max_s and cur_s != max_s)


def _help_text() -> str:
    return """lsnvme — NVMe controllers and namespaces for lab / workstation builds

What
  Reports NVMe controllers from sysfs: model, serial, firmware, state,
  temperature, PCI BDF, PCIe link width/speed, and namespace sizes (GiB).

Why
  Builders need to confirm the boot/data SSD is the expected drive, sitting on
  a full-width PCIe path, and not thermally or link-limited — without requiring
  smartctl for a first look.

How
  Run on the host; one block per controller. SMART / partitions → lsdisk.
  PCIe path and idle Gen caveats → lstopology. Lean machine output → --json.

Usage: lsnvme [options]
  --json, -j       JSON (controllers + namespaces)
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def get_nvme_bundle() -> dict:
    devices = get_nvme_info()
    total_gib = 0.0
    for d in devices:
        for ns in d.get("namespaces") or []:
            total_gib += float(ns.get("size_gib") or 0)
    return {
        "devices": devices,
        "controller_count": len(devices),
        "total_namespace_gib": round(total_gib, 2),
    }


def print_nvme_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_nvme_bundle()
    devices = info.get("devices") or []

    labels = [
        "Note", "Controllers", "Capacity", "Controller", "Model", "Serial",
        "Firmware", "State", "Temperature", "PCI", "PCIe link", "Namespace",
    ]
    for d in devices:
        labels.append(d.get("controller") or "")
        for ns in d.get("namespaces") or []:
            labels.append(ns.get("name") or "")
    w = _label_width(*labels)

    print("\nNVMe")

    _section("Summary")
    if not devices:
        _kv("Note", "no controllers under /sys/class/nvme", w)
        print()
        return
    _kv("Controllers", str(info.get("controller_count") or len(devices)), w)
    _kv("Capacity", f"{info.get('total_namespace_gib')} GiB (namespaces)", w)

    for d in devices:
        _section(d.get("controller") or "nvme?")
        _kv("Model", d.get("model"), w)
        _kv("Serial", d.get("serial"), w)
        _kv("Firmware", d.get("firmware"), w)
        if d.get("state"):
            _kv("State", _state_display(d.get("state"), color=color), w)
        temp = _temp_display(d, color=color)
        if temp:
            _kv("Temperature", temp, w)
        if d.get("pci_bdf"):
            _kv("PCI", d["pci_bdf"], w)
        link = _pcie_link_line(d)
        if link:
            _kv("PCIe link", link, w)
        if _link_below_max(d):
            _kv("Note", f"{d.get('pci_bdf') or d.get('controller')} below device max — see lstopology", w)
        nss = d.get("namespaces") or []
        if not nss:
            _kv("Namespace", "none", w)
        for ns in nss:
            _kv("Namespace", f"{ns.get('name')} ({ns.get('size_gib')} GiB)", w)

    _section("Notes")
    _kv("Note", "SMART / partitions → lsdisk", w)
    _kv("Note", "PCIe path / idle Gen → lstopology", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.

    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_nvme_bundle()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_nvme_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
