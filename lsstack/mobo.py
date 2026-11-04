"""lsmobo: motherboard / chassis / DMI slots / onboard / ports.

Firmware deep-dive → lsbios. Populated PCIe connectors → lsslots.
Type 9 slots via first-party dmi.py. Baseboard/chassis/ports/onboard via
dmidecode when root; otherwise /sys/class/dmi/id (partial).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.color import color_enabled, privilege_note
from lsstack.common import (
    chassis_type_name,
    dmi_or_none,
    read_dmi_id,
    sudo_full_results_note,
    system_binary,
)


def _is_root() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False


def _run_dmidecode(*types: str) -> str:
    """Run dmidecode -t type… via full path; return stdout or empty string."""
    if not types:
        return ""
    exe = system_binary("dmidecode")
    if not exe:
        return ""
    try:
        cmd = [exe] + [x for t in types for x in ("-t", t)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _parse_section_filter(raw: str, section_name: str, keys: tuple[str, ...]) -> list[tuple[str, str]]:
    """Find first dmidecode block whose type line matches section_name."""
    blocks = re.split(r"\n(?=Handle 0x)", raw)
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        type_line = lines[1] if len(lines) > 1 else ""
        if section_name.lower() not in type_line.lower():
            continue
        found = []
        for line in lines[2:]:
            if ":" in line:
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if not key:
                    continue
                if not keys or key in keys:
                    found.append((key, val))
        return found
    return []


def _parse_bios(raw: str) -> list[tuple[str, str]]:
    keys = ("Vendor", "Version", "Release Date", "ROM Size", "BIOS Revision")
    return _parse_section_filter(raw, "BIOS Information", keys)


def _parse_baseboard(raw: str) -> list[tuple[str, str]]:
    keys = (
        "Manufacturer", "Product Name", "Version", "Serial Number",
        "Asset Tag", "Features", "Location In Chassis", "Type",
    )
    return _parse_section_filter(raw, "Base Board Information", keys)


def _parse_chassis(raw: str) -> list[tuple[str, str]]:
    keys = (
        "Manufacturer", "Type", "Version", "Serial Number", "Asset Tag",
        "Power Supply State", "Thermal State", "Security Status",
    )
    return _parse_section_filter(raw, "Chassis Information", keys)


def _parse_slots(raw: str) -> list[dict]:
    """Parse dmidecode -t slot into list of dicts."""
    slots = []
    current: dict = {}
    for line in raw.splitlines():
        if line.strip().startswith("Handle 0x"):
            if current and current.get("Designation") is not None:
                slots.append(current)
            current = {}
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "ID":
                current["ID"] = val
            elif key == "Designation":
                current["Designation"] = val
            elif key == "Current Usage":
                current["Usage"] = val
            elif key == "Length":
                current["Length"] = val
            elif key == "Type":
                current["Type"] = val
            elif key in ("Bus Address", "Bus address"):
                current["Bus Address"] = val
    if current and current.get("Designation") is not None:
        slots.append(current)
    return slots


def _parse_onboard(raw: str) -> list[dict]:
    keys = ("Reference Designation", "Type", "Status", "Type Instance")
    blocks = re.split(r"\n(?=Handle 0x)", raw)
    out: list[dict] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2 or "On Board" not in lines[1]:
            continue
        rec: dict = {}
        for line in lines[2:]:
            if ":" in line:
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if key in keys:
                    rec[key] = val
        if rec:
            out.append(rec)
    return out


def _parse_ports(raw: str) -> list[dict]:
    keys = (
        "Internal Reference Designator", "Internal Connector Type",
        "External Reference Designator", "External Connector Type", "Port Type",
    )
    blocks = re.split(r"\n(?=Handle 0x)", raw)
    out: list[dict] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2 or "Port Connector" not in lines[1]:
            continue
        rec: dict = {}
        for line in lines[2:]:
            if ":" in line:
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip()
                if key in keys:
                    rec[key] = val
        if rec:
            out.append(rec)
    return out


def _pairs_to_dict(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Drop OEM placeholder values; keep real inventory strings."""
    out: dict[str, str] = {}
    for k, v in pairs:
        clean = dmi_or_none(v)
        if clean is not None:
            out[k] = clean
    return out


