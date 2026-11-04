"""lsram: memory/DIMM details. Reads /proc/meminfo; SMBIOS Type 17 from DMI for slot, size, type, clock, width, vendor, part number. All DIMM table fields from DMI only.

Always runs unprivileged for /proc/meminfo. DIMM/SPD detail needs sudo (amber notes).
"""

import glob
import os
import re
import sys

from lsstack.color import color_enabled, privilege_note
from lsstack.common import load_json, read_file, sudo_full_results_note


def _require_root() -> bool:
    """True if process has root privileges (e.g. euid 0 on Linux)."""
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False


def _dmi_readable() -> bool:
    """True when SMBIOS table bytes are readable (usually root)."""
    from lsstack.dmi import probe_table

    return bool(probe_table().get("ok"))


def _meminfo_kb(key: str) -> int | None:
    raw = read_file("/proc/meminfo")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith(key + ":"):
            m = re.match(r"%s:\s+(\d+)" % re.escape(key), line)
            return int(m.group(1)) if m else None
    return None


def _format_kb(kb: int) -> str:
    if kb >= 1024 * 1024:
        return "%.2f GiB" % (kb / (1024 * 1024))
    if kb >= 1024:
        return "%.2f MiB" % (kb / 1024)
    return "%d KiB" % kb


# SPD (Serial Presence Detect) from sysfs: kernel exposes ee1004 (DDR4) / at24 (DDR3 and older) under /sys/bus/i2c/devices/*/eeprom
_SPD_EEPROM_GLOB = "/sys/bus/i2c/devices/*/eeprom"
# JEDEC SPD byte offsets (page 0)
_SPD_BYTE_MEMORY_TYPE = 2   # 0x0B=DDR3, 0x0C=DDR4, 0x12=DDR5
_SPD_BYTE_MODULE_TYPE = 3   # [1:0] 00=UDIMM, 01=RDIMM, 10=SODIMM, 11=LRDIMM
_SPD_BYTE_TIMEBASE = 17     # DDR4: 0x00 = 1/256 ns
_SPD_BYTE_TCKMIN_LO = 18    # DDR4 tCKmin low byte (1/256 ns)
_SPD_BYTE_TCKMIN_HI = 19    # DDR4 tCKmin high byte
_SPD_BYTE_TCK_DDR3 = 18     # DDR3 tCK in 0.25 ns units
_SPD_MFG_LO = 117          # DDR3: Module Manufacturer ID lsbyte (JEP-106)
_SPD_MFG_HI = 118          # DDR3: Module Manufacturer ID msbyte
_SPD_PN_START = 128        # DDR3: Module part number ASCII (bytes 128-145)
_SPD_PN_LEN = 18
# DDR4 (EE1004): Block 1 = bytes 256-511; manufacturer at 320-321, part number at 328-347
_SPD_DDR4_MFG_LO = 320
_SPD_DDR4_MFG_HI = 321
_SPD_DDR4_PN_START = 328
_SPD_DDR4_PN_LEN = 20
_SPD_DDR4_SERIAL = 325      # DDR4: Module Serial Number, 4 bytes (JEDEC)
_SPD_DDR3_SERIAL = 122      # DDR3: Module Serial Number, 4 bytes
_SPD_BYTE_MODULE_ORG = 12   # DDR4: bits 2:0 = Package Ranks per DIMM - 1
_SPD_MIN_LEN = 20

# JEDEC manufacturer ID (msbyte or 16-bit); partial list (DDR3 byte 118, DDR4 byte 321)
_JEDEC_MFG = {
    0x80: "Micron", 0x2C: "Micron", 0x4F: "Micron",
    0x98: "Samsung", 0xCE: "Samsung", 0x04: "Samsung",
    0xAD: "SK Hynix", 0xAE: "SK Hynix",
    0xFE: "Elpida", 0x9E: "Smart Modular", 0x51: "Qimonda",
    0x9B: "G.Skill", 0x2B: "G.Skill", 0x34: "G.Skill",
    0xEF: "Elpida", 0x26: "Nanya", 0x4C: "Ramaxel",
}

