"""lsdisk: block devices (NVMe, SATA, …). Size/partitions via lsblk; SMART when permitted.
NVMe PCIe link/temp from sysfs. Controller deep-dive → lsnvme."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.color import (
    color_enabled,
    looks_like_privilege_note,
    privilege_note,
    red,
    yellow,
)
from lsstack.common import read_file, sudo_full_results_note, system_binary


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run command; return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (-1, "", "")


def _format_size_bytes(bytes_val: int) -> str:
    """Human-readable size using binary units (GiB), matching lsnvme."""
    if bytes_val <= 0:
        return "0 bytes"
    if bytes_val >= 1073741824:
        return "%.2f GiB" % (bytes_val / 1073741824)
    if bytes_val >= 1048576:
        return "%.2f MiB" % (bytes_val / 1048576)
    if bytes_val >= 1024:
        return "%.2f KiB" % (bytes_val / 1024)
    return "%d bytes" % bytes_val


def _get_mount_free_kb() -> dict[str, int]:
    """Run df; return dict device -> available (1K blocks). Keys are e.g. /dev/sda1."""
    exe = system_binary("df")
    if not exe:
        return {}
    code, out, _ = _run([exe, "-P"])
    if code != 0 or not out:
        return {}
    result: dict[str, int] = {}
    for line in out.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("/dev/"):
            try:
                result[parts[0]] = int(parts[3])
            except ValueError:
                pass
    return result


def _get_disk_names() -> list[str]:
    """List block device names that are disks (type=disk)."""
    exe = system_binary("lsblk")
    if not exe:
        return []
    code, out, _ = _run([exe, "-d", "-o", "NAME,TYPE", "-n"])
    if code != 0 or not out:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[1].strip() == "disk":
            names.append(parts[0].strip())
    return names


def _nvme_ctrl(device: str) -> str | None:
    m = re.match(r"(nvme\d+)", device)
    return m.group(1) if m else None


def _get_model(device: str) -> str:
    exe = system_binary("smartctl")
    if exe:
        _, out, _ = _run([exe, "-i", "/dev/" + device], timeout=15)
        for line in out.splitlines():
            if "Device Model:" in line or "Model Number:" in line:
                return line.split(":", 1)[1].strip()
    exe = system_binary("lsblk")
    if exe:
        code, out, _ = _run([exe, "-d", "-n", "-o", "MODEL", "/dev/" + device])
        if code == 0 and out.strip():
            return out.strip()
    ctrl = _nvme_ctrl(device)
    if ctrl:
        m = read_file(f"/sys/class/nvme/{ctrl}/model")
        if m:
            return m.strip()
    for path in (
        f"/sys/block/{device}/device/model",
        f"/sys/class/block/{device}/device/model",
    ):
        m = read_file(path)
        if m:
            return m.strip()
    return "N/A"


def _get_serial(device: str) -> str | None:
    ctrl = _nvme_ctrl(device)
    if ctrl:
        s = read_file(f"/sys/class/nvme/{ctrl}/serial")
        if s and s.strip():
            return s.strip()
    exe = system_binary("smartctl")
    if exe:
        _, out, _ = _run([exe, "-i", "/dev/" + device], timeout=15)
        for line in out.splitlines():
            if line.lower().startswith("serial number:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    return val
    for path in (
        f"/sys/block/{device}/device/serial",
        f"/sys/class/block/{device}/device/serial",
    ):
        s = read_file(path)
        if s and s.strip():
            return s.strip()
    return None


def _get_size(device: str) -> str:
    exe = system_binary("lsblk")
    if exe:
        code, out, _ = _run([exe, "-b", "-d", "-n", "-o", "SIZE", "/dev/" + device])
        if code == 0 and out:
            try:
                return _format_size_bytes(int(out.strip().splitlines()[0].strip()))
            except (ValueError, IndexError):
                pass
    # sysfs: size is number of 512-byte sectors
    raw = read_file(f"/sys/block/{device}/size")
    if raw and raw.isdigit():
        return _format_size_bytes(int(raw) * 512)
    return "0 bytes"


def _get_transport(device: str) -> str | None:
    exe = system_binary("lsblk")
    if not exe:
        return "nvme" if device.startswith("nvme") else None
    code, out, _ = _run([exe, "-d", "-n", "-o", "TRAN", "/dev/" + device])
    if code == 0 and out.strip():
        return out.strip().lower()
    if device.startswith("nvme"):
        return "nvme"
    return None


def _get_drive_type(device: str) -> str:
    if device.startswith("nvme"):
        return "NVMe"
    exe = system_binary("lsblk")
    if exe:
        code, out, _ = _run([exe, "-d", "-n", "-o", "ROTA,TRAN", "/dev/" + device])
        if code == 0 and out:
            parts = out.split()
            rota = parts[0] if parts else ""
            tran = parts[1].lower() if len(parts) > 1 else ""
            if tran == "nvme" or device.startswith("nvme"):
                return "NVMe"
            if rota == "0":
                return "SSD"
            if rota == "1":
                return "HDD"
    exe = system_binary("smartctl")
    if not exe:
        return "Unknown"
    _, out, _ = _run([exe, "-i", "/dev/" + device], timeout=15)
    if "Rotation Rate: Solid State Device" in out or "Solid State Device" in out:
        return "SSD"
    return "HDD"


def _nvme_pci_link(device: str) -> dict | None:
    """For nvme0n1 → sysfs PCI current_link_speed / current_link_width."""
    ctrl = _nvme_ctrl(device)
    if not ctrl:
        return None
    for candidate in (
        f"/sys/class/nvme/{ctrl}/device",
        f"/sys/block/{device}/device",
    ):
        if not os.path.exists(candidate):
            continue
        pci = os.path.realpath(candidate)
        cur = pci
        for _ in range(6):
            speed = read_file(os.path.join(cur, "current_link_speed"))
            width = read_file(os.path.join(cur, "current_link_width"))
            if speed or width:
                return {
                    "link_speed": (speed or "").strip() or None,
                    "link_width": (width or "").strip() or None,
                    "pci_path": cur,
                }
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return None


def _smartctl_targets(device: str) -> list[str]:
    """Prefer NVMe controller chardev for SMART; fall back to namespace/block."""
    targets = []
    ctrl = _nvme_ctrl(device)
    if ctrl:
        targets.append("/dev/" + ctrl)
    targets.append("/dev/" + device)
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _parse_health(blob: str) -> str | None:
    m = re.search(r"overall-health[^:]*:\s*(.+)", blob, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_nvme_smart_attrs(blob: str) -> dict:
    """Parse NVMe SMART/Health Information lines from smartctl -A/-x."""
    out: dict = {}
    patterns = {
        "critical_warning": r"Critical Warning:\s*(0x[0-9a-fA-F]+|\d+)",
        "available_spare_pct": r"Available Spare:\s*(\d+)\s*%",
        "available_spare_threshold_pct": r"Available Spare Threshold:\s*(\d+)\s*%",
        "percentage_used": r"Percentage Used:\s*(\d+)\s*%",
        "power_on_hours": r"Power On Hours:\s*([\d,]+)",
        "data_units_written": r"Data Units Written:\s*([\d,]+)",
        "media_errors": r"Media and Data Integrity Errors:\s*([\d,]+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, blob, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).replace(",", "")
        if key == "critical_warning":
            try:
                out[key] = int(raw, 0)
            except ValueError:
                out[key] = raw
        else:
            try:
                out[key] = int(raw)
            except ValueError:
                out[key] = raw
    if out:
        out["protocol"] = "nvme"
    return out


def _ata_attr_raw(blob: str, name: str) -> int | None:
    """RAW_VALUE for an ATA SMART attribute by name (last integer on the line)."""
    for line in blob.splitlines():
        if name not in line:
            continue
        # Skip header
        if "ATTRIBUTE_NAME" in line:
            continue
        nums = re.findall(r"\d+", line.replace(",", ""))
        if not nums:
            continue
        try:
            return int(nums[-1])
        except ValueError:
            return None
    return None


def _parse_ata_smart_attrs(blob: str) -> dict:
    """SATA/ATA wear siblings from smartctl -A attribute table."""
    if "Reallocated_Sector" not in blob and "Current_Pending_Sector" not in blob:
        # Still allow Power_On_Hours-only ATA
        if "Power_On_Hours" not in blob or "SMART/Health Information (NVMe" in blob:
            return {}
    out: dict = {"protocol": "ata"}
    mapping = {
        "reallocated_sectors": "Reallocated_Sector",
        "pending_sectors": "Current_Pending_Sector",
        "offline_uncorrectable": "Offline_Uncorrectable",
        "reported_uncorrect": "Reported_Uncorrect",
        "power_on_hours": "Power_On_Hours",
    }
    for key, needle in mapping.items():
        val = _ata_attr_raw(blob, needle)
        if val is not None:
            out[key] = val
    # Drop protocol-only empty
    if len(out) == 1:
        return {}
    return out


def _format_wear_line(wear: dict) -> str | None:
    """Human one-liner for NVMe or ATA wear attrs."""
    bits: list[str] = []
    proto = wear.get("protocol")
    if proto == "ata" or wear.get("reallocated_sectors") is not None:
        if wear.get("reallocated_sectors") is not None:
            bits.append(f"{wear['reallocated_sectors']} realloc")
        if wear.get("pending_sectors") is not None:
            bits.append(f"{wear['pending_sectors']} pending")
        unc = wear.get("offline_uncorrectable")
        if unc is None:
            unc = wear.get("reported_uncorrect")
        if unc is not None:
            bits.append(f"{unc} uncorrect")
        hours = wear.get("power_on_hours")
        if isinstance(hours, int):
            if hours >= 1000:
                bits.append(f"{hours / 1000:.1f} kh powered")
            else:
                bits.append(f"{hours} h powered")
        return " · ".join(bits) if bits else None

    if wear.get("percentage_used") is not None:
        bits.append(f"{wear['percentage_used']}% used")
    if wear.get("available_spare_pct") is not None:
        spare = f"spare {wear['available_spare_pct']}%"
        thr = wear.get("available_spare_threshold_pct")
        if isinstance(thr, int):
            spare += f" (thr {thr}%)"
        bits.append(spare)
    hours = wear.get("power_on_hours")
    if isinstance(hours, int):
        if hours >= 1000:
            bits.append(f"{hours / 1000:.1f} kh powered")
        else:
            bits.append(f"{hours} h powered")
    if wear.get("media_errors") is not None:
        n = wear["media_errors"]
        bits.append(f"{n} media err" if n != 1 else "1 media err")
    cw = wear.get("critical_warning")
    if isinstance(cw, int) and cw != 0:
        bits.append(f"critical warn 0x{cw:02x}")
    elif isinstance(cw, str) and cw not in ("0", "0x0", "0x00"):
        bits.append(f"critical warn {cw}")
    return " · ".join(bits) if bits else None


def _wear_attention(wear: dict | None) -> str | None:
    """'warn' or 'fail' when wear attrs deserve color; else None."""
    if not wear:
        return None
    for key in ("reallocated_sectors", "pending_sectors", "offline_uncorrectable", "reported_uncorrect"):
        n = wear.get(key)
        if isinstance(n, int) and n > 0:
            return "fail"
    media = wear.get("media_errors")
    if isinstance(media, int) and media > 0:
        return "fail"
    cw = wear.get("critical_warning")
    if isinstance(cw, int) and cw != 0:
        return "fail"
    spare = wear.get("available_spare_pct")
    thr = wear.get("available_spare_threshold_pct")
    if isinstance(spare, int) and isinstance(thr, int) and spare <= thr:
        return "fail"
    if isinstance(spare, int) and spare < 100:
        return "warn"
    used = wear.get("percentage_used")
    if isinstance(used, int) and used >= 90:
        return "warn"
    return None


def _get_smart_bundle(device: str) -> dict:
    """One smartctl -H -A call → health + wear. Partial-friendly on permission errors."""
    empty = {
        "health": "Unknown",
        "health_note": None,
        "wear": None,
        "wear_line": None,
        "wear_note": None,
        "note": None,
    }
    exe = system_binary("smartctl")
    if not exe:
        empty["health_note"] = "install smartmontools from distro for SMART health"
        empty["wear_note"] = "install smartmontools for wear details"
        empty["note"] = empty["health_note"]
        return empty

    last_err = ""
    for target in _smartctl_targets(device):
        code, out, err = _run([exe, "-H", "-A", target], timeout=20)
        blob = (out + "\n" + err).strip()
        last_err = err or last_err
        if "Permission denied" in blob:
            empty["health_note"] = "needs sudo + smartctl"
            empty["wear_note"] = "needs sudo + smartctl"
            empty["note"] = "needs sudo + smartctl"
            return empty
        if "No such device" in blob or "Unable to detect" in blob:
            continue

        health = _parse_health(blob) or "Unknown"
        wear = _parse_nvme_smart_attrs(blob) or _parse_ata_smart_attrs(blob) or None
        wear_line = _format_wear_line(wear) if wear else None
        note = None
        health_note = None
        wear_note = None
        if health == "Unknown" and code != 0 and not out:
            continue
        return {
            "health": health,
            "health_note": health_note,
            "wear": wear,
            "wear_line": wear_line,
            "wear_note": wear_note,
            "note": note,
        }

    if "Permission denied" in last_err:
        empty["health_note"] = "needs sudo + smartctl"
        empty["wear_note"] = "needs sudo + smartctl"
        empty["note"] = "needs sudo + smartctl"
    return empty


def _get_lsblk_identity(device: str) -> dict:
    """WWN / rotational / discard max from lsblk (lab inventory cues)."""
    exe = system_binary("lsblk")
    out_id: dict = {
        "wwn": None,
        "rota": None,
        "discard_max": None,
    }
    if not exe:
        return out_id
    code, out, _ = _run(
        [exe, "-d", "-n", "-o", "WWN,ROTA,DISC-MAX", "/dev/" + device],
        timeout=5,
    )
    if code != 0 or not out.strip():
        return out_id
    # WWN can contain spaces rarely; DISC-MAX is last token like 2G / 0B
    line = out.strip().splitlines()[0].strip()
    parts = line.split()
    if not parts:
        return out_id
    # Heuristic: last is DISC-MAX, second-to-last ROTA (0/1), rest WWN
    if len(parts) >= 3 and parts[-2] in ("0", "1"):
        out_id["discard_max"] = parts[-1]
        out_id["rota"] = int(parts[-2])
        wwn = " ".join(parts[:-2]).strip()
        if wwn and wwn not in ("-", ""):
            out_id["wwn"] = wwn
    elif len(parts) == 2 and parts[0] in ("0", "1"):
        out_id["rota"] = int(parts[0])
        out_id["discard_max"] = parts[1]
    elif len(parts) == 1 and parts[0] in ("0", "1"):
        out_id["rota"] = int(parts[0])
    return out_id


def _health_display(health: str | None, *, note: str | None, color: bool) -> str | None:
    if not health:
        return None
    low = health.lower()
    if "pass" in low:
        from lsstack.color import green
        return green(health, enabled=color)
    if "fail" in low:
        return red(health, enabled=color)
    if health == "Unknown" and looks_like_privilege_note(note):
        return privilege_note(health, enabled=color)
    if "unknown" in low:
        return yellow(health, enabled=color)
    return health


def _nvme_hwmon_temp_c(device: str) -> str | None:
    """NVMe composite temp from sysfs hwmon (millidegrees)."""
    ctrl = _nvme_ctrl(device)
    if not ctrl:
        return None
    base = f"/sys/class/nvme/{ctrl}"
    candidates = []
    hwmon_root = os.path.join(base, "hwmon0")
    if os.path.isdir(hwmon_root):
        candidates.append(hwmon_root)
    device_hwmon = os.path.join(base, "device", "hwmon")
    if os.path.isdir(device_hwmon):
        try:
            for name in sorted(os.listdir(device_hwmon)):
                candidates.append(os.path.join(device_hwmon, name))
        except OSError:
            pass
    for hdir in candidates:
        raw = read_file(os.path.join(hdir, "temp1_input"))
        if raw and raw.isdigit():
            return "%d°C" % (int(raw) // 1000)
    return None


def _get_temperature(device: str) -> str:
    if device.startswith("nvme"):
        t = _nvme_hwmon_temp_c(device)
        if t:
            return t
    exe = system_binary("smartctl")
    if not exe:
        return "N/A"
    for target in _smartctl_targets(device):
        _, out, _ = _run([exe, "-A", target], timeout=15)
        for line in out.splitlines():
            if "Temperature_Celsius" in line:
                parts = line.split()
                if len(parts) >= 10 and parts[9].isdigit():
                    return "%s°C" % parts[9]
            m = re.search(r"Temperature:\s+(\d+)\s*C", line, re.I)
            if m:
                return "%s°C" % m.group(1)
    return "N/A"


def _get_raid_status(device: str) -> str | None:
    """Return RAID level string, or None if not an md device / not RAID."""
    exe = system_binary("mdadm")
    if not exe:
        return None
    code, out, _ = _run([exe, "--detail", "/dev/" + device], timeout=5)
    if code != 0:
        return None
    m = re.search(r"Raid Level\s*:\s*(.+)", out, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _get_partitions(device: str, free_kb: dict[str, int] | None = None) -> list[tuple[str, str, str, str]]:
    """Return list of (name, size, mountpoint, free) for children of device."""
    exe = system_binary("lsblk")
    if not exe:
        return []
    code, out, _ = _run([exe, "-n", "-o", "NAME,SIZE,MOUNTPOINT", "/dev/" + device])
    if code != 0 or not out:
        return []
    free_kb = free_kb or {}
    result = []
    for line in out.strip().splitlines():
        parts = line.split(None, 2)
        if not parts or parts[0] == device:
            continue
        name = parts[0].lstrip(" \t├└│─")
        size = parts[1] if len(parts) > 1 else ""
        mount = (parts[2] if len(parts) > 2 else "") or "Not Mounted"
        dev = "/dev/" + name
        if dev in free_kb:
            free_str = _format_size_bytes(free_kb[dev] * 1024)
        else:
            free_str = "—" if mount == "Not Mounted" else "N/A"
        result.append((name, size, mount, free_str))
    return result


def get_disk_info() -> list[dict]:
    """Return list of disk dicts for human/--json output."""
    names = _get_disk_names()
    free_kb = _get_mount_free_kb()
    result = []
    for device in names:
        link = _nvme_pci_link(device) if device.startswith("nvme") else None
        smart = _get_smart_bundle(device)
        ident = _get_lsblk_identity(device)
        result.append({
            "device": device,
            "model": _get_model(device),
            "type": _get_drive_type(device),
            "transport": _get_transport(device),
            "size": _get_size(device),
            "serial": _get_serial(device),
            "wwn": ident.get("wwn"),
            "rota": ident.get("rota"),
            "discard_max": ident.get("discard_max"),
            "health": smart.get("health"),
            "health_note": smart.get("health_note"),
            "wear": smart.get("wear"),
            "wear_line": smart.get("wear_line"),
            "wear_note": smart.get("wear_note"),
            "note": smart.get("note"),
            "temperature": _get_temperature(device),
            "raid_status": _get_raid_status(device),
            "pcie_link_speed": (link or {}).get("link_speed"),
            "pcie_link_width": (link or {}).get("link_width"),
            "partitions": [
                {"name": n, "size": s, "mount": m, "free": f}
                for n, s, m, f in _get_partitions(device, free_kb)
            ],
        })
    return result


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _help_text() -> str:
    return """lsdisk — block devices (NVMe, SATA, …) for lab / workstation builds