def _clean_dict(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, str):
            clean = dmi_or_none(v)
            if clean is not None:
                out[k] = clean
        elif v is not None:
            out[k] = v
    return out


def _enrich_chassis(chassis: dict) -> dict:
    """Add Type label from SMBIOS code when Type is numeric."""
    chassis = dict(chassis or {})
    raw = chassis.get("Type")
    if raw is None:
        return chassis
    # Already a word (dmidecode sometimes expands)
    if isinstance(raw, str) and not raw.strip().isdigit():
        return chassis
    label = chassis_type_name(raw)
    if label:
        chassis["Type"] = label
        chassis["Type code"] = str(raw).strip()
    return chassis


def _sysfs_mobo_info() -> dict:
    """Partial board/BIOS/chassis from /sys/class/dmi/id."""
    dmi = read_dmi_id()
    bios = [(k, dmi[f]) for k, f in (
        ("Vendor", "bios_vendor"),
        ("Version", "bios_version"),
        ("Release Date", "bios_date"),
        ("BIOS Release", "bios_release"),
    ) if dmi.get(f)]
    baseboard = [(k, dmi[f]) for k, f in (
        ("Manufacturer", "board_vendor"),
        ("Product Name", "board_name"),
        ("Version", "board_version"),
        ("Serial Number", "board_serial"),
        ("Asset Tag", "board_asset_tag"),
    ) if dmi.get(f)]
    chassis = [(k, dmi[f]) for k, f in (
        ("Manufacturer", "chassis_vendor"),
        ("Type", "chassis_type"),
        ("Version", "chassis_version"),
        ("Serial Number", "chassis_serial"),
        ("Asset Tag", "chassis_asset_tag"),
    ) if dmi.get(f)]
    if dmi.get("sys_vendor") or dmi.get("product_name"):
        for k, f in (("System Manufacturer", "sys_vendor"), ("System Product", "product_name")):
            if dmi.get(f):
                baseboard.append((k, dmi[f]))
    return {
        "bios": _pairs_to_dict(bios),
        "baseboard": _pairs_to_dict(baseboard),
        "chassis": _pairs_to_dict(chassis),
        "slots": [],
        "onboard": [],
        "ports": [],
        "source": "sysfs",
        "needs_sudo": True,
    }


def _slots_from_dmi() -> list[dict]:
    """Type 9 slots via first-party reader, keyed like former dmidecode scrape."""
    from lsstack.dmi import system_slots

    out: list[dict] = []
    for s in system_slots():
        out.append(
            {
                "Designation": s.get("designation") or "",
                "Type": s.get("type") or "",
                "Usage": s.get("usage") or "",
                "Bus Address": s.get("bus_address") or "",
            }
        )
    return out