_SPD_MEMORY_TYPES = {
    0x0B: "DDR3",
    0x0C: "DDR4",
    0x12: "DDR5",
}
_SPD_MODULE_TYPE_NAMES = {
    0: "DIMM",   # UDIMM
    1: "DIMM",   # RDIMM -> "Registered (Buffered)"
    2: "SODIMM",
    3: "DIMM",   # LRDIMM
}


def _read_spd_raw(path: str, size: int = 512) -> bytes | None:
    """Read up to size bytes from a sysfs eeprom (SPD). EE1004/DDR4 has 512 bytes (page 0 + page 1).
    Returns None on error. May return fewer than size bytes if device is smaller (e.g. 256)."""
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return None


def _decode_spd_description(data: bytes) -> str | None:
    """Build description string from SPD page 0. Returns None if not recognizable."""
    if len(data) < _SPD_MIN_LEN:
        return None
    mem_type_val = data[_SPD_BYTE_MEMORY_TYPE]
    mem_type = _SPD_MEMORY_TYPES.get(mem_type_val)
    if not mem_type:
        return None
    module_bits = data[_SPD_BYTE_MODULE_TYPE] & 0x0F
    form = _SPD_MODULE_TYPE_NAMES.get(module_bits & 3, "DIMM")
    # Registered vs Unbuffered: RDIMM=1, LRDIMM=3
    if (module_bits & 3) in (1, 3):
        detail = "Synchronous Registered (Buffered)"
    else:
        detail = "Synchronous Unbuffered (Unregistered)"
    speed_mts: float | None = None
    if mem_type_val == 0x0C and len(data) > _SPD_BYTE_TCKMIN_HI + 1:
        # DDR4: tCKmin in 1/256 ns
        tck_lo = data[_SPD_BYTE_TCKMIN_LO]
        tck_hi = data[_SPD_BYTE_TCKMIN_HI]
        tck_256 = tck_lo + (tck_hi << 8)
        if tck_256 > 0:
            tck_ns = tck_256 / 256.0
            speed_mts = 1000.0 / tck_ns
    elif mem_type_val == 0x0B and len(data) > _SPD_BYTE_TCK_DDR3:
        # DDR3: tCK in 0.25 ns units (byte 18)
        tck_quarter = data[_SPD_BYTE_TCK_DDR3]
        if tck_quarter > 0:
            tck_ns = tck_quarter * 0.25
            speed_mts = 1000.0 / tck_ns
    if speed_mts is None or speed_mts <= 0:
        parts = [form, mem_type, detail]
    else:
        speed_int = int(round(speed_mts))
        period_ns = 1000.0 / speed_int
        parts = [form, mem_type, detail, "%d MHz (%.1f ns)" % (speed_int, period_ns)]
    return " ".join(parts)


def _spd_rated_mts(data: bytes) -> int | None:
    """Return rated speed in MT/s from SPD page 0, or None."""
    if len(data) < _SPD_MIN_LEN:
        return None
    mem_type_val = data[_SPD_BYTE_MEMORY_TYPE]
    speed_mts: float | None = None
    if mem_type_val == 0x0C and len(data) > _SPD_BYTE_TCKMIN_HI + 1:
        tck_256 = data[_SPD_BYTE_TCKMIN_LO] + (data[_SPD_BYTE_TCKMIN_HI] << 8)
        if tck_256 > 0:
            speed_mts = 1000.0 / (tck_256 / 256.0)
    elif mem_type_val == 0x0B and len(data) > _SPD_BYTE_TCK_DDR3:
        tck_quarter = data[_SPD_BYTE_TCK_DDR3]
        if tck_quarter > 0:
            speed_mts = 1000.0 / (tck_quarter * 0.25)
    return int(round(speed_mts)) if speed_mts and speed_mts > 0 else None


def _spd_manufacturer(data: bytes) -> str:
    """Decode module manufacturer from SPD. DDR3: bytes 117-118 (page 0). DDR4: bytes 320-321 (block 1)."""
    mem_type_val = data[_SPD_BYTE_MEMORY_TYPE] if len(data) > _SPD_BYTE_MEMORY_TYPE else 0
    if mem_type_val == 0x0C and len(data) > _SPD_DDR4_MFG_HI:
        lo, hi = data[_SPD_DDR4_MFG_LO], data[_SPD_DDR4_MFG_HI]
    elif len(data) > _SPD_MFG_HI:
        lo, hi = data[_SPD_MFG_LO], data[_SPD_MFG_HI]
    else:
        return ""
    if lo == 0 and hi == 0:
        return ""
    name = _JEDEC_MFG.get(hi) or _JEDEC_MFG.get(lo)
    if name:
        return name.strip()
    return "0x%02X%02X" % (lo, hi)


