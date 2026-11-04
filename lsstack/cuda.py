"""lscuda: CUDA driver API / toolkit (nvcc) versions. No cross-subsystem imports."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.color import color_enabled, green, red, yellow
from lsstack.common import read_file, system_binary


def _nvidia_smi_cuda() -> str | None:
    """CUDA version reported by the driver (max API), from nvidia-smi banner or query."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=cuda_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            val = r.stdout.strip().splitlines()[0].strip()
            if val and val.upper() not in ("N/A", "[N/A]"):
                return val
        r2 = subprocess.run([exe], capture_output=True, text=True, timeout=5)
        blob = (r2.stdout or "") + (r2.stderr or "")
        m = re.search(r"CUDA Version:\s*([\d.]+)", blob)
        if m:
            return m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _nvcc_version() -> dict | None:
    exe = system_binary("nvcc")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        text = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 and not text:
            return None
        release = None
        build = None
        m = re.search(r"release\s+([\d.]+)", text, re.I)
        if m:
            release = m.group(1)
        m2 = re.search(r"Build\s+(\S+)", text)
        if m2:
            build = m2.group(1)
        m3 = re.search(r"V([\d.]+)", text)
        full = m3.group(1) if m3 else release
        return {
            "path": exe,
            "release": release,
            "version": full,
            "build": build,
            "raw_line": text.strip().splitlines()[-1] if text.strip() else None,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _toolkit_version_json() -> dict | None:
    for path in (
        "/usr/local/cuda/version.json",
        "/usr/local/cuda/version.txt",
    ):
        if not os.path.isfile(path):
            continue
        if path.endswith(".json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    if "cuda" in data and isinstance(data["cuda"], dict):
                        return {
                            "path": path,
                            "version": data["cuda"].get("version")
                            or data["cuda"].get("cuda_version"),
                        }
                    ver = data.get("version") or data.get("cuda_version")
                    if ver:
                        return {"path": path, "version": ver}
            except (OSError, json.JSONDecodeError):
                continue
        else:
            raw = read_file(path)
            if raw:
                return {"path": path, "version": raw.splitlines()[0].strip()}
    return None


def _cuda_home() -> dict | None:
    """CUDA_HOME env and/or /usr/local/cuda realpath when present."""
    env = (os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH") or "").strip() or None
    link = "/usr/local/cuda"
    real = None
    if os.path.exists(link):
        try:
            real = os.path.realpath(link)
        except OSError:
            real = link
    if not env and not real:
        return None
    return {"CUDA_HOME": env, "usr_local_cuda": real}


def _ver_tuple(s: str | None) -> tuple[int, ...] | None:
    if not s:
        return None
    try:
        return tuple(int(x) for x in str(s).split(".")[:2])
    except ValueError:
        return None


def get_cuda_info() -> dict:
    driver_cuda = _nvidia_smi_cuda()
    nvcc = _nvcc_version()
    toolkit = _toolkit_version_json()
    home = _cuda_home()

    status = "unknown"
    note = None
    dmaj = _ver_tuple(driver_cuda)
    tmaj = _ver_tuple((nvcc or {}).get("release") if nvcc else None)

    if driver_cuda and nvcc and nvcc.get("release") and dmaj and tmaj:
        if tmaj > dmaj:
            status = "toolkit_newer"
            note = (
                f"nvcc toolkit {nvcc['release']} is newer than driver CUDA {driver_cuda} "
                "— upgrade driver or use an older toolkit"
            )
        else:
            status = "ok"
            note = (
                f"Driver supports CUDA {driver_cuda}; "
                f"toolkit {nvcc['release']} is within that ceiling"
            )
    elif driver_cuda and not nvcc:
        status = "no_toolkit"
        note = (
            f"Driver supports CUDA {driver_cuda}; "
            "install CUDA toolkit (nvcc) for compile-time builds"
        )
    elif nvcc and not driver_cuda:
        status = "no_driver_cuda"
        note = "nvcc present but nvidia-smi CUDA version unavailable (driver issue?)"
    elif not driver_cuda and not nvcc:
        status = "absent"
        note = "No driver CUDA (nvidia-smi) and no nvcc — see lsdriver / install toolkit"
    else:
        note = f"Driver CUDA {driver_cuda}; toolkit {nvcc.get('release') if nvcc else '—'}"
        status = "ok" if driver_cuda else "unknown"

    return {
        "driver_cuda_version": driver_cuda,
        "nvcc": nvcc,
        "toolkit_file": toolkit,
        "cuda_home": home,
        "status": status,
        "compatibility_note": note,
        "frameworks_note": (
            "PyTorch/TF wheels pin their own CUDA — match wheel CUDA ≤ driver CUDA"
        ),
    }


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _status_display(info: dict, *, color: bool) -> str:
    note = info.get("compatibility_note") or info.get("status") or "—"
    st = info.get("status")
    if st == "ok":
        return green(note, enabled=color)
    if st == "toolkit_newer":
        return red(note, enabled=color)
    if st in ("no_toolkit", "no_driver_cuda", "absent", "unknown"):
        return yellow(note, enabled=color)
    return note


def _help_text() -> str:
    return """lscuda — CUDA driver API vs toolkit for lab / workstation builds

What
  Reports the CUDA version the NVIDIA driver supports (nvidia-smi), the installed
  nvcc toolkit (if any), and whether the toolkit is within the driver’s ceiling.

Why
  Lab builders install toolkits and PyTorch wheels that must not exceed the
  driver’s CUDA API. Catch “toolkit newer than driver” before compile/runtime fails.

How
  Read Summary/Status first. Green = toolkit ≤ driver CUDA. Red = toolkit newer.
  Driver packages → lsdriver. GPU inventory → lsgpu. AMD/ROCm is separate (later).
  Lean data → --json.

Usage: lscuda [options]
  --json, -j       JSON
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def print_cuda_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_cuda_info()
    nvcc = info.get("nvcc") or {}
    tf = info.get("toolkit_file") or {}
    home = info.get("cuda_home") or {}

    labels = [
        "Status", "Driver CUDA", "nvcc", "nvcc build", "version file",
        "CUDA_HOME", "/usr/local/cuda", "Frameworks", "Note",
    ]
    w = _label_width(*labels)

    print("\nCUDA")

    _section("Summary")
    _kv("Status", _status_display(info, color=color), w)

    _section("Driver")
    _kv("Driver CUDA", info.get("driver_cuda_version") or "— (nvidia-smi)", w)

    _section("Toolkit")
    if nvcc:
        _kv(
            "nvcc",
            f"{nvcc.get('version') or nvcc.get('release')} ({nvcc.get('path')})",
            w,
        )
        if nvcc.get("build"):
            _kv("nvcc build", nvcc["build"], w)
    else:
        _kv("nvcc", "not found", w)
    if tf.get("version"):
        _kv("version file", f"{tf['version']} ({tf.get('path')})", w)
    if home.get("CUDA_HOME"):
        _kv("CUDA_HOME", home["CUDA_HOME"], w)
    if home.get("usr_local_cuda"):
        _kv("/usr/local/cuda", home["usr_local_cuda"], w)

    _section("Notes")
    _kv("Frameworks", info.get("frameworks_note"), w)
    _kv("Note", "GPU driver match → lsdriver", w)
    _kv("Note", "GPU inventory → lsgpu", w)
    _kv("Note", "AMD/ROCm support planned after this cluster", w)
    _kv("Note", "color: auto on TTY; disable with -nc, --no-color or NO_COLOR=1", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_cuda_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_cuda_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
