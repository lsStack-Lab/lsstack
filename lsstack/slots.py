"""lsslots: board PCIe connector inventory — DMI Type 9 + populated endpoints by role.

Lane budgets and gen mismatch detail stay in lstopology. Drive/NIC health stay in
lsnvme / lsdisk / lsnetwork. SATA is not a PCIe slot (pointer only).
"""

from __future__ import annotations

import json
import os
import re
import sys

from lsstack.color import color_enabled, privilege_note
from lsstack.common import load_json, lookup_gpu_specs, read_file, sudo_full_results_note

_SYS_PCI = "/sys/bus/pci/devices"

_DETAIL_CMD = {
    "gpu": "lsgpu",
    "nvme": "lsnvme",
    "storage": "lsdisk",
    "nic": "lsnetwork",
    "wifi": "lsnetwork",
}


def _type9_slots() -> list[dict]:
    """DMI Type 9 system slots via first-party SMBIOS reader."""
    from lsstack.dmi import system_slots

    return system_slots()


def _board_product() -> str:
    return (read_file("/sys/class/dmi/id/board_name") or "").strip()


def _int_sysfs(path: str) -> int | None:
    raw = read_file(path)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _str_sysfs(path: str) -> str | None:
    raw = (read_file(path) or "").strip()
    return raw or None


def _class_int(bdf: str) -> int | None:
    raw = read_file(os.path.join(_SYS_PCI, bdf, "class"))
    if not raw:
        return None
    try:
        return int(raw.strip(), 0)
    except ValueError:
        return None


def _role_for_class(cls: int) -> str | None:
    base = (cls >> 16) & 0xFF
    sub = (cls >> 8) & 0xFF
    if base in (0x06, 0x04):  # bridge, multimedia (GPU HD audio)
        return None
    if base == 0x03:
        return "gpu"
    if base == 0x02:
        return "nic"
    if base == 0x0D:
        return "wifi"
    if base == 0x01:
        if sub == 0x08:
            return "nvme"
        return "storage"
    return None