def _is_placeholder_serial(serial: str | None) -> bool:
    """True if SMBIOS/DMI left a blank or all-zero serial (common on consumer boards)."""
    if serial is None:
        return True
    s = str(serial).strip()
    if not s:
        return True
    low = s.lower()
    if low in ("n/a", "na", "none", "unknown", "not specified", "to be filled by o.e.m.", "oem"):
        return True
    # All zeros / hex zeros (00000000, 0x00000000, 00-00-…)
    hexish = re.sub(r"[^0-9a-fA-F]", "", s)
    if hexish and set(hexish) <= {"0"}:
        return True
    if set(s.replace(" ", "")) <= {"0"}:
        return True
    return False


def _spd_serial(data: bytes) -> str | None:
    """Module serial from SPD as 8 hex digits, or None if missing/placeholder."""
    if len(data) <= _SPD_BYTE_MEMORY_TYPE:
        return None
    mem_type_val = data[_SPD_BYTE_MEMORY_TYPE]
    raw = b""
    if mem_type_val == 0x0C and len(data) >= _SPD_DDR4_SERIAL + 4:
        raw = data[_SPD_DDR4_SERIAL : _SPD_DDR4_SERIAL + 4]
    elif mem_type_val == 0x0B and len(data) >= _SPD_DDR3_SERIAL + 4:
        raw = data[_SPD_DDR3_SERIAL : _SPD_DDR3_SERIAL + 4]
    if not raw or len(raw) < 4:
        return None
    if set(raw) <= {0}:
        return None
    return "".join("%02X" % b for b in raw)


def _spd_part_number(data: bytes) -> str:
    """Extract module part number from SPD. DDR3: bytes 128-145 (ASCII). DDR4: bytes 328-347 (block 1)."""
    mem_type_val = data[_SPD_BYTE_MEMORY_TYPE] if len(data) > _SPD_BYTE_MEMORY_TYPE else 0
    if mem_type_val == 0x0C and len(data) >= _SPD_DDR4_PN_START + _SPD_DDR4_PN_LEN:
        raw = data[_SPD_DDR4_PN_START : _SPD_DDR4_PN_START + _SPD_DDR4_PN_LEN]
    elif len(data) >= _SPD_PN_START + _SPD_PN_LEN:
        raw = data[_SPD_PN_START : _SPD_PN_START + _SPD_PN_LEN]
    else:
        return ""
    try:
        s = raw.decode("ascii", errors="replace").strip().strip("\x00")
        return s if s and not all(c in " \x00" for c in s) else ""
    except Exception:
        return ""


def _decode_spd_description_no_speed(data: bytes) -> str | None:
    """Description (type + detail) without speed, for use with separate Rated column."""
    if len(data) < _SPD_MIN_LEN:
        return None
    mem_type = _SPD_MEMORY_TYPES.get(data[_SPD_BYTE_MEMORY_TYPE])
    if not mem_type:
        return None
    module_bits = data[_SPD_BYTE_MODULE_TYPE] & 0x0F
    form = _SPD_MODULE_TYPE_NAMES.get(module_bits & 3, "DIMM")
    detail = "Synchronous Registered (Buffered)" if (module_bits & 3) in (1, 3) else "Synchronous Unbuffered (Unregistered)"
    return f"{form} {mem_type} {detail}"


def _spd_columns(data: bytes) -> dict | None:
    """Return {dimm_form, sync, unbuffered, unregistered} from SPD. Unbuffered implies unregistered."""
    if len(data) < _SPD_MIN_LEN:
        return None
    if _SPD_MEMORY_TYPES.get(data[_SPD_BYTE_MEMORY_TYPE]) is None:
        return None
    module_bits = data[_SPD_BYTE_MODULE_TYPE] & 0x0F
    form = _SPD_MODULE_TYPE_NAMES.get(module_bits & 3, "DIMM")
    registered = (module_bits & 3) in (1, 3)  # RDIMM, LRDIMM
    unbuffered = not registered
    return {"dimm_form": form, "sync": "yes", "unbuffered": "yes" if unbuffered else "no", "unregistered": "yes" if unbuffered else "no"}


