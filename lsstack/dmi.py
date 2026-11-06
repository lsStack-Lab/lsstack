"""First-party SMBIOS/DMI table reader (sysfs).

Owns Types 0 (BIOS extras), 4 (socket designation), 9 (system slots),
17 (memory devices), 41 (onboard Ethernet BDFs). Other types stay on
system dmidecode inside lsmobo only.

Requires readable /sys/firmware/dmi/tables/DMI (usually root).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterator

_DMI_TABLES = "/sys/firmware/dmi/tables/DMI"

# --- Type 17 offsets ---
_T17_TOTAL_WIDTH = 8
_T17_DATA_WIDTH = 10
_T17_SIZE = 12
_T17_FORM_FACTOR = 14
_T17_LOCATOR = 16
_T17_BANK = 17
_T17_MEMORY_TYPE = 18
_T17_TYPE_DETAIL = 19
_T17_SPEED = 21
_T17_MANUFACTURER = 23
_T17_SERIAL = 24
_T17_PART_NUMBER = 26
_T17_EXT_SIZE = 28
_T17_CONFIGURED_SPEED = 44
_T17_MIN_LEN = 24

# SMBIOS Type 17 Form Factor / Memory Type enums are 1-based (0 unused).
_FORM_FACTORS = (
    "",
    "Other", "Unknown", "SIMM", "SIP", "Chip", "DIP", "ZIP", "Proprietary Card",
    "DIMM", "TSOP", "Row of chips", "RIMM", "SODIMM", "SRIMM", "FB-DIMM", "Die",
)
_TYPE_DETAIL_BITS = (
    (1 << 7, "Synchronous"),
    (1 << 13, "Registered (Buffered)"),
    (1 << 14, "Unbuffered (Unregistered)"),
)
_MEM_TYPES = (
    "",
    "Other", "Unknown", "DRAM", "EDRAM", "VRAM", "SRAM", "RAM", "ROM",
    "Flash", "EEPROM", "FEPROM", "EPROM", "CDRAM", "3DRAM", "SDRAM", "SGRAM",
    "RDRAM", "DDR", "DDR2", "DDR2 FB-DIMM",
    "Reserved",
    "DDR3", "FBD2", "DDR4", "LPDDR",
    "LPDDR2", "LPDDR3", "LPDDR4", "Logical non-volatile", "HBM", "HBM2", "DDR5", "LPDDR5",
)

# --- Type 9 ---
_T9_DESIGNATION = 4
_T9_TYPE = 5
_T9_DATA_WIDTH = 6
_T9_USAGE = 7
_T9_MIN_LEN = 13
# SMBIOS 2.6+ bus address fields
_T9_SEGMENT = 13
_T9_BUS = 15
_T9_DEVFN = 16

_SLOT_TYPES = {
    0x01: "Other",
    0x02: "Unknown",
    0x03: "ISA",
    0x04: "MCA",
    0x05: "EISA",
    0x06: "PCI",
    0x07: "PC Card",
    0x08: "VL-VESA",
    0x09: "Proprietary",
    0x0A: "Processor Card",
    0x0B: "Proprietary Memory Card",
    0x0C: "I/O Riser Card",
    0x0D: "NuBus",
    0x0E: "PCI-66",
    0x0F: "AGP",
    0x10: "AGP 2X",
    0x11: "AGP 4X",
    0x12: "PCI-X",
    0x13: "AGP 8X",
    0xA0: "PC-98/C20",
    0xA1: "PC-98/C24",
    0xA2: "PC-98/E",
    0xA3: "PC-98/Local Bus",
    0xA4: "PC-98/Card",
    0xA5: "PCI Express",
    0xA6: "PCI Express x1",
    0xA7: "PCI Express x2",
    0xA8: "PCI Express x4",
    0xA9: "PCI Express x8",
    0xAA: "PCI Express x16",
    0xAB: "PCI Express Gen 2",
    0xAC: "PCI Express Gen 2 x1",
    0xAD: "PCI Express Gen 2 x2",
    0xAE: "PCI Express Gen 2 x4",
    0xAF: "PCI Express Gen 2 x8",
    0xB0: "PCI Express Gen 2 x16",
    0xB1: "PCI Express Gen 3",
    0xB2: "PCI Express Gen 3 x1",
    0xB3: "PCI Express Gen 3 x2",
    0xB4: "PCI Express Gen 3 x4",
    0xB5: "PCI Express Gen 3 x8",
    0xB6: "PCI Express Gen 3 x16",
    0xB8: "PCI Express Gen 4",
    0xB9: "PCI Express Gen 4 x1",
    0xBA: "PCI Express Gen 4 x2",
    0xBB: "PCI Express Gen 4 x4",
    0xBC: "PCI Express Gen 4 x8",
    0xBD: "PCI Express Gen 4 x16",
    0xBE: "PCI Express Gen 5",
    0xBF: "PCI Express Gen 5 x1",
    0xC0: "PCI Express Gen 5 x2",
    0xC1: "PCI Express Gen 5 x4",
    0xC2: "PCI Express Gen 5 x8",
    0xC3: "PCI Express Gen 5 x16",
}
_SLOT_WIDTHS = {
    0x01: "Other",
    0x02: "Unknown",
    0x03: "8-bit",
    0x04: "16-bit",
    0x05: "32-bit",
    0x06: "64-bit",
    0x07: "128-bit",
    0x08: "1x or x1",
    0x09: "2x or x2",
    0x0A: "4x or x4",
    0x0B: "8x or x8",
    0x0C: "12x or x12",
    0x0D: "16x or x16",
    0x0E: "32x or x32",
}
_SLOT_USAGE = {
    0x01: "Other",
    0x02: "Unknown",
    0x03: "Available",
    0x04: "In Use",
}

# --- Type 0 ---
_T0_VENDOR = 4
_T0_VERSION = 5
_T0_DATE = 8
_T0_ROM_SIZE = 9
_T0_MIN_LEN = 18
# SMBIOS 2.4+: BIOS Major/Minor at 20-21; EC firmware at 22-23 when length >= 24
_T0_BIOS_MAJOR = 20
_T0_BIOS_MINOR = 21
_T0_EC_MAJOR = 22
_T0_EC_MINOR = 23
# SMBIOS 3.1+: Extended BIOS ROM Size (WORD) when ROM Size byte is 0xFF
_T0_EXT_ROM = 24
_T0_EXT_ROM_MIN_LEN = 26

# --- Type 4 ---
_T4_SOCKET = 4
_T4_MIN_LEN = 26

# --- Type 41 ---
_T41_REF = 4
_T41_DEVICE_TYPE = 5
_T41_SEGMENT = 6
_T41_BUS = 8
_T41_DEVFN = 9
_T41_MIN_LEN = 11
# Type byte: bits 6:0 = type; bit 7 = enabled
_T41_TYPE_ETHERNET = 5


def probe_table() -> dict[str, Any]:
    """Return ``{ok, reason, path}`` for DMI table access without decoding."""
    path = _DMI_TABLES
    if not os.path.isfile(path):
        return {"ok": False, "reason": "no_table", "path": path}
    try:
        with open(path, "rb") as f:
            data = f.read(4)
    except PermissionError:
        return {"ok": False, "reason": "needs_sudo", "path": path}
    except OSError:
        return {"ok": False, "reason": "failed", "path": path}
    if len(data) < 4:
        return {"ok": False, "reason": "no_table", "path": path}
    return {"ok": True, "reason": None, "path": path}


def read_table() -> tuple[bytes | None, str | None]:
    """Return ``(data, reason)``. reason is None on success."""
    p = probe_table()
    if not p["ok"]:
        return None, p["reason"]
    try:
        with open(_DMI_TABLES, "rb") as f:
            data = f.read()
    except PermissionError:
        return None, "needs_sudo"
    except OSError:
        return None, "failed"
    if len(data) < 4:
        return None, "no_table"
    return data, None


def _dmi_strings(block: bytes) -> list[str]:
    """String table: index 0 unused/empty, 1 = first string."""
    strings = [""]
    i = 0
    while i < len(block):
        end = block.find(b"\x00", i)
        if end < 0:
            s = block[i:].decode("utf-8", errors="replace").strip()
            if s:
                strings.append(s)
            break
        s = block[i:end].decode("utf-8", errors="replace").strip()
        strings.append(s)
        i = end + 1
        if i < len(block) and block[i] == 0:
            break
    return strings


def _str_at(strings: list[str], idx: int) -> str:
    if 0 <= idx < len(strings):
        return (strings[idx] or "").strip()
    return ""


def iter_structures(data: bytes | None = None) -> Iterator[dict[str, Any]]:
    """Yield ``{type, length, handle, formatted, strings}`` for each structure."""
    if data is None:
        data, reason = read_table()
        if data is None:
            return
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        typ = data[pos]
        length = data[pos + 1]
        if length < 4:
            break
        if pos + length > n:
            break
        handle = data[pos + 2] | (data[pos + 3] << 8)
        formatted = data[pos : pos + length]
        str_start = pos + length
        str_end = data.find(b"\x00\x00", str_start)
        if str_end < 0:
            str_end = n
        strings = _dmi_strings(data[str_start:str_end])
        next_pos = str_end + 2 if str_end + 2 <= n else n
        yield {
            "type": typ,
            "length": length,
            "handle": handle,
            "formatted": formatted,
            "strings": strings,
        }
        pos = next_pos


def memory_devices() -> list[dict[str, Any]]:
    """SMBIOS Type 17 memory devices (populated and empty)."""
    data, _reason = read_table()
    if data is None:
        return []
    result: list[dict[str, Any]] = []
    bank_index = 0
    for st in iter_structures(data):
        if st["type"] != 17:
            continue
        formatted = st["formatted"]
        length = st["length"]
        strings = st["strings"]
        if length < _T17_MIN_LEN:
            continue
        bank_index += 1
        size_mb = formatted[_T17_SIZE] | (formatted[_T17_SIZE + 1] << 8)
        if length >= 32:
            ext = (
                formatted[_T17_EXT_SIZE]
                | (formatted[_T17_EXT_SIZE + 1] << 8)
                | (formatted[_T17_EXT_SIZE + 2] << 16)
                | (formatted[_T17_EXT_SIZE + 3] << 24)
            )
            ext &= 0x7FFFFFFF
            if size_mb == 0x7FFF and ext != 0:
                size_mb = ext * (1 << 10)
        loc_idx = formatted[_T17_LOCATOR]
        bank_idx = formatted[_T17_BANK]
        mem_type_idx = formatted[_T17_MEMORY_TYPE]
        speed_max = formatted[_T17_SPEED] | (formatted[_T17_SPEED + 1] << 8)
        configured = 0
        if length >= 46:
            configured = (
                formatted[_T17_CONFIGURED_SPEED]
                | (formatted[_T17_CONFIGURED_SPEED + 1] << 8)
            )
        locator = _str_at(strings, loc_idx)
        bank_str = _str_at(strings, bank_idx)
        width_str = "-"
        if length >= 12:
            total_w = formatted[_T17_TOTAL_WIDTH] | (formatted[_T17_TOTAL_WIDTH + 1] << 8)
            data_w = formatted[_T17_DATA_WIDTH] | (formatted[_T17_DATA_WIDTH + 1] << 8)
            if total_w != 0xFFFF and total_w != 0:
                if data_w != 0xFFFF and data_w != 0 and data_w != total_w:
                    width_str = "%d/%d" % (total_w, data_w)
                else:
                    width_str = str(total_w)
        mem_type = (
            _MEM_TYPES[mem_type_idx]
            if 0 < mem_type_idx < len(_MEM_TYPES) and _MEM_TYPES[mem_type_idx]
            else "Unknown"
        )
        manufacturer = ""
        part_number = ""
        serial = ""
        if length >= 24:
            manufacturer = _str_at(strings, formatted[_T17_MANUFACTURER])
        if length >= 25:
            serial = _str_at(strings, formatted[_T17_SERIAL])
        if length >= 27:
            part_number = _str_at(strings, formatted[_T17_PART_NUMBER])

        if size_mb == 0 or size_mb == 0x7FFF:
            description = "[empty]" if size_mb == 0 else "Unknown"
            size_str = "0"
            dimm_form, sync, unbuffered, unregistered = "[empty]", "-", "-", "-"
            type_str = "-"
        else:
            if size_mb >= 1024:
                size_str = "%d GiB" % (size_mb // 1024)
            else:
                size_str = "%d MiB" % size_mb
            parts: list[str] = []
            dimm_form = "DIMM"
            if length >= 16:
                ff_idx = formatted[_T17_FORM_FACTOR]
                ff_name = (
                    _FORM_FACTORS[ff_idx]
                    if 0 < ff_idx < len(_FORM_FACTORS) and _FORM_FACTORS[ff_idx]
                    else ""
                )
                if ff_name in ("Other", "Unknown", "TSOP") and size_mb >= 4096:
                    parts.append("DIMM")
                elif ff_name and ff_name not in ("Other", "Unknown"):
                    parts.append(ff_name)
                    dimm_form = ff_name
                else:
                    parts.append("DIMM")
            if length >= 21:
                detail = formatted[_T17_TYPE_DETAIL] | (formatted[_T17_TYPE_DETAIL + 1] << 8)
            else:
                detail = 0
            # Do not invent DDR4 when type is Unknown — show what firmware reported.
            parts.append(mem_type)
            detail_bits_set = detail & ((1 << 7) | (1 << 13) | (1 << 14))
            is_sync = bool(detail & (1 << 7))
            is_unbuffered = bool(detail & (1 << 14))
            is_registered = bool(detail & (1 << 13))
            if detail_bits_set and length >= 21:
                for bit, name in _TYPE_DETAIL_BITS:
                    if detail & bit:
                        parts.append(name)
            sync = "yes" if is_sync else "no"
            unbuffered = "yes" if is_unbuffered else "no"
            unregistered = "no" if is_registered else ("yes" if is_unbuffered else "no")
            description = " ".join(parts)
            if part_number:
                description += " (%s)" % part_number
            type_str = mem_type
        label = bank_str or locator or ("Bank %d" % bank_index)
        if length >= 46 and configured and speed_max:
            rated_mts_dmi = max(speed_max, configured)
            running_mts = min(speed_max, configured)
        else:
            rated_mts_dmi = speed_max if speed_max else None
            running_mts = (
                configured
                if (length >= 46 and configured)
                else (speed_max if speed_max else None)
            )
        result.append(
            {
                "bank": bank_index,
                "label": label,
                "slot": locator,
                "description": description,
                "dimm_form": dimm_form,
                "sync": sync,
                "unbuffered": unbuffered,
                "unregistered": unregistered,
                "size": size_str,
                "type": type_str,
                "width": width_str,
                "part_number": part_number or None,
                "serial": serial or None,
                "manufacturer": manufacturer or None,
                "rated_mts": rated_mts_dmi,
                "running_mts": running_mts,
                "size_mb": size_mb if size_mb not in (0, 0x7FFF) else 0,
            }
        )
    return result


def populated_dimm_count() -> tuple[int | None, str | None]:
    """Return ``(count, reason)`` for Balance DIMM vs channels."""
    data, reason = read_table()
    if data is None:
        return None, reason or "failed"
    devices = memory_devices()
    if not devices:
        return None, "no_dimm_data"
    n = sum(1 for d in devices if (d.get("size_mb") or 0) > 0)
    if n == 0:
        # All empty rows still means we decoded the table.
        return 0, None
    return n, None


def system_slots() -> list[dict[str, str]]:
    """SMBIOS Type 9: designation, type, data_width, usage, bus_address."""
    data, _reason = read_table()
    if data is None:
        return []
    slots: list[dict[str, str]] = []
    for st in iter_structures(data):
        if st["type"] != 9:
            continue
        formatted = st["formatted"]
        length = st["length"]
        strings = st["strings"]
        if length < _T9_MIN_LEN:
            continue
        designation = _str_at(strings, formatted[_T9_DESIGNATION])
        typ = _SLOT_TYPES.get(formatted[_T9_TYPE], "Unknown")
        width = _SLOT_WIDTHS.get(formatted[_T9_DATA_WIDTH], "Unknown")
        usage = _SLOT_USAGE.get(formatted[_T9_USAGE], "Unknown")
        bus_address = ""
        if length > _T9_DEVFN:
            seg = formatted[_T9_SEGMENT] | (formatted[_T9_SEGMENT + 1] << 8)
            bus = formatted[_T9_BUS]
            devfn = formatted[_T9_DEVFN]
            # 0xFF bus often means "not provided"
            if bus != 0xFF:
                device = (devfn >> 3) & 0x1F
                function = devfn & 0x07
                bus_address = f"{seg:04x}:{bus:02x}:{device:02x}.{function}"
        if designation or bus_address:
            slots.append(
                {
                    "designation": designation,
                    "type": typ,
                    "data_width": width,
                    "usage": usage,
                    "bus_address": bus_address,
                }
            )
    return slots


def _type0_rom_size(formatted: bytes, length: int) -> str | None:
    """Legacy 64KiB units, or Extended BIOS ROM Size when the byte is 0xFF."""
    if length <= _T0_ROM_SIZE:
        return None
    rom = formatted[_T0_ROM_SIZE]
    if rom != 0xFF:
        kib = 64 * (rom + 1)
        if kib >= 1024:
            return "%d MB" % (kib // 1024)
        return "%d kB" % kib
    if length < _T0_EXT_ROM_MIN_LEN:
        return None
    raw = formatted[_T0_EXT_ROM] | (formatted[_T0_EXT_ROM + 1] << 8)
    unit = (raw >> 14) & 0x3
    n = raw & 0x3FFF
    if n == 0:
        return None
    if unit == 0:
        return "%d MB" % n
    if unit == 1:
        return "%d GB" % n
    return None


def bios_extra() -> dict[str, str]:
    """Type 0 fields beyond sysfs: ROM size, BIOS revision, firmware revision."""
    data, _reason = read_table()
    if data is None:
        return {}
    for st in iter_structures(data):
        if st["type"] != 0:
            continue
        formatted = st["formatted"]
        length = st["length"]
        if length < _T0_MIN_LEN:
            return {}
        out: dict[str, str] = {}
        rom = _type0_rom_size(formatted, length)
        if rom:
            out["ROM Size"] = rom
        if length >= 22:
            major = formatted[_T0_BIOS_MAJOR]
            minor = formatted[_T0_BIOS_MINOR]
            if major != 0xFF and minor != 0xFF:
                out["BIOS Revision"] = "%d.%d" % (major, minor)
        if length >= 24:
            major = formatted[_T0_EC_MAJOR]
            minor = formatted[_T0_EC_MINOR]
            if major != 0xFF and minor != 0xFF:
                out["Firmware Revision"] = "%d.%d" % (major, minor)
        return out
    return {}


def processor_socket_designations() -> list[str]:
    """Type 4 Socket Designation strings (multi-socket)."""
    data, _reason = read_table()
    if data is None:
        return []
    result: list[str] = []
    for st in iter_structures(data):
        if st["type"] != 4:
            continue
        formatted = st["formatted"]
        if st["length"] < _T4_MIN_LEN:
            continue
        sock = _str_at(st["strings"], formatted[_T4_SOCKET])
        if sock:
            result.append(sock)
    return result


def onboard_ethernet_bdfs() -> set[str] | None:
    """Type 41 onboard Ethernet bus addresses, or None if table unreadable."""
    data, reason = read_table()
    if data is None:
        return None
    onboard: set[str] = set()
    for st in iter_structures(data):
        if st["type"] != 41:
            continue
        formatted = st["formatted"]
        if st["length"] < _T41_MIN_LEN:
            continue
        type_byte = formatted[_T41_DEVICE_TYPE]
        device_type = type_byte & 0x7F
        if device_type != _T41_TYPE_ETHERNET:
            continue
        seg = formatted[_T41_SEGMENT] | (formatted[_T41_SEGMENT + 1] << 8)
        bus = formatted[_T41_BUS]
        devfn = formatted[_T41_DEVFN]
        if bus == 0xFF:
            continue
        device = (devfn >> 3) & 0x1F
        function = devfn & 0x07
        bdf = f"{seg:04x}:{bus:02x}:{device:02x}.{function}"
        onboard.add(bdf.lower())
    return onboard


def normalize_bdf(bus: str) -> str | None:
    """Normalize a bus address string to lowercase seg:bus:dev.fn."""
    bus = (bus or "").strip().lower()
    if re.match(r"^[0-9a-f]+:[0-9a-f]+:[0-9a-f]+\.[0-9a-f]+$", bus):
        return bus
    if re.match(r"^[0-9a-f]+:[0-9a-f]+\.[0-9a-f]+$", bus):
        return "0000:" + bus
    return None