def _upstream_bridge_bdf(device_bdf: str) -> str:
    path = os.path.join(_SYS_PCI, device_bdf)
    if not os.path.exists(path):
        return ""
    real = os.path.realpath(path)
    parent = os.path.basename(os.path.dirname(real))
    if re.match(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", parent):
        return parent
    return ""


def _lookup_max_lanes(by_board: dict, board_product: str, designation: str) -> int | None:
    for bid in (board_product, ""):
        if bid in by_board and designation in by_board[bid]:
            return by_board[bid][designation]
    return None


def _gpu_spec(bdf: str, gpu_json: dict | None) -> tuple[int | None, str | None]:
    vendor_hex = read_file(os.path.join(_SYS_PCI, bdf, "vendor")) or ""
    device_hex = read_file(os.path.join(_SYS_PCI, bdf, "device")) or ""
    entry = lookup_gpu_specs(gpu_json, vendor_hex, device_hex)
    if not entry:
        return None, None
    return entry.get("pcie_lanes"), entry.get("model")


def get_populated_endpoints() -> list[dict]:
    """Populated PCIe endpoints of interest (gpu/nvme/storage/nic/wifi) with link fields."""
    board_product = _board_product()
    dmi_slots = _type9_slots()
    addr_to_des = {
        (s.get("bus_address") or "").strip(): s.get("designation") or ""
        for s in dmi_slots
        if s.get("bus_address")
    }
    mobo = load_json("mobo_slots.json")
    by_board = (mobo or {}).get("by_board", {})
    gpu_json = load_json("gpu_pci_id.json")

    if not os.path.isdir(_SYS_PCI):
        return []

    try:
        names = os.listdir(_SYS_PCI)
    except OSError:
        return []

    result: list[dict] = []
    for bdf in sorted(names):
        cls = _class_int(bdf)
        if cls is None:
            continue
        role = _role_for_class(cls)
        if not role:
            continue
        base = os.path.join(_SYS_PCI, bdf)
        cur_w = _int_sysfs(os.path.join(base, "current_link_width"))
        max_w = _int_sysfs(os.path.join(base, "max_link_width"))
        cur_s = _str_sysfs(os.path.join(base, "current_link_speed"))
        max_s = _str_sysfs(os.path.join(base, "max_link_speed"))
        if cur_w is None and max_w is None:
            continue
        bridge = _upstream_bridge_bdf(bdf)
        designation = addr_to_des.get(bridge, "") or addr_to_des.get(bdf, "") or None
        max_lanes = _lookup_max_lanes(by_board, board_product, designation) if designation else None
        pcie_lanes, gpu_model = (None, None)
        if role == "gpu":
            pcie_lanes, gpu_model = _gpu_spec(bdf, gpu_json)
        warn_undersized = bool(
            pcie_lanes is not None and max_lanes is not None and pcie_lanes > max_lanes
        )
        result.append({
            "bdf": bdf,
            "role": role,
            "bridge_bdf": bridge or None,
            "designation": designation,
            "board_product": board_product or None,
            "max_lanes": max_lanes,
            "current_width": cur_w,
            "max_width": max_w,
            "current_speed": cur_s,
            "max_speed": max_s,
            "pcie_lanes": pcie_lanes,
            "gpu_model": gpu_model,
            "warn_undersized": warn_undersized,
            "detail_cmd": _DETAIL_CMD.get(role),
        })
    return result


def get_slot_info() -> list[dict]:
    """GPU entries only — used by report PCIe slot balance checks."""
    return [e for e in get_populated_endpoints() if e.get("role") == "gpu"]


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _link_summary(ep: dict) -> str:
    parts = []
    cur = ep.get("current_width")
    if cur is not None:
        parts.append(f"x{cur}")
    spd = ep.get("current_speed")
    if spd:
        # shorten "2.5 GT/s PCIe" → keep as-is for clarity
        parts.append(spd.replace(" PCIe", ""))
    return "  ".join(parts) if parts else "link N/A"


def _help_text() -> str:
    return """lsslots — board PCIe connectors and populated endpoints

What
  Reports DMI Type 9 slot names (empty and occupied) plus live GPU/NVMe/storage/
  NIC/Wi-Fi endpoints from sysfs (BDF, link width/speed, catalog slot map).

Why
  Builders need to know which physical connector a card is in, and whether a
  GPU is sitting in a lane-starved slot, before chasing BIOS or cable myths.

How
  Populated endpoints work without root. Physical slot names need sudo for DMI
  Type 9 (amber). Lane budgets / gen mismatch → lstopology. SATA is not PCIe
  (see lsdisk). Lean data → --json.

Usage: lsslots [options]
  --json, -j       JSON (dmi_slots + populated)
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.

    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    from lsstack.dmi import probe_table

    board = _board_product()
    dmi_slots = _type9_slots()
    dmi_probe = probe_table()
    endpoints = get_populated_endpoints()

    if "--json" in args or "-j" in args:
        print(json.dumps({
            "board_product": board or None,
            "dmi_slots": dmi_slots,
            "dmi_type9_reason": None if dmi_probe.get("ok") else dmi_probe.get("reason"),
            "populated": endpoints,
        }, indent=2))
        return

    # One label width for Board / Physical / Populated / Details
    label_w = 14
    for s in dmi_slots:
        label_w = max(label_w, len(s.get("designation") or ""))
    for ep in endpoints:
        label_w = max(label_w, len(ep.get("role") or ""), len(ep.get("bdf") or ""))

    color = color_enabled(args)
    print("\nPCIe slots / connectors")
    _section("Board")
    _kv("Product", board or "N/A", label_w)

    _section("Physical (DMI Type 9)")
    if dmi_slots:
        for s in dmi_slots:
            des = s.get("designation") or "?"
            usage = (s.get("usage") or "").strip()
            addr = (s.get("bus_address") or "").strip()
            typ = (s.get("type") or "").strip()
            bits = []
            if usage:
                bits.append(usage)
            if typ:
                bits.append(typ)
            if addr:
                bits.append(addr)
            _kv(des, "  ".join(bits) if bits else "-", label_w)
    elif dmi_probe.get("reason") == "needs_sudo":
        _kv(
            "Note",
            privilege_note("— (needs sudo for DMI Type 9)", enabled=color),
            label_w,
        )
    else:
        _kv("Note", "no Type 9 slots in DMI (firmware omitted them)", label_w)

    _section("Populated")
    if not endpoints:
        _kv("Note", "no GPU/NVMe/storage/NIC/Wi‑Fi PCIe endpoints found", label_w)
    else:
        for ep in endpoints:
            role = ep["role"]
            parts = [ep["bdf"], _link_summary(ep)]
            if ep.get("designation"):
                parts.append(ep["designation"])
            elif role == "gpu":
                parts.append("slot unknown")
            if ep.get("gpu_model"):
                parts.append(ep["gpu_model"])
            if ep.get("detail_cmd"):
                parts.append(f"→ {ep['detail_cmd']}")
            _kv(role, "  ".join(p for p in parts if p), label_w)

        for ep in endpoints:
            if ep.get("role") != "gpu":
                continue
            cur = ep.get("current_width")
            rec = ep.get("pcie_lanes")
            max_l = ep.get("max_lanes")
            cur_spd = ep.get("current_speed") or ""
            max_spd = ep.get("max_speed") or ""
            if ep.get("warn_undersized"):
                _kv("Note", f"{rec}-lane GPU in {max_l}-lane slot — may limit bandwidth", label_w)
            elif rec is not None and cur is not None and cur < int(rec):
                _kv("Note", f"{ep['bdf']} running x{cur}, GPU expects x{rec} — see lstopology", label_w)
            if cur_spd and max_spd and cur_spd != max_spd:
                _kv("Note", f"{ep['bdf']} link speed below device max — see lsgpu / lstopology", label_w)

    if endpoints:
        _section("Details")
        for ep in endpoints:
            _kv(
                ep["bdf"],
                f"bridge {ep.get('bridge_bdf') or 'N/A'}; "
                f"max width x{ep['max_width'] if ep.get('max_width') is not None else '?'}; "
                f"max speed {ep.get('max_speed') or 'N/A'}",
                label_w,
            )
    _section("Notes")
    sudo_note = sudo_full_results_note("lsslots")
    if sudo_note:
        _kv("Note", privilege_note(sudo_note, enabled=color), label_w)
    _kv("Note", "lane budget / gen mismatch → lstopology", label_w)
    _kv("Note", "SATA / drive health → lsdisk; not listed as PCIe slots", label_w)
    print()


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else [])