What
  Lists disks with model, size, serial, WWN, Health/Wear (SMART when permitted),
  temperature, PCIe link (NVMe), and partitions with free space.

Why
  Builders need to confirm which drive is which, whether SMART health/wear is
  quiet, and how full the filesystems are — before cloning a lab image or
  trusting a used SSD.

How
  Runs without root for inventory + partitions. SMART Health/Wear often need
  sudo + smartctl (amber notes). Controller deep-dive → lsnvme. Lean data → --json.

Usage: lsdisk [options]
  --json, -j       JSON (disks + partitions + wear attrs)
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.

    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    if not system_binary("lsblk"):
        print(
            "Error: lsblk not found at /usr/bin/lsblk. Install from distro (util-linux).",
            file=sys.stderr,
        )
        sys.exit(1)

    disks = get_disk_info()

    if "--json" in args or "-j" in args:
        print(json.dumps({"disks": disks}, indent=2))
        return

    color = color_enabled(args)
    print("\nDisks")
    if not disks:
        _section("Summary")
        _kv("Note", "no block devices (type=disk) found")
        print()
        return

    sudo_bits = sum(
        1
        for d in disks
        if looks_like_privilege_note(d.get("note") or d.get("health_note") or d.get("wear_note"))
    )
    _section("Summary")
    _kv("Disks", str(len(disks)))
    if sudo_bits:
        _kv(
            "Note",
            privilege_note(
                f"{sudo_bits} disk(s) have SMART details that need sudo + smartctl",
                enabled=color,
            ),
        )

    labels = [
        "Disks", "Model", "Type", "Transport", "Size", "Serial", "WWN", "Rotational",
        "Discard", "Health", "Wear", "Note", "Temperature", "RAID", "PCIe link",
    ]
    for d in disks:
        labels.extend(p["name"] for p in d.get("partitions") or [])
    w = _label_width(*labels)

    for d in disks:
        _section(d["device"])
        _kv("Model", d.get("model"), w)
        _kv("Type", d.get("type"), w)
        tran = d.get("transport")
        if tran and (d.get("type") or "").upper() != tran.upper():
            _kv("Transport", tran, w)
        elif tran and d.get("type") != "NVMe":
            _kv("Transport", tran, w)
        _kv("Size", d.get("size"), w)
        _kv("Serial", d.get("serial"), w)
        if d.get("wwn"):
            _kv("WWN", d["wwn"], w)
        if d.get("rota") is not None:
            _kv("Rotational", "yes (HDD)" if d["rota"] == 1 else "no (SSD/NVMe)", w)
        if d.get("discard_max") and d.get("discard_max") not in ("0B", "0", "-"):
            _kv("Discard", d["discard_max"], w)
        _kv(
            "Health",
            _health_display(
                d.get("health"),
                note=d.get("health_note") or d.get("note"),
                color=color,
            ),
            w,
        )
        if d.get("wear_line"):
            wear_txt = d["wear_line"]
            attn = _wear_attention(d.get("wear"))
            if attn == "fail":
                wear_txt = red(wear_txt, enabled=color)
            elif attn == "warn":
                wear_txt = yellow(wear_txt, enabled=color)
            _kv("Wear", wear_txt, w)
        elif looks_like_privilege_note(d.get("wear_note") or d.get("note")):
            _kv("Wear", privilege_note("— (needs sudo + smartctl)", enabled=color), w)
        note = d.get("note") or d.get("health_note") or d.get("wear_note")
        if note:
            if looks_like_privilege_note(note):
                note = privilege_note(note, enabled=color)
            _kv("Note", note, w)
        temp = d.get("temperature")
        if temp and temp != "N/A":
            _kv("Temperature", temp, w)
        if d.get("raid_status"):
            _kv("RAID", d["raid_status"], w)
        if d.get("pcie_link_speed") or d.get("pcie_link_width"):
            width = d.get("pcie_link_width")
            link = f"x{width}" if width and not str(width).startswith("x") else (width or "—")
            _kv("PCIe link", f"{link}, {d.get('pcie_link_speed') or '—'}", w)
        if (d.get("type") or "").upper() == "NVMe" or (d.get("transport") or "") == "nvme":
            _kv("Note", "controller detail → lsnvme", w)

        parts = d.get("partitions") or []
        if parts:
            _section("Partitions")
            for p in parts:
                _kv(
                    p["name"],
                    f"{p['size']}  {p['mount']}  free: {p['free']}",
                    w,
                )
        else:
            _kv("Partitions", "none", w)

    sudo_note = sudo_full_results_note("lsdisk")
    if sudo_note:
        _section("Notes")
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    print()


if __name__ == "__main__":
    main()