def _spd_per_dimm() -> list[dict]:
    """Read SPD from sysfs; return list of {description, rated_mts, manufacturer, part_number} per populated DIMM."""
    all_paths = sorted(glob.glob(_SPD_EEPROM_GLOB))
    # Prefer devices at SPD addresses 0x50-0x57 (e.g. .../4-0050/eeprom or i2c-4/4-0050/eeprom)
    paths = [p for p in all_paths if re.search(r"005[0-7]/eeprom$", p)]
    if not paths:
        paths = all_paths
    result: list[dict] = []
    for path in paths:
        data = _read_spd_raw(path)
        if not data or len(data) < _SPD_MIN_LEN:
            continue
        desc = _decode_spd_description_no_speed(data)
        if not desc:
            continue
        rated = _spd_rated_mts(data)
        mfg = _spd_manufacturer(data)
        pn = _spd_part_number(data)
        mem_type_val = data[_SPD_BYTE_MEMORY_TYPE]
        type_str = _SPD_MEMORY_TYPES.get(mem_type_val) or ""
        rank_str = "-"
        if mem_type_val == 0x0C and len(data) > _SPD_BYTE_MODULE_ORG:
            # DDR4: byte 12 bits 2:0 = Package Ranks per DIMM - 1
            ranks_minus_1 = data[_SPD_BYTE_MODULE_ORG] & 7
            rank_str = str(ranks_minus_1 + 1)
        elif mem_type_val == 0x0B and len(data) > 7:
            # DDR3: byte 7 bits 1:0 = ranks - 1
            rank_str = str((data[7] & 3) + 1)
        elif mem_type_val == 0x12 and len(data) > 7:
            # DDR5: byte 7 bits 2:0 = Package Ranks per DIMM (1-based in some specs; we use (n&7)+1)
            rank_str = str((data[7] & 7) + 1)
        cols = _spd_columns(data)
        row = {
            "description": desc,
            "rated_mts": rated,
            "manufacturer": mfg,
            "part_number": pn,
            "type": type_str,
            "rank": rank_str,
            "serial": _spd_serial(data),
            "spd_path": path,
        }
        if cols:
            row.update(cols)
        result.append(row)
    return result


def _merge_spd_serials(banks: list[dict]) -> None:
    """Prefer SPD module serial when DMI/SMBIOS left a placeholder (e.g. 00000000)."""
    spd_rows = _spd_per_dimm()
    if not spd_rows:
        return
    populated = [b for b in banks if not _is_empty_bank(b)]
    # Zip by discovery order (i2c 0x50.. vs DMI Type17 order). Same kit P/Ns are common.
    for b, s in zip(populated, spd_rows):
        spd_ser = (s.get("serial") or "").strip()
        if not spd_ser:
            continue
        if _is_placeholder_serial(b.get("serial")):
            b["serial"] = spd_ser
            b["serial_source"] = "spd"


def _spd_descriptions() -> list[str]:
    """Read SPD from sysfs eeproms (ee1004/at24). Returns list of description strings (with speed), one per DIMM. No external tools."""
    all_paths = sorted(glob.glob(_SPD_EEPROM_GLOB))
    paths = [p for p in all_paths if re.search(r"005[0-7]/eeprom$", p)]
    if not paths:
        paths = all_paths
    result: list[str] = []
    for path in paths:
        data = _read_spd_raw(path)
        if not data:
            continue
        desc = _decode_spd_description(data)
        if desc:
            result.append(desc)
    return result


def get_dimm_devices() -> list[dict]:
    """Populated and empty DIMM slots from SMBIOS Type 17 (requires readable DMI table)."""
    from lsstack.dmi import memory_devices

    return memory_devices()


