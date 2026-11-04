"""lsdriver: GPU kernel driver / package versions (NVIDIA + AMD live; sysfs + optional tools)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.color import color_enabled, green, privilege_note, red, yellow
from lsstack.common import read_file, system_binary

_SYS_PCI = "/sys/bus/pci/devices"
_AMD_VENDOR = "0x1002"
_NVIDIA_VENDOR = "0x10de"


def _modinfo_field(module: str, field: str) -> str | None:
    exe = system_binary("modinfo")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-F", field, module], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _pci_has_vendor_gpu(vendor_hex: str) -> bool:
    """True if a VGA/3D PCI device with this vendor is present."""
    want = vendor_hex.lower().replace("0x", "")
    if not os.path.isdir(_SYS_PCI):
        return False
    try:
        names = os.listdir(_SYS_PCI)
    except OSError:
        return False
    for name in names:
        path = os.path.join(_SYS_PCI, name)
        cls = (read_file(os.path.join(path, "class")) or "").strip().lower()
        if not (cls.startswith("0x030") or cls.startswith("0x038")):
            continue
        vend = (read_file(os.path.join(path, "vendor")) or "").strip().lower().replace("0x", "")
        if vend == want:
            return True
    return False


def _nvidia_smi_driver() -> str | None:
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0].strip()
        blob = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"Driver Version:\s*([\d.]+)", blob)
        if m:
            return m.group(1)
        if "Driver/library version mismatch" in blob:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _nvidia_smi_mismatch() -> bool:
    exe = system_binary("nvidia-smi")
    if not exe:
        return False
    try:
        r = subprocess.run([exe], capture_output=True, text=True, timeout=5)
        blob = (r.stdout or "") + (r.stderr or "")
        return "Driver/library version mismatch" in blob
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _dpkg_packages(prefixes: tuple[str, ...], exact: tuple[str, ...] = ()) -> list[dict]:
    exe = system_binary("dpkg")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    out: list[dict] = []
    for line in r.stdout.splitlines():
        if not line.startswith("ii"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        name, ver = parts[1], parts[2]
        if name in exact or any(name.startswith(p) for p in prefixes):
            out.append({"name": name, "version": ver})
    return out[:12]


def _loaded_modules(prefix: str) -> list[str]:
    raw = read_file("/proc/modules")
    if not raw:
        return []
    names = []
    for line in raw.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith(prefix):
            names.append(name)
    return sorted(names)


def _nvrm_short(banner: str | None) -> str | None:
    if not banner:
        return None
    line = banner.strip().splitlines()[0].strip()
    m = re.search(r"NVIDIA UNIX.*?Kernel Module\s+([\d.]+)", line, re.IGNORECASE)
    if m:
        return f"NVIDIA Kernel Module {m.group(1)}"
    m2 = re.search(r"Kernel Module\s+([\d.]+)", line, re.IGNORECASE)
    if m2:
        return f"NVIDIA Kernel Module {m2.group(1)}"
    if len(line) > 90:
        return line[:87] + "…"
    return line


def _collect_nvidia() -> dict | None:
    kernel_ver = (
        read_file("/sys/module/nvidia/version")
        or _modinfo_field("nvidia", "version")
    )
    proc_banner = read_file("/proc/driver/nvidia/version")
    smi_ver = _nvidia_smi_driver()
    present = bool(
        kernel_ver or smi_ver or os.path.isdir("/sys/module/nvidia")
        or _pci_has_vendor_gpu(_NVIDIA_VENDOR)
    )
    if not present:
        return None

    mismatch = bool(kernel_ver and smi_ver is None and _nvidia_smi_mismatch())
    if mismatch:
        status, detail = "mismatch", (
            "Driver/library version mismatch — reboot after driver install, "
            "or align userspace/kernel packages"
        )
    elif kernel_ver and smi_ver and kernel_ver == smi_ver:
        status, detail = "match", "kernel module and nvidia-smi versions match"
    elif kernel_ver and smi_ver:
        status, detail = "differ", (
            f"versions differ (module {kernel_ver} vs smi {smi_ver})"
        )
    elif kernel_ver and not smi_ver:
        status, detail = "module_only", (
            "kernel module present; nvidia-smi unavailable or mismatched"
        )
    elif smi_ver:
        status, detail = "smi_only", "nvidia-smi present; kernel module version unread"
    else:
        status, detail = "pci_only", "NVIDIA GPU PCI present; module not loaded"

    return {
        "vendor": "nvidia",
        "kernel_module_version": kernel_ver,
        "nvidia_smi_version": smi_ver,
        "proc_banner": (proc_banner.splitlines()[0] if proc_banner else None),
        "nvrm_short": _nvrm_short(proc_banner),
        "mismatch": mismatch,
        "status": status,
        "status_detail": detail,
        "modules_loaded": _loaded_modules("nvidia"),
        "packages": _dpkg_packages(("nvidia-driver",), ("nvidia-dkms", "cuda-drivers")),
        "modinfo_filename": _modinfo_field("nvidia", "filename"),
    }


def _rocm_smi_driver() -> str | None:
    exe = system_binary("rocm-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--showdriverversion"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        blob = (r.stdout or "") + (r.stderr or "")
        # Common: "Driver version: 6.x.x" or bare version line
        m = re.search(r"[Dd]river\s+version[:\s]+([\w.\-]+)", blob)
        if m:
            return m.group(1)
        for line in blob.splitlines():
            line = line.strip()
            if re.match(r"^[\d.]+$", line):
                return line
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _collect_amd() -> dict | None:
    kernel_ver = (
        read_file("/sys/module/amdgpu/version")
        or _modinfo_field("amdgpu", "version")
    )
    rocm = _rocm_smi_driver()
    mods = _loaded_modules("amd")
    # Prefer amdgpu-named modules in display
    mods = [m for m in mods if m.startswith(("amdgpu", "amdkfd", "amd_"))] or mods
    present = bool(
        kernel_ver
        or os.path.isdir("/sys/module/amdgpu")
        or "amdgpu" in mods
        or _pci_has_vendor_gpu(_AMD_VENDOR)
        or rocm
    )
    if not present:
        return None

    if kernel_ver and "amdgpu" in (mods or ["amdgpu"] if os.path.isdir("/sys/module/amdgpu") else []):
        status, detail = "loaded", f"amdgpu loaded ({kernel_ver})"
    elif kernel_ver or os.path.isdir("/sys/module/amdgpu") or "amdgpu" in mods:
        status, detail = "loaded", "amdgpu kernel module loaded"
    elif _pci_has_vendor_gpu(_AMD_VENDOR):
        status, detail = "pci_only", "AMD GPU PCI present; amdgpu not loaded"
    else:
        status, detail = "userspace", "ROCm/userspace tools present; module state unclear"

    return {
        "vendor": "amd",
        "kernel_module_version": kernel_ver,
        "rocm_smi_driver": rocm,
        "status": status,
        "status_detail": detail,
        "modules_loaded": mods if mods else _loaded_modules("amdgpu"),
        "packages": _dpkg_packages(("amdgpu", "rocm-"), ("amdgpu-dkms",)),
        "modinfo_filename": _modinfo_field("amdgpu", "filename"),
    }


def get_driver_info() -> dict:
    """NVIDIA and/or AMD GPU driver status."""
    nvidia = _collect_nvidia()
    amd = _collect_amd()
    backends = []
    if nvidia:
        backends.append("nvidia")
    if amd:
        backends.append("amd")

    out: dict = {
        "backends": backends,
        "nvidia": nvidia,
        "amd": amd,
    }

    # Legacy flat NVIDIA fields when NVIDIA present (older callers / habit)
    if nvidia:
        out["vendor"] = "nvidia" if backends == ["nvidia"] else "+".join(backends)
        out["kernel_module_version"] = nvidia.get("kernel_module_version")
        out["nvidia_smi_version"] = nvidia.get("nvidia_smi_version")
        out["proc_banner"] = nvidia.get("proc_banner")
        out["nvrm_short"] = nvidia.get("nvrm_short")
        out["mismatch"] = nvidia.get("mismatch")
        out["status"] = nvidia.get("status")
        out["status_detail"] = nvidia.get("status_detail")
        out["modules_loaded"] = nvidia.get("modules_loaded")
        out["packages"] = nvidia.get("packages")
        out["modinfo_filename"] = nvidia.get("modinfo_filename")
    elif amd:
        out["vendor"] = "amd"
        out["status"] = amd.get("status")
        out["status_detail"] = amd.get("status_detail")
        out["kernel_module_version"] = amd.get("kernel_module_version")
        out["modules_loaded"] = amd.get("modules_loaded")
        out["packages"] = amd.get("packages")
        out["modinfo_filename"] = amd.get("modinfo_filename")
    else:
        out["vendor"] = None
        out["status"] = "none"
        out["status_detail"] = "No NVIDIA or AMD GPU driver detected"

    return out


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _nvidia_status_display(block: dict, *, color: bool) -> str:
    detail = block.get("status_detail") or block.get("status") or "—"
    st = block.get("status")
    if st == "match":
        return green(detail, enabled=color)
    if st in ("mismatch", "differ"):
        return red(detail, enabled=color)
    if st in ("module_only", "smi_only", "pci_only"):
        return yellow(detail, enabled=color)
    return detail


def _amd_status_display(block: dict, *, color: bool) -> str:
    detail = block.get("status_detail") or block.get("status") or "—"
    st = block.get("status")
    if st == "loaded":
        return green(detail, enabled=color)
    if st in ("pci_only", "userspace"):
        return yellow(detail, enabled=color)
    return detail


def _help_text() -> str:
    return """lsdriver — GPU kernel driver status for lab / workstation builds

