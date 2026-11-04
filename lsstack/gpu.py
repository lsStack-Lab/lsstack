"""lsgpu: GPU details. Reads sysfs PCI; lookup in gpu_pci_id.json; optional nvidia-smi, lspci.

Run with sudo so lspci can read PCI capabilities (Link speed, Link width). Without root,
capabilities may show as <access denied> and Live link data will be missing."""

import csv
import json
import io
import os
import re
import subprocess
import sys

from lsstack.common import (
    config_dir,
    gpu_spec_key,
    load_json,
    lookup_gpu_specs,
    normalize_pci_hex,
    read_file,
    system_binary,
)

_SYS_PCI = "/sys/bus/pci/devices"
_VGA_CLASS = "0x030000"
_3D_CLASS = "0x038000"
_NVIDIA_VENDOR = "0x10de"
_AMD_VENDOR = "0x1002"


def _vendor_is(vendor: str, *codes: str) -> bool:
    v = (vendor or "").strip().lower().replace("0x", "")
    return any(v == c.lower().replace("0x", "") for c in codes)


def _pci_device_ids() -> list[tuple[str, str, str]]:
    """Yield (bus_id, vendor_hex, device_hex) for each VGA/3D PCI device."""
    result = []
    if not os.path.isdir(_SYS_PCI):
        return result
    for name in os.listdir(_SYS_PCI):
        path = os.path.join(_SYS_PCI, name)
        class_path = os.path.join(path, "class")
        cls = read_file(class_path)
        if not cls:
            continue
        cls = cls.strip()
        # VGA (0x0300) or 3D (0x0380) - class is 0x030000 or 0x038000 etc
        if not (cls == _VGA_CLASS or cls.startswith("0x030") or cls == _3D_CLASS or cls.startswith("0x038")):
            continue
        vendor = read_file(os.path.join(path, "vendor"))
        device = read_file(os.path.join(path, "device"))
        if vendor and device:
            result.append((name, vendor.strip(), device.strip()))
    return result


def _normalize_device_id(device_hex: str) -> str:
    """Normalize to 4-digit lowercase hex (strip 0x, pad)."""
    return normalize_pci_hex(device_hex) or ""