def get_mobo_info() -> dict:
    """Motherboard info: Python Type 9 slots; dmidecode for board/chassis/ports/onboard."""
    from lsstack.dmi import bios_extra, probe_table

    dmi_ok = bool(probe_table().get("ok"))
    slots = _slots_from_dmi() if dmi_ok else []

    if _is_root() and system_binary("dmidecode"):
        bios = _pairs_to_dict(_parse_bios(_run_dmidecode("bios")))
        # Prefer first-party Type 0 extras when present (same table, no text scrape).
        for k, v in bios_extra().items():
            if v and not bios.get(k):
                bios[k] = v
        info = {
            "bios": bios,
            "baseboard": _pairs_to_dict(_parse_baseboard(_run_dmidecode("baseboard"))),
            "chassis": _enrich_chassis(_pairs_to_dict(_parse_chassis(_run_dmidecode("chassis")))),
            "slots": slots if slots else _parse_slots(_run_dmidecode("slot")),
            "onboard": [_clean_dict(x) for x in _parse_onboard(_run_dmidecode("onboard"))],
            "ports": [_clean_dict(x) for x in _parse_ports(_run_dmidecode("port"))],
            "source": "hybrid" if slots else "dmidecode",
            "needs_sudo": False,
        }
        info["onboard"] = [x for x in info["onboard"] if x]
        info["ports"] = [x for x in info["ports"] if x]
        return info

    info = _sysfs_mobo_info()
    info["chassis"] = _enrich_chassis(info.get("chassis") or {})
    if slots:
        info["slots"] = slots
        info["source"] = "sysfs+dmi"
    # Ports/onboard/serials still need dmidecode when present.
    if not system_binary("dmidecode"):
        info["needs_sudo"] = True
    elif not _is_root():
        info["needs_sudo"] = True
    else:
        info["needs_sudo"] = False
    return info


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _slot_line(s: dict) -> str:
    typ = s.get("Type") or ""
    lanes = "—"
    if "PCI Express" in typ or "PCIe" in typ:
        m = re.search(r"x(\d+)", typ)
        lanes = f"x{m.group(1)}" if m else "—"
    bits = [
        s.get("Usage") or "—",
        s.get("Length") or "",
        lanes,
        typ[:40] if typ else "",
        s.get("Bus Address") or "",
    ]
    return "  ".join(b for b in bits if b)