What
  Reports NVIDIA (module vs nvidia-smi match/mismatch) and/or AMD (amdgpu
  loaded, optional rocm-smi) driver state, loaded modules, and packages.

Why
  After driver installs, NVIDIA’s #1 lab failure is kernel ≠ userspace.
  AMD checks that amdgpu is loaded before chasing ROCm or display issues.

How
  NVIDIA: Match → green; mismatch → reboot or align packages.
  AMD: loaded → green; PCI-only → load amdgpu. Inventory → lsgpu.
  CUDA toolkit → lscuda. Spec DB → lsgpu (gpu_pci_id).
  Lean data → --json.

Usage: lsdriver [options]
  --json, -j       JSON
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def _print_nvidia_block(n: dict, *, color: bool, w: int) -> None:
    _section("NVIDIA")
    _kv("Status", _nvidia_status_display(n, color=color), w)
    _kv("Kernel module", n.get("kernel_module_version") or "—", w)
    _kv("nvidia-smi", n.get("nvidia_smi_version") or "—", w)
    if n.get("nvrm_short"):
        _kv("NVRM", n["nvrm_short"], w)
    if n.get("modinfo_filename"):
        _kv("Module path", n["modinfo_filename"], w)
    mods = n.get("modules_loaded") or []
    if mods:
        _kv("Loaded", ", ".join(mods), w)
    pkgs = n.get("packages") or []
    if pkgs:
        _kv("Packages", ", ".join(f"{p['name']}={p['version']}" for p in pkgs), w)
    if n.get("status") in ("mismatch", "differ"):
        _kv(
            "Note",
            privilege_note(
                "reboot after driver install, or align kernel/userspace packages",
                enabled=color,
            ),
            w,
        )