def _get_nvidia_board_part_numbers(bus_id: str) -> dict:
    """Board/GPU part numbers from `nvidia-smi -q` (not exposed via --query-gpu)."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [exe, "-q", "-i", bus_id],
            capture_output=True,
            text=True,
            timeout=8,
        )
        text = out.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not val or val.upper() == "N/A":
            continue
        if key == "Board Part Number":
            found["board_part_number"] = val
        elif key == "GPU Part Number":
            found["gpu_part_number"] = val
    return found


def _get_nvidia_power_draw(bus_id: str) -> str | None:
    """Optional separate nvidia-smi call for power draw (W); returns None if unavailable."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-i", bus_id, "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_nvidia_cuda_version(bus_id: str) -> str | None:
    """Optional separate nvidia-smi call for CUDA version; does not affect main query."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-i", bus_id, "--query-gpu=cuda_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_nvidia_smi_info(bus_id: str) -> dict | None:
    """Return dict with name, driver_version, vbios, ... or on failure {_error, stderr, returncode} so caller can show mismatch guidance."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [
                exe, "-i", bus_id,
                "--query-gpu=name,driver_version,vbios_version,pcie.link.width.current,memory.total,clocks.current.graphics,clocks.current.memory,temperature.gpu,fan.speed,uuid,serial",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            # Return error payload so we can parse stderr for mismatch guidance (single nvidia-smi call)
            return {"_error": True, "stderr": out.stderr or "", "returncode": out.returncode}
        line = out.stdout.strip()
        try:
            parts = next(csv.reader(io.StringIO(line)))
            parts = [p.strip() if p else "" for p in parts]
        except (StopIteration, csv.Error):
            parts = [p.strip() for p in line.split(",")]
        # Handle 11 cols (with uuid,serial); fallback for older / malformed CSV
        if len(parts) >= 11:
            result = {
                "name": parts[0] or None,
                "driver_version": parts[1] or None,
                "vbios_version": parts[2] or None,
                "link_width": parts[3] or None,
                "memory_total": parts[4] or None,
                "gpu_clock_mhz": parts[5] or None,
                "mem_clock_mhz": parts[6] or None,
                "temperature_gpu": parts[7] or None,
                "fan_speed": parts[8] or None,
                "uuid": parts[9] or None,
                "serial": parts[10] or None,
            }
            power = _get_nvidia_power_draw(bus_id)
            result["power_draw_w"] = power
            parts_extra = _get_nvidia_board_part_numbers(bus_id)
            result.update(parts_extra)
            return result
        if len(parts) >= 9:
            result = {
                "name": parts[0] or None,
                "driver_version": parts[1] or None,
                "vbios_version": parts[2] or None,
                "link_width": parts[3] or None,
                "memory_total": parts[4] or None,
                "gpu_clock_mhz": parts[5] or None,
                "mem_clock_mhz": parts[6] or None,
                "temperature_gpu": parts[7] or None,
                "fan_speed": parts[8] or None,
            }
            power = _get_nvidia_power_draw(bus_id)
            result["power_draw_w"] = power
            return result
        if len(parts) >= 4:
            return {"name": parts[0], "driver_version": parts[1], "memory_total": parts[2], "temperature_gpu": parts[3]}
        if len(parts) >= 2:
            return {"name": parts[0], "driver_version": parts[1]}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_nvidia_package_name() -> str | None:
    """Return package name that provides nvidia kernel module (e.g. nvidia-driver-570), or None."""
    mod_exe = system_binary("modinfo")
    dpkg_exe = system_binary("dpkg")
    if not mod_exe or not dpkg_exe:
        return None
    try:
        out = subprocess.run([mod_exe, "-n", "nvidia"], capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        path = out.stdout.strip()
        out2 = subprocess.run([dpkg_exe, "-S", path], capture_output=True, text=True, timeout=5)
        if out2.returncode != 0 or not out2.stdout.strip():
            return None
        # dpkg -S returns "package: path" or "package1, package2: path"
        line = out2.stdout.strip().split("\n")[0]
        if ":" in line:
            pkg = line.split(":")[0].strip().split(",")[0].strip()
            return pkg if pkg else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _get_nvidia_driver_version_fallback() -> str | None:
    """Return NVIDIA kernel module version from modinfo when nvidia-smi is unavailable (e.g. NVML mismatch)."""
    exe = system_binary("modinfo")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-F", "version", "nvidia"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _parse_nvidia_mismatch_guidance(stderr: str, kernel_version: str | None) -> dict | None:
    """If stderr indicates Driver/library version mismatch, return {nvml, kernel} for guidance. Else None."""
    if not stderr or "Driver/library version mismatch" not in stderr:
        return None
    m = re.search(r"NVML\s+library\s+version:\s*(\S+)", stderr, re.I)
    if not m:
        return None
    return {"nvml": m.group(1).strip(), "kernel": kernel_version or "—"}


def _maybe_link_downgrade_note(link_speed_val: str, bus_interface: str, width: int) -> None:
    """If live link speed is much lower than spec (e.g. 2.5 vs 16 GT/s), print a note."""
    m = re.search(r"(\d+\.?\d*)\s*GT/s", str(link_speed_val), re.I)
    if not m:
        return
    live_gt_s = float(m.group(1))
    spec_gt_s = {"PCIe 1.0": 2.5, "PCIe 2.0": 5, "PCIe 3.0": 8, "PCIe 4.0": 16, "PCIe 5.0": 32, "PCIe 6.0": 64}.get(bus_interface)
    if spec_gt_s and live_gt_s < spec_gt_s * 0.5:
        _kv(
            "Note",
            "link downgraded from spec — check slot/BIOS (lsstack/docs/lsgpu-link-downgrade.md)",
            width,
        )


def _print_nvidia_mismatch_guidance(nvml_version: str, kernel_version: str, width: int) -> None:
    """Print one clear block: installed vs required, cause, and copy-paste remedy."""
    w = max(width, len("Installed (kernel module)"), len("NVML (userspace library)"))
    _section("NVIDIA driver/library mismatch")
    _kv("Installed (kernel module)", kernel_version, w)
    _kv("NVML (userspace library)", nvml_version, w)
    _kv(
        "Note",
        "kernel and userspace versions differ; nvidia-smi cannot run until they match",
        w,
    )
    _kv("Remedy", "align versions, then reboot:", w)
    _kv("Note", "sudo apt update && sudo apt upgrade -y --allow-downgrades", w)
    _kv("Note", "sudo reboot", w)
    _kv("Note", "or reinstall a matching driver package, then reboot", w)


def _core_config_label(core_config: str, *, is_nvidia: bool, is_amd: bool) -> str:
    """Human label for gpu_pci_id core_config (CUDA cores, SMs, CUs, or generic)."""
    raw = (core_config or "").strip()
    if re.search(r"\bCU\b", raw, re.I):
        return "Compute units"
    try:
        n = int(float(raw.replace(",", "")))
    except ValueError:
        n = None
    if is_nvidia:
        # Bulk refresh usually stores CUDA core counts; older rows often store SM count.
        if n is not None and n >= 256:
            return "CUDA cores"
        if n is not None:
            return "SMs"
        return "CUDA cores"
    if is_amd:
        return "Compute units" if n is None else "Cores"
    return "Cores"


def _get_lspci_link_info(bus_id: str) -> dict | None:
    """Return dict with link_width, link_speed from lspci -vv -s (e.g. 'Width x16', '16 GT/s')."""
    exe = system_binary("lspci")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-vv", "-s", bus_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout:
            return None
        width = speed = None
        for line in out.stdout.splitlines():
            # Match only the main LnkSta line (not LnkSta2), e.g. "LnkSta: Speed 2.5GT/s (downgraded), Width x8 (ok)"
            if re.search(r"LnkSta:\s", line) and "LnkSta2" not in line:
                m = re.search(r"Width\s+x(\d+)", line, re.I)
                if m:
                    width = "x%s" % m.group(1)
                m = re.search(r"(\d+\.?\d*)\s*GT/s", line, re.I)
                if m:
                    speed = "%s GT/s" % m.group(1)
                break
        if width or speed:
            return {"link_width": width, "link_speed": speed}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _detect_software_renderer(lspci_name: str | None) -> bool:
    """True if device name suggests VM/software renderer (llvmpipe, virgl, swrast, etc.)."""
    if not lspci_name:
        return False
    name_lower = lspci_name.lower()
    return any(x in name_lower for x in ("llvmpipe", "virgl", "swrast", "softpipe", "vmware", "virtualbox", "virtio"))


def _device_name_from_lspci(bus_id: str) -> str | None:
    """Return first line device name from lspci -s (e.g. 'NVIDIA GeForce RTX 3080')."""
    exe = system_binary("lspci")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-s", bus_id], capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        line = out.stdout.strip().split("\n")[0]
        if ":" in line:
            return line.split(":", 1)[1].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _amd_drm_device_path(bus_id: str) -> str | None:
    """Sysfs path to DRM card device dir for this PCI BDF (…/drm/cardN/device)."""
    drm_root = os.path.join(_SYS_PCI, bus_id, "drm")
    if not os.path.isdir(drm_root):
        return None
    try:
        cards = sorted(n for n in os.listdir(drm_root) if re.match(r"card\d+$", n))
    except OSError:
        return None
    if not cards:
        return None
    # Prefer the card that has mem_info_vram_total
    for card in cards:
        dev = os.path.join(drm_root, card, "device")
        if os.path.isfile(os.path.join(dev, "mem_info_vram_total")):
            return dev
    return os.path.join(drm_root, cards[0], "device")


def _amd_hwmon_temp_c(dev_path: str) -> int | None:
    hwmon = os.path.join(dev_path, "hwmon")
    if not os.path.isdir(hwmon):
        return None
    try:
        names = sorted(os.listdir(hwmon))
    except OSError:
        return None
    for name in names:
        raw = read_file(os.path.join(hwmon, name, "temp1_input"))
        if raw and raw.strip().lstrip("-").isdigit():
            return int(raw.strip()) // 1000
    return None


def _get_amd_sysfs_live(bus_id: str) -> dict | None:
    """Live AMD fields from amdgpu sysfs (no CSV / no rocm-smi required)."""
    dev = _amd_drm_device_path(bus_id)
    if not dev or not os.path.isdir(dev):
        # Module version only
        ver = read_file("/sys/module/amdgpu/version") or None
        if not ver and not _vendor_is(
            read_file(os.path.join(_SYS_PCI, bus_id, "vendor")) or "",
            _AMD_VENDOR,
        ):
            return None
        return {
            "driver": "amdgpu",
            "driver_version": (read_file("/sys/module/amdgpu/version") or "").strip() or None,
            "source": "sysfs",
        }
    vram_total = read_file(os.path.join(dev, "mem_info_vram_total"))
    vram_used = read_file(os.path.join(dev, "mem_info_vram_used"))
    busy = read_file(os.path.join(dev, "gpu_busy_percent"))
    unique_id = read_file(os.path.join(dev, "unique_id"))
    ver = (
        read_file("/sys/module/amdgpu/version")
        or read_file(os.path.join(dev, "driver", "module", "version"))
    )
    out: dict = {
        "driver": "amdgpu",
        "driver_version": (ver or "").strip() or None,
        "source": "sysfs",
        "unique_id": (unique_id or "").strip() or None,
    }
    if vram_total and vram_total.strip().isdigit():
        out["vram_total_bytes"] = int(vram_total.strip())
        out["vram_total_gib"] = round(int(vram_total.strip()) / (1024 ** 3), 2)
    if vram_used and vram_used.strip().isdigit():
        out["vram_used_bytes"] = int(vram_used.strip())
        out["vram_used_gib"] = round(int(vram_used.strip()) / (1024 ** 3), 2)
    if busy and busy.strip().lstrip("-").isdigit():
        out["gpu_busy_percent"] = int(busy.strip())
    temp = _amd_hwmon_temp_c(dev)
    if temp is not None:
        out["temperature_gpu"] = temp
    return out


def _get_amd_rocm_smi(bus_id: str) -> dict | None:
    """Optional enrich from rocm-smi (best-effort; may be multi-GPU index based)."""
    exe = system_binary("rocm-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--showproductname", "--showmeminfo", "vram", "--showtemp", "--showuse"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        blob = (r.stdout or "") + (r.stderr or "")
        if not blob.strip():
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    # Keep raw snippet for JSON; human path prefers sysfs numbers
    return {"raw_present": True, "snippet": blob.strip().splitlines()[:12]}


def get_gpu_info() -> tuple[list[dict], dict | None]:
    """Return (list of per-GPU dicts, gpu_json or None). Each dict has bus_id, vendor, device, specs, nvidia_smi, amd_live, lspci_link, etc."""
    gpu_json = load_json("gpu_pci_id.json")
    devices = _pci_device_ids()
    result = []
    for bus_id, vendor, device in devices:
        dev_id = _normalize_device_id(device)
        specs = lookup_gpu_specs(gpu_json, vendor, device) if gpu_json else None
        nvidia_smi = None
        amd_live = None
        if _vendor_is(vendor, _NVIDIA_VENDOR):
            nvidia_smi = _get_nvidia_smi_info(bus_id)
            if nvidia_smi and not nvidia_smi.get("_error"):
                cuda = _get_nvidia_cuda_version(bus_id)
                if cuda:
                    nvidia_smi["cuda_version"] = cuda
        elif _vendor_is(vendor, _AMD_VENDOR):
            amd_live = _get_amd_sysfs_live(bus_id) or {}
            rocm = _get_amd_rocm_smi(bus_id)
            if rocm:
                amd_live["rocm_smi"] = rocm
            if not amd_live.get("driver"):
                amd_live["driver"] = "amdgpu"
        lspci_link = _get_lspci_link_info(bus_id)
        lspci_name = _device_name_from_lspci(bus_id)
        result.append({
            "bus_id": bus_id,
            "vendor": vendor,
            "device": device,
            "device_id_hex": dev_id,
            "specs": specs,
            "nvidia_smi": nvidia_smi,
            "amd_live": amd_live,
            "lspci_link": lspci_link,
            "lspci_name": lspci_name,
            "software_renderer": _detect_software_renderer(lspci_name),
        })
    return result, gpu_json


def _sysfs_link(bus_id: str) -> dict:
    """current/max link width and speed from sysfs (no lspci required)."""
    base = os.path.join(_SYS_PCI, bus_id)
    return {
        "link_width": read_file(os.path.join(base, "current_link_width")),
        "max_width": read_file(os.path.join(base, "max_link_width")),
        "link_speed": read_file(os.path.join(base, "current_link_speed")),
        "max_speed": read_file(os.path.join(base, "max_link_speed")),
    }


def _kv(label: str, value: str | None, width: int = 16) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def main(verbose: bool = False) -> None:
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.
    _ = verbose or ("--verbose" in sys.argv or "-v" in sys.argv)
    gpus, gpu_json = get_gpu_info()
    if not gpus:
        print("\nNo GPU devices found (no VGA/3D class PCI devices). Run with sudo for full detection.")
        return
    if gpu_json is None:
        print("\n  Spec database (gpu_pci_id.json) not found. Set LSSTACK_CONFIG to config dir, or run csv_to_json.")

    def _live(v):
        if v is None or (isinstance(v, str) and (not v.strip() or v.strip().upper() == "N/A")):
            return None
        return str(v).strip()

    def _fmt_clock_mhz(v):
        d = _live(v)
        if not d:
            return None
        try:
            n = int(float(str(v).strip()))
        except (ValueError, TypeError):
            return str(v)
        return f"{n / 1000:.1f} GHz" if n >= 1000 else f"{n} MHz"

    def _fmt_width(w) -> str | None:
        w = _live(w)
        if not w:
            return None
        return w if w.startswith("x") else f"x{w}"

    width = _label_width(
        "Model", "Chip", "Code name", "Generation", "TDP", "Note",
        "Size", "Type", "Bus width", "Mem clock", "Bandwidth",
        "Base", "Boost",
        "Driver", "CUDA", "VBIOS", "UUID", "Serial", "Board P/N",
        "VRAM (live)", "GPU clock", "Temperature", "Fan", "Power",
        "Spec", "Link width", "Link speed",
        "CUDA cores", "SMs", "Compute units", "Cores", "L2",
        "Display", "PCI ID",
        "Installed (kernel module)", "NVML (userspace library)", "Remedy",
    )
    for i, g in enumerate(gpus):
        print(f"\nGPU {i + 1}  ({g['bus_id']})")
        name = (
            (g.get("nvidia_smi") or {}).get("name")
            or (g.get("specs") or {}).get("model")
            or g.get("lspci_name")
            or None
        )
        if not name:
            name = f"Unknown (PCI ID {g.get('device_id_hex', '????')})"

        ns = g.get("nvidia_smi") if not (g.get("nvidia_smi") or {}).get("_error") else None
        ns_err = g.get("nvidia_smi") if (g.get("nvidia_smi") or {}).get("_error") else None
        amd = g.get("amd_live")
        lspci = g.get("lspci_link") or {}
        sysfs = _sysfs_link(g["bus_id"])
        is_nvidia = _vendor_is(str(g.get("vendor", "")), _NVIDIA_VENDOR)
        is_amd = _vendor_is(str(g.get("vendor", "")), _AMD_VENDOR)
        specs_missing = g.get("specs") is None
        s = g.get("specs") or {}
        catalog_key = gpu_spec_key(g.get("vendor"), g.get("device")) or g.get("device_id_hex") or "????"

        _section("Summary")
        _kv("Model", name, width)
        if g.get("software_renderer"):
            _kv("Note", "Software renderer / VM — not a physical GPU", width)
        if specs_missing:
            _kv(
                "Note",
                f"No curated specs for {catalog_key} (not in lab catalog). "
                "Live and PCI link data still shown. "
                "Add locally: lscatalog add gpu --from-live",
                width,
            )
        chip = s.get("gpu_chip")
        code = s.get("code_name")
        if chip:
            _kv("Chip", chip, width)
        if code and code != chip:
            _kv("Code name", code, width)
        if s.get("generation"):
            _kv("Generation", str(s["generation"]), width)
        if s.get("tdp_watts") is not None:
            _kv("TDP", f"{s['tdp_watts']} W", width)

        if not specs_missing:
            _section("Memory (spec)")
            if s.get("size_gib") is not None:
                _kv("Size", f"{s['size_gib']} GiB", width)
            if s.get("memory_type"):
                _kv("Type", str(s["memory_type"]), width)
            if s.get("bus_width_bit"):
                _kv("Bus width", f"{s['bus_width_bit']} bit", width)
            if s.get("memory_gt_s"):
                _kv("Mem clock", f"{s['memory_gt_s']} GT/s", width)
            if s.get("bandwidth_gb_s"):
                _kv("Bandwidth", f"{s['bandwidth_gb_s']} GB/s", width)

            _section("Clocks (spec)")
            if s.get("base_core_clock_mhz") is not None:
                _kv("Base", _fmt_clock_mhz(s["base_core_clock_mhz"]), width)
            if s.get("boost_core_clock_mhz") is not None:
                _kv("Boost", _fmt_clock_mhz(s["boost_core_clock_mhz"]), width)

        # Live
        _section("Live")
        if ns:
            driver_val = _live(ns.get("driver_version"))
            if not driver_val and is_nvidia:
                driver_val = _get_nvidia_driver_version_fallback()
            _kv("Driver", driver_val, width)
            if ns.get("cuda_version"):
                _kv("CUDA", _live(ns.get("cuda_version")), width)
            _kv("VBIOS", _live(ns.get("vbios_version")), width)
            _kv("UUID", _live(ns.get("uuid")), width)
            _kv("Serial", _live(ns.get("serial")), width)
            if ns.get("board_part_number"):
                _kv("Board P/N", _live(ns.get("board_part_number")), width)
            mem_live = ns.get("memory_total")
            mem_d = None
            if _live(mem_live):
                try:
                    mem_d = f"{int(float(str(mem_live).strip())) / 1024:.1f} GiB"
                except (ValueError, TypeError):
                    mem_d = str(mem_live)
            _kv("VRAM (live)", mem_d, width)
            if (
                s.get("size_gib") is not None
                and mem_d
                and abs(float(s["size_gib"]) - float(mem_d.split()[0])) > 0.5
            ):
                _kv("Note", f"spec database says {s['size_gib']} GiB; live nvidia-smi differs", width)
            _kv("GPU clock", _fmt_clock_mhz(ns.get("gpu_clock_mhz")), width)
            _kv("Mem clock", _fmt_clock_mhz(ns.get("mem_clock_mhz")), width)
            temp = _live(ns.get("temperature_gpu"))
            _kv("Temperature", f"{temp}°C" if temp else None, width)
            fan = _live(ns.get("fan_speed"))
            _kv("Fan", f"{fan}%" if fan else None, width)
            if _live(ns.get("power_draw_w")):
                _kv("Power", f"{ns['power_draw_w']} W", width)
        elif ns_err or is_nvidia:
            driver_val = _get_nvidia_driver_version_fallback() if is_nvidia else None
            if is_nvidia and driver_val:
                pkg = _get_nvidia_package_name()
                if pkg:
                    driver_val = f"{driver_val} ({pkg})"
            _kv("Driver", driver_val or "—", width)
            if ns_err:
                guidance = _parse_nvidia_mismatch_guidance(
                    ns_err.get("stderr", ""), driver_val
                )
                if guidance:
                    _print_nvidia_mismatch_guidance(guidance["nvml"], guidance["kernel"], width)
                else:
                    _kv("Note", "nvidia-smi unavailable — reboot after driver update may fix", width)
        elif is_amd:
            if amd:
                drv = amd.get("driver_version") or amd.get("driver") or "amdgpu"
                if amd.get("driver_version") and amd.get("driver"):
                    drv = f"{amd['driver']} {amd['driver_version']}"
                _kv("Driver", drv, width)
                if amd.get("vram_total_gib") is not None:
                    used = amd.get("vram_used_gib")
                    if used is not None:
                        _kv("VRAM (live)", f"{used} / {amd['vram_total_gib']} GiB", width)
                    else:
                        _kv("VRAM (live)", f"{amd['vram_total_gib']} GiB", width)
                if not s:
                    _kv("Note", "AMD device ID not in gpu_pci_id database", width)
                if amd.get("temperature_gpu") is not None:
                    _kv("Temperature", f"{amd['temperature_gpu']}°C", width)
                if amd.get("gpu_busy_percent") is not None:
                    _kv("GPU busy", f"{amd['gpu_busy_percent']}%", width)
                if amd.get("unique_id"):
                    _kv("Unique ID", amd["unique_id"], width)
                if amd.get("rocm_smi", {}).get("raw_present"):
                    _kv("Note", "rocm-smi present (enrichment available)", width)
            else:
                _kv("Driver", "amdgpu (live sysfs unavailable)", width)
                if not s:
                    _kv("Note", "AMD device ID not in gpu_pci_id database", width)

        # PCIe
        _section("PCIe")
        spec_bus = s.get("bus_interface")
        spec_lanes = s.get("pcie_lanes")
        if spec_bus:
            _kv("Spec", f"{spec_bus}" + (f", x{spec_lanes}" if spec_lanes else ""), width)
        cur_w = _fmt_width((ns or {}).get("link_width") if ns else None) or _fmt_width(sysfs.get("link_width")) or _fmt_width(lspci.get("link_width"))
        max_w = _fmt_width(sysfs.get("max_width"))
        cur_s = _live(sysfs.get("link_speed")) or _live(lspci.get("link_speed"))
        max_s = _live(sysfs.get("max_speed"))
        link_line = cur_w or "—"
        if max_w and max_w != cur_w:
            link_line = f"{cur_w or '—'} (device max {max_w})"
        _kv("Link width", link_line, width)
        speed_line = cur_s or "—"
        if max_s and cur_s and max_s != cur_s:
            speed_line = f"{cur_s} (max {max_s})"
        _kv("Link speed", speed_line, width)
        if cur_s and spec_bus:
            _maybe_link_downgrade_note(cur_s, spec_bus, width)
        if spec_lanes and cur_w:
            try:
                cw = int(str(cur_w).lstrip("x"))
                if cw < int(spec_lanes):
                    _kv(
                        "Note",
                        f"running {cur_w}, spec expects x{spec_lanes} — see lstopology / PCIe checks",
                        width,
                    )
            except ValueError:
                pass

        _section("Compute (spec)" if not specs_missing else "Identity")
        if s.get("core_config"):
            label = _core_config_label(
                str(s["core_config"]), is_nvidia=is_nvidia, is_amd=is_amd
            )
            _kv(label, str(s["core_config"]), width)
        if s.get("l2_cache_mib"):
            try:
                if float(s["l2_cache_mib"]) > 0:
                    _kv("L2", f"{s['l2_cache_mib']} MiB", width)
            except (TypeError, ValueError):
                _kv("L2", f"{s['l2_cache_mib']} MiB", width)
        if s.get("display"):
            _kv("Display", str(s["display"]), width)
        _kv("PCI ID", f"{g.get('vendor')}:{g.get('device')} ({g.get('device_id_hex')})", width)
        print()


def _gpus_to_json(gpus: list[dict]) -> list[dict]:
    """Convert gpus list to JSON-serializable structure."""
    out = []
    for g in gpus:
        ns = g.get("nvidia_smi")
        s = g.get("specs")
        lspci = g.get("lspci_link")
        obj = {
            "bus_id": g["bus_id"],
            "vendor": g["vendor"],
            "device": g["device"],
            "device_id_hex": g["device_id_hex"],
            "model": (ns or {}).get("name") or (s or {}).get("model") or g.get("lspci_name") or f"Unknown (PCI ID {g.get('device_id_hex', '????')})",
            "software_renderer": g.get("software_renderer", False),
            "specs": dict(s) if s else None,
            "live": {},
        }
        if s is None:
            key = gpu_spec_key(g.get("vendor"), g.get("device")) or g.get("device_id_hex") or "????"
            obj["specs_note"] = (
                f"No curated specs for {key} (not in lab catalog). "
                "Live and PCI link data still shown."
            )
        amd = g.get("amd_live")
        if ns and not ns.get("_error"):
            obj["live"] = {
                "driver": ns.get("driver_version"),
                "cuda_version": ns.get("cuda_version"),
                "vbios": ns.get("vbios_version"),
                "uuid": ns.get("uuid"),
                "serial": ns.get("serial"),
                "board_part_number": ns.get("board_part_number"),
                "gpu_part_number": ns.get("gpu_part_number"),
                "link_width": ns.get("link_width"),
                "link_speed": (lspci or {}).get("link_speed"),
                "memory_mib": ns.get("memory_total"),
                "gpu_clock_mhz": ns.get("gpu_clock_mhz"),
                "mem_clock_mhz": ns.get("mem_clock_mhz"),
                "temperature": ns.get("temperature_gpu"),
                "fan_speed": ns.get("fan_speed"),
                "power_draw_w": ns.get("power_draw_w"),
            }
        elif amd:
            obj["live"] = {
                "driver": amd.get("driver"),
                "driver_version": amd.get("driver_version"),
                "vram_total_gib": amd.get("vram_total_gib"),
                "vram_used_gib": amd.get("vram_used_gib"),
                "temperature": amd.get("temperature_gpu"),
                "gpu_busy_percent": amd.get("gpu_busy_percent"),
                "unique_id": amd.get("unique_id"),
                "source": amd.get("source"),
                "rocm_smi": bool((amd.get("rocm_smi") or {}).get("raw_present")),
                "link_width": (lspci or {}).get("link_width"),
                "link_speed": (lspci or {}).get("link_speed"),
            }
        elif lspci:
            obj["live"] = {
                "link_width": lspci.get("link_width"),
                "link_speed": lspci.get("link_speed"),
            }
        out.append(obj)
    return out


def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="List GPU details from sysfs + JSON + nvidia-smi/lspci.",
        epilog=(
            "Peak TFLOPS are omitted from specs (vendor marketing peaks). "
            "Use VRAM, link, TDP, and lsstack balance instead; see docs/specs.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--validate", action="store_true", help="Print raw nvidia-smi and lspci output for each GPU to verify parsing.")
    ap.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Accepted for compatibility; human output is always full (use --json for lean data).",
    )
    args = ap.parse_args()
    if args.validate:
        gpus, _ = get_gpu_info()
        for i, g in enumerate(gpus):
            bus_id = g["bus_id"]
            print(f"\n=== GPU {i + 1}: {bus_id} ===")
            # Always run nvidia-smi for this GPU so we see raw output or failure
            print("nvidia-smi (same query as lsgpu):")
            exe = system_binary("nvidia-smi")
            if exe:
                out = subprocess.run(
                    [exe, "-i", bus_id,
                     "--query-gpu=name,driver_version,vbios_version,pcie.link.width.current,memory.total,clocks.current.graphics,clocks.current.memory,temperature.gpu,fan.speed",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                print(out.stdout if out.stdout else "(no output)")
                if out.stderr:
                    print("stderr:", out.stderr)
                if out.returncode != 0:
                    print("(nvidia-smi exit code:", out.returncode, ")")
            else:
                print("(nvidia-smi not found in PATH)")
            # Always run lspci and show LnkSta/LnkCap, or full output if none
            print("lspci -vv -s (LnkSta for link width/speed):")
            lspci_exe = system_binary("lspci")
            out = subprocess.run([lspci_exe or "lspci", "-vv", "-s", bus_id], capture_output=True, text=True, timeout=5)
            raw = (out.stdout or "").strip()
            lnk_lines = [line for line in raw.splitlines() if "LnkSta" in line or "LnkCap" in line]
            if lnk_lines:
                for line in lnk_lines:
                    print(" ", line.strip())
            else:
                print(" (no LnkSta/LnkCap lines in lspci output)")
                if raw:
                    for line in raw.splitlines()[:25]:
                        print(" ", line)
                    if len(raw.splitlines()) > 25:
                        print(" ...")
        return
    if args.json:
        gpus, _ = get_gpu_info()
        print(json.dumps(_gpus_to_json(gpus), indent=2))
        return
    main(verbose=args.verbose)


if __name__ == "__main__":
    _main()
