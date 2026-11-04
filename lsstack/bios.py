"""lsbios: BIOS/firmware identity from /sys/class/dmi/id; Type 0 extras via dmi.py.

Board/chassis/slots → lsmobo. PCIe connectors → lsslots.
"""

from __future__ import annotations

import json
import sys

from lsstack.color import color_enabled, privilege_note
from lsstack.common import dmi_or_none, read_dmi_id, sudo_full_results_note
from lsstack.dmi import bios_extra, probe_table


def get_bios_info() -> dict:
    """Structured BIOS fields from sysfs (+ optional Type 0 extras)."""
    dmi = read_dmi_id(
        "bios_vendor", "bios_version", "bios_date", "bios_release",
        "board_vendor", "board_name",
    )
    extras = bios_extra()
    board_bits = [
        dmi_or_none(dmi.get("board_vendor")),
        dmi_or_none(dmi.get("board_name")),
    ]
    board = " / ".join(x for x in board_bits if x) or None
    return {
        "vendor": dmi_or_none(dmi.get("bios_vendor")),
        "version": dmi_or_none(dmi.get("bios_version")),
        "date": dmi_or_none(dmi.get("bios_date")),
        "release": dmi_or_none(dmi.get("bios_release")),
        "rom_size": dmi_or_none(extras.get("ROM Size")),
        "bios_revision": dmi_or_none(extras.get("BIOS Revision")),
        "firmware_revision": dmi_or_none(extras.get("Firmware Revision")),
        "board": board,
        "source": "sysfs+dmi" if extras else "sysfs",
        "needs_sudo_for_rom": not bool(extras) and not probe_table().get("ok"),
    }


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _help_text() -> str:
    return """lsbios — BIOS / firmware identity for lab / workstation builds

What
  Reports firmware vendor, version, release date, and (with sudo) ROM size /
  BIOS revision from DMI.

Why
  Builders pin a known-good BIOS before GPU/PCIe experiments, and need a quick
  check that the machine is on the expected firmware train.

How
  Sysfs works without root. ROM size / revision need sudo for DMI Type 0 (amber).
  Board / chassis / slots → lsmobo. PCIe connectors → lsslots. Lean data → --json.

Usage: lsbios [options]
  --json, -j       JSON
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def print_bios_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_bios_info()
    labels = [
        "Vendor", "Version", "Release date", "BIOS release", "ROM size",
        "BIOS revision", "FW revision", "Board", "Note",
    ]
    w = _label_width(*labels)

    print("\nBIOS")
    _section("Firmware")
    _kv("Vendor", info.get("vendor"), w)
    _kv("Version", info.get("version"), w)
    _kv("Release date", info.get("date"), w)
    _kv("BIOS release", info.get("release"), w)
    if info.get("rom_size"):
        _kv("ROM size", info["rom_size"], w)
    else:
        _kv(
            "ROM size",
            privilege_note("— (needs sudo for DMI Type 0)", enabled=color)
            if info.get("needs_sudo_for_rom")
            else None,
            w,
        )
    if info.get("bios_revision"):
        _kv("BIOS revision", info["bios_revision"], w)
    elif info.get("needs_sudo_for_rom"):
        _kv(
            "BIOS revision",
            privilege_note("— (needs sudo for DMI Type 0)", enabled=color),
            w,
        )
    if info.get("firmware_revision"):
        _kv("FW revision", info["firmware_revision"], w)

    _section("Notes")
    sudo_note = sudo_full_results_note("lsbios")
    if sudo_note:
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    if info.get("board"):
        _kv("Board", f"{info['board']}  → lsmobo", w)
    _kv("Note", "PCIe connectors → lsslots", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_bios_info()
    if not any(info.get(k) for k in ("vendor", "version", "date")):
        print("lsbios: no BIOS data under /sys/class/dmi/id (not Linux SMBIOS?).", file=sys.stderr)
        sys.exit(1)

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_bios_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