def _print_amd_block(a: dict, *, color: bool, w: int) -> None:
    _section("AMD")
    _kv("Status", _amd_status_display(a, color=color), w)
    _kv("Kernel module", a.get("kernel_module_version") or "—", w)
    _kv("rocm-smi", a.get("rocm_smi_driver") or "— (optional)", w)
    if a.get("modinfo_filename"):
        _kv("Module path", a["modinfo_filename"], w)
    mods = a.get("modules_loaded") or []
    if mods:
        _kv("Loaded", ", ".join(mods), w)
    pkgs = a.get("packages") or []
    if pkgs:
        _kv("Packages", ", ".join(f"{p['name']}={p['version']}" for p in pkgs), w)
    if a.get("status") == "pci_only":
        _kv("Note", yellow("load amdgpu (or install amdgpu-dkms) for a live driver", enabled=color), w)


def print_driver_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_driver_info()
    nvidia = info.get("nvidia")
    amd = info.get("amd")
    labels = [
        "Backends", "Status", "Kernel module", "nvidia-smi", "rocm-smi", "NVRM",
        "Module path", "Loaded", "Packages", "Note",
    ]
    w = _label_width(*labels)

    print("\nGPU driver")

    _section("Summary")
    backends = info.get("backends") or []
    if backends:
        _kv("Backends", ", ".join(backends), w)
    else:
        _kv("Status", yellow(info.get("status_detail") or "none detected", enabled=color), w)

    if nvidia:
        _print_nvidia_block(nvidia, color=color, w=w)
    if amd:
        _print_amd_block(amd, color=color, w=w)

    _section("Notes")
    if not backends:
        _kv("Note", "PCI GPU listing → lsgpu", w)
    else:
        _kv("Note", "GPU inventory → lsgpu", w)
    if nvidia:
        _kv("Note", "CUDA driver/toolkit ceiling → lscuda", w)
    if amd and not nvidia:
        _kv("Note", "ROCm toolkit checks planned later (not lscuda)", w)
    _kv("Note", "GPU specs (vendor:device) → lsgpu / gpu_pci_id.json", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_driver_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_driver_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