def _help_text() -> str:
    return """lsmobo — motherboard / chassis / DMI inventory for lab builds

What
  Reports baseboard identity, chassis, DMI Type 9 slots, onboard devices, and
  system ports. Thin BIOS summary with pointer to lsbios.

Why
  Builders need board model/serial and which physical slots exist before seating
  GPUs or matching inventory — separate from firmware version (lsbios) and live
  populated endpoints (lsslots).

How
  Sysfs board/chassis without root. Type 9 slots need sudo (dmi.py).
  Ports/onboard/serials need sudo + dmidecode (optional enricher).
  Firmware detail → lsbios. Populated PCIe → lsslots. Lean data → --json.

Usage: lsmobo [options]
  --json, -j       JSON
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def print_mobo_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_mobo_info()
    bios = info.get("bios") or {}
    board = info.get("baseboard") or {}
    chassis = info.get("chassis") or {}
    slots = info.get("slots") or []
    onboard = info.get("onboard") or []
    ports = info.get("ports") or []

    labels = [
        "Note", "Vendor", "Version", "Release date", "Manufacturer", "Product",
        "Serial", "Asset Tag", "Type", "System", "Firmware",
    ]
    labels.extend(s.get("Designation") or "" for s in slots)
    w = _label_width(*labels, minimum=16)

    print("\nMotherboard")

    _section("Summary")
    product = dmi_or_none(board.get("Product Name")) or "—"
    mfg = dmi_or_none(board.get("Manufacturer")) or ""
    summary = f"{mfg} {product}".strip() if mfg and product != "—" else product
    _kv("Product", summary, w)
    if dmi_or_none(board.get("Version")):
        _kv("Version", board["Version"], w)
    if info.get("needs_sudo"):
        _kv(
            "Note",
            privilege_note(
                "ports/serials need sudo + dmidecode; slots need sudo for DMI",
                enabled=color,
            ),
            w,
        )
    _kv("Source", info.get("source"), w)

    _section("Firmware")
    if bios:
        ver = bios.get("Version") or "—"
        date = bios.get("Release Date") or bios.get("Release date") or ""
        line = ver + (f"  ({date})" if date else "")
        _kv("BIOS", f"{line}  → lsbios", w)
        if bios.get("Vendor"):
            _kv("Vendor", bios["Vendor"], w)
    else:
        _kv("Note", "BIOS → lsbios", w)

    _section("Board")
    if not board:
        _kv("Note", "no baseboard DMI", w)
    else:
        for key, label in (
            ("Manufacturer", "Manufacturer"),
            ("Product Name", "Product"),
            ("Version", "Version"),
            ("Serial Number", "Serial"),
            ("Asset Tag", "Asset Tag"),
            ("Type", "Type"),
            ("System Manufacturer", "System mfg"),
            ("System Product", "System product"),
        ):
            if dmi_or_none(board.get(key)):
                _kv(label, board[key], w)
        if info.get("needs_sudo") and not dmi_or_none(board.get("Serial Number")):
            _kv(
                "Serial",
                privilege_note("— (needs sudo + dmidecode for ports/serials)", enabled=color),
                w,
            )

    _section("Chassis")
    if not chassis:
        _kv("Note", "no chassis DMI", w)
    else:
        for key, label in (
            ("Manufacturer", "Manufacturer"),
            ("Type", "Type"),
            ("Type code", "Type code"),
            ("Version", "Version"),
            ("Serial Number", "Serial"),
            ("Asset Tag", "Asset Tag"),
            ("Power Supply State", "PSU state"),
            ("Thermal State", "Thermal"),
            ("Security Status", "Security"),
        ):
            if key == "Type code":
                continue  # keep in JSON; human shows decoded Type only
            if dmi_or_none(chassis.get(key)) or (key == "Type" and chassis.get(key)):
                val = chassis[key]
                # SMBIOS chassis status enums — not live sensors
                if key in ("Power Supply State", "Thermal State") and val:
                    val = (
                        f"{val} (SMBIOS chassis status, not live → "
                        f"{'lspower' if key == 'Power Supply State' else 'lsthermal'})"
                    )
                _kv(label, val, w)

    _section("DMI slots")
    if slots:
        for s in slots:
            des = s.get("Designation") or s.get("ID") or "?"
            _kv(des, _slot_line(s), w)
        _kv("Note", "populated endpoints → lsslots", w)
    else:
        _kv(
            "Note",
            privilege_note("— (needs sudo + dmidecode)", enabled=color)
            if info.get("needs_sudo")
            else "none reported",
            w,
        )
        _kv("Note", "populated endpoints → lsslots", w)

    if onboard:
        _section("Onboard")
        for rec in onboard:
            name = rec.get("Reference Designation") or rec.get("Type") or "?"
            bits = [rec.get("Type"), rec.get("Status")]
            _kv(name, "  ".join(b for b in bits if b) or "—", w)
    elif info.get("needs_sudo"):
        _section("Onboard")
        _kv(
            "Note",
            privilege_note("— (needs sudo + dmidecode)", enabled=color),
            w,
        )

    if ports:
        _section("Ports")
        for rec in ports:
            name = (
                rec.get("External Reference Designator")
                or rec.get("Internal Reference Designator")
                or rec.get("Port Type")
                or "?"
            )
            bits = [
                rec.get("Port Type"),
                rec.get("External Connector Type"),
                rec.get("Internal Connector Type"),
            ]
            _kv(name, "  ".join(b for b in bits if b) or "—", w)
    elif info.get("needs_sudo"):
        _section("Ports")
        _kv(
            "Note",
            privilege_note("— (needs sudo + dmidecode)", enabled=color),
            w,
        )

    _section("Notes")
    sudo_note = sudo_full_results_note("lsmobo")
    if sudo_note:
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    _kv("Note", "firmware detail → lsbios", w)
    _kv("Note", "PCIe populated / roles → lsslots", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    if _is_root() and not system_binary("dmidecode"):
        print(
            "Note: dmidecode not found; ports/onboard use sysfs only. "
            "Optional: apt install dmidecode for full lsmobo dump. "
            "Type 9 slots still come from dmi.py when DMI is readable.",
            file=sys.stderr,
        )

    info = get_mobo_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    if not any([
        info.get("bios"), info.get("baseboard"), info.get("chassis"),
        info.get("slots"), info.get("onboard"), info.get("ports"),
    ]):
        print("lsmobo: no DMI data available.", file=sys.stderr)
        sys.exit(1)

    print_mobo_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