def _infer_dram_gen_from_part_number(part_no: str) -> str | None:
    """Infer DDR4 vs DDR5 from part number patterns. Returns 'DDR4', 'DDR5', or None."""
    if not part_no or not part_no.strip():
        return None
    p = part_no.strip().upper()
    # G.Skill: F4 = DDR4, F5 = DDR5
    if p.startswith("F5-") or p.startswith("F5"):
        return "DDR5"
    if p.startswith("F4-") or p.startswith("F4"):
        return "DDR4"
    # Samsung: M32x = DDR5, M3x7/8/9 = DDR4
    if p.startswith("M32") or p.startswith("M321") or p.startswith("M323") or p.startswith("M324"):
        return "DDR5"
    if p.startswith("M378") or p.startswith("M391") or p.startswith("M393") or p.startswith("M386") or p.startswith("M392") or p.startswith("M474"):
        return "DDR4"
    # Kingston
    if "KF5" in p or "KVR48" in p or "KVR56" in p or "KVR60" in p:
        return "DDR5"
    if p.startswith("KVR") or "KF4" in p:
        return "DDR4"
    # Crucial / Micron
    if "G48" in p or "48C40" in p or "4800" in p or "48U5" in p or p.startswith("MTC20") or p.startswith("MT40"):
        return "DDR5"
    if "G4" in p or "G4D" in p or p.startswith("MT8") or p.startswith("MT18") or "3200" in p:
        return "DDR4"
    # SK Hynix
    if p.startswith("HMCG"):
        return "DDR5"
    if p.startswith("HMA8"):
        return "DDR4"
    return None


def _wrap(text: str, width: int) -> list[str]:
    """Split text into lines of at most width chars, breaking at word boundaries."""
    if not text or width < 1:
        return [text] if text else [""]
    lines = []
    rest = text
    while rest:
        if len(rest) <= width:
            lines.append(rest)
            break
        chunk = rest[: width + 1]
        last_space = chunk.rfind(" ")
        if last_space <= 0:
            last_space = width
        lines.append(rest[:last_space].rstrip())
        rest = rest[last_space:].lstrip()
    return lines


def _ram_specs_lookup(specs_list: list[dict]) -> dict[tuple[str, str], dict]:
    """Build lookup (vendor_lower, part_number) -> spec dict from ram_specs.json array.
    Stores under both vendor and vendor with '.'/'-' normalized so DMI 'G-Skill' matches CSV 'G.Skill'."""
    out: dict[tuple[str, str], dict] = {}
    for s in specs_list or []:
        vendor = (s.get("vendor") or "").strip()
        part = (s.get("part_number") or "").strip()
        if not part:
            continue
        vlo = vendor.lower()
        out[(vlo, part)] = s
        alt = vlo.replace(".", "-")
        if alt != vlo:
            out[(alt, part)] = s
        alt2 = vlo.replace("-", ".")
        if alt2 != vlo:
            out[(alt2, part)] = s
    return out


def get_memory_info() -> dict:
    """Return dict with total_kb, available_kb, free_kb, and optional dimm_count."""
    total = _meminfo_kb("MemTotal")
    available = _meminfo_kb("MemAvailable")
    free = _meminfo_kb("MemFree")
    return {
        "total_kb": total,
        "available_kb": available,
        "free_kb": free,
        "total_human": _format_kb(total) if total else "N/A",
        "available_human": _format_kb(available) if available else "N/A",
    }


def get_edac_info() -> dict:
    """System DRAM ECC via EDAC (/sys/devices/system/edac/mc).

    Returns controllers list with ce_count / ue_count when present.
    Empty controllers means no ECC memory controller exposed (typical desktop).
    """
    root = "/sys/devices/system/edac/mc"
    controllers: list[dict] = []
    if not os.path.isdir(root):
        return {"available": False, "controllers": controllers}
    try:
        names = os.listdir(root)
    except OSError:
        return {"available": False, "controllers": controllers}
    for name in sorted(names):
        if not re.match(r"mc\d+$", name):
            continue
        base = os.path.join(root, name)
        def _int_field(fname: str) -> int | None:
            raw = read_file(os.path.join(base, fname))
            if raw is None:
                return None
            try:
                return int(raw.strip())
            except ValueError:
                return None

        controllers.append({
            "id": name,
            "ce_count": _int_field("ce_count"),
            "ue_count": _int_field("ue_count"),
            "ce_noinfo_count": _int_field("ce_noinfo_count"),
            "ue_noinfo_count": _int_field("ue_noinfo_count"),
            "size_mb": _int_field("size_mb"),
        })
    return {"available": bool(controllers), "controllers": controllers}


def _kv(label: str, value: str | None, width: int = 18) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _is_empty_bank(b: dict) -> bool:
    return (b.get("size") or "0") == "0"


def _slot_short(slot: str | None) -> str:
    if not slot:
        return "?"
    s = slot.strip()
    if s.upper().startswith("DIMM_"):
        return s[5:]
    return s


def _cpu_memory_channels_spec() -> int | None:
    """Package memory channel count from cpu_specs JSON (same data lsproc uses)."""
    model = None
    for line in (read_file("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    if not model:
        return None
    from lsstack.common import lookup_cpu_specs

    leaf = lookup_cpu_specs(model)
    if not leaf:
        return None
    ch = leaf.get("memory_channels")
    try:
        return int(ch) if ch is not None else None
    except (TypeError, ValueError):
        return None


def _enrich_banks_with_specs(banks: list[dict], spec_lookup: dict) -> bool:
    """Attach ram_specs fields onto banks. Returns True if any spec DB is loaded."""
    has_specs = bool(spec_lookup)
    if not has_specs:
        for b in banks:
            b["display_type"] = b.get("type") or "-"
        return False
    for b in banks:
        vendor = (b.get("manufacturer") or "").strip()
        part_no = (b.get("part_number") or "").strip()
        if vendor and part_no and b.get("size") and b["size"] != "0":
            spec = spec_lookup.get((vendor.lower(), part_no))
            if spec:
                b["spec_voltage"] = spec.get("voltage") or "-"
                b["spec_timings"] = spec.get("primary_timings") or "-"
                b["spec_thermal"] = spec.get("thermal_prof") or "-"
                r, o = (spec.get("rank") or "").strip(), (spec.get("organization") or "").strip()
                b["spec_rank"] = "%sR%s" % (r, o) if (r and o) else (r or "-")
                ms = spec.get("max_speed_mts")
                b["spec_speed_mts"] = str(ms) if ms is not None and ms != "" else "-"
                inferred = _infer_dram_gen_from_part_number(part_no)
                b["display_type"] = inferred or b.get("type") or "-"
                continue
        b["spec_voltage"] = "-"
        b["spec_timings"] = "-"
        b["spec_thermal"] = "-"
        b["spec_rank"] = "-"
        b["spec_speed_mts"] = "-"
        b["display_type"] = b.get("type") or "-"
    for b in banks:
        b["display_type"] = b.get("display_type") or b.get("type") or "-"
    return True


def _bank_row(b: dict, has_specs: bool) -> tuple:
    raw_size = b.get("size") or "0"
    if raw_size == "0":
        size_display = "-"
        vendor = "-"
        part_no = "-"
    else:
        size_display = raw_size.replace(" GiB", "").replace(" MiB", "").strip()
        vendor = (b.get("manufacturer") or "").strip() or "-"
        part_no = (b.get("part_number") or "").strip() or "-"
    form = b.get("dimm_form") or "-"
    if form == "[empty]":
        form = "-"
    running = str(b["running_mts"]) if b.get("running_mts") else "-"
    rated = str(b["rated_mts"]) if b.get("rated_mts") else "-"
    if has_specs:
        return (
            str(b["bank"] - 1),
            size_display,
            b.get("slot") or "-",
            b.get("display_type") or "-",
            form,
            rated,
            running,
            b.get("spec_speed_mts") or "-",
            b.get("width") or "-",
            vendor,
            part_no,
            b.get("spec_rank") or "-",
            b.get("spec_voltage") or "-",
            b.get("spec_timings") or "-",
            b.get("spec_thermal") or "-",
        )
    return (
        str(b["bank"] - 1),
        size_display,
        b.get("slot") or "-",
        b.get("display_type") or "-",
        form,
        rated,
        running,
        b.get("width") or "-",
        vendor,
        part_no,
    )


def _print_dimm_table(banks: list[dict], has_specs: bool) -> None:
    if has_specs:
        header_line1 = ("", "Size", "", "", "", "Clock", "Config", "Max", "", "", "", "", "", "", "")
        header_line2 = (
            "Bank", "GiB", "Slot", "Type", "Form", "MHz", "MHz", "MHz", "Width",
            "Vendor", "Part#", "Rank", "Volts", "Timings", "Thermal",
        )
        right_cols = (0, 1, 5, 6, 7, 8, 11)
    else:
        header_line1 = ("", "Size", "", "", "", "Clock", "Config", "", "", "")
        header_line2 = ("Bank", "GiB", "Slot", "Type", "Form", "MHz", "MHz", "Width", "Vendor", "Part#")
        right_cols = (0, 1, 5, 6, 7)
    rows = [_bank_row(b, has_specs) for b in banks]
    all_cells = [header_line1, header_line2] + list(rows)
    cols = list(zip(*all_cells))
    widths = [max(len(str(cell)) for cell in col) for col in cols]
    sep = "|"
    fmt_parts = []
    for i, w in enumerate(widths):
        if i in right_cols:
            fmt_parts.append(sep + " %%%ds " % w)
        else:
            fmt_parts.append(sep + " %%-%ds " % w)
    fmt = "".join(fmt_parts) + sep
    print(fmt % header_line1)
    print(fmt % header_line2)
    print(sep + sep.join("-" * (w + 2) for w in widths) + sep)
    for row in rows:
        print(fmt % row)


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.
    _ = "--verbose" in args or "-v" in args
    color = color_enabled(args)
    if not read_file("/proc/meminfo"):
        print("Error: /proc/meminfo not found", file=sys.stderr)
        sys.exit(1)

    as_root = _dmi_readable()
    info = get_memory_info()
    banks = get_dimm_devices() if as_root else []
    if banks:
        _merge_spd_serials(banks)
    ram_specs_raw = load_json("ram_specs.json")
    spec_lookup = _ram_specs_lookup(ram_specs_raw) if isinstance(ram_specs_raw, list) else {}
    has_specs = _enrich_banks_with_specs(banks, spec_lookup) if banks else False

    populated_banks = [b for b in banks if not _is_empty_bank(b)]

    print("\nMemory")
    _section("Summary")
    _kv("MemTotal", info.get("total_human", "N/A"))
    _kv("MemAvailable", info.get("available_human", "N/A"))
    if info.get("free_kb") is not None:
        _kv("MemFree", _format_kb(info["free_kb"]))
    if not as_root:
        _kv(
            "Note",
            privilege_note(
                "DIMM table / SPD need sudo (showing meminfo only)",
                enabled=color,
            ),
        )

    if banks:
        pop_labels = [_slot_short(b.get("slot")) for b in populated_banks]
        pop_n = len(populated_banks)
        total_slots = len(banks)
        if pop_n:
            ch_line = "%d populated (%s) of %d slots" % (
                pop_n,
                "+".join(pop_labels),
                total_slots,
            )
        else:
            ch_line = "0 populated of %d slots" % total_slots
        _kv("Channels", ch_line)
        cpu_ch = _cpu_memory_channels_spec()
        if cpu_ch is not None:
            _kv("Note", f"CPU: {cpu_ch} memory channels — see lsproc")
        else:
            _kv("Note", "CPU memory channels — see lsproc")

    if banks:
        _section("DIMMs")
        _print_dimm_table(banks, has_specs)

        total_gib = 0
        for b in populated_banks:
            sz = b.get("size") or "0"
            if sz.endswith("GiB"):
                try:
                    total_gib += int(sz.replace("GiB", "").strip())
                except ValueError:
                    pass
        if total_gib or populated_banks:
            print()
            print(
                "  DIMMs: %d GiB total (%d slot%s populated)."
                % (total_gib, len(populated_banks), "s" if len(populated_banks) != 1 else "")
            )

        serial_lines = []
        for b in populated_banks:
            ser = (b.get("serial") or "").strip()
            if ser and not _is_placeholder_serial(ser):
                serial_lines.append((_slot_short(b.get("slot")), ser))
        _section("Serials")
        if serial_lines:
            for slot, ser in serial_lines:
                _kv(slot, ser)
        else:
            _kv(
                "Note",
                "unavailable (BIOS placeholder; SPD empty or unreadable — common on consumer boards)",
            )
    elif not as_root:
        _section("DIMMs")
        _kv(
            "Note",
            privilege_note("— (needs sudo for DMI Type 17 / SPD)", enabled=color),
        )
    sudo_note = sudo_full_results_note("lsram")
    if sudo_note:
        _section("Notes")
        _kv("Note", privilege_note(sudo_note, enabled=color))
    print()


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else [])

