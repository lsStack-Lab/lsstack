"""Dependency probe + low-friction install hints (Ubuntu/apt).

Does not invent packages: only maps known full-path binaries to distro packages
or a short NVIDIA/CUDA/ROCm hint. Never installs unless --deps-install is explicit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

from lsstack.common import SYSTEM_BINARIES, system_binary

# tier: base (usually present) | enricher (apt) | vendor (NVIDIA/CUDA/ROCm, not apt one-liner)
_DEPENDENCY_META: dict[str, dict[str, Any]] = {
    "lsblk": {
        "tier": "base",
        "apt": "util-linux",
        "provides": "block device listing (lsdisk)",
    },
    "df": {
        "tier": "base",
        "apt": "coreutils",
        "provides": "filesystem free space",
    },
    "lscpu": {
        "tier": "base",
        "apt": "util-linux",
        "provides": "CPU topology helpers",
    },
    "nproc": {
        "tier": "base",
        "apt": "coreutils",
        "provides": "CPU count",
    },
    "ip": {
        "tier": "base",
        "apt": "iproute2",
        "provides": "interface addresses",
    },
    "modinfo": {
        "tier": "base",
        "apt": "kmod",
        "provides": "kernel module metadata",
    },
    "dpkg": {
        "tier": "base",
        "apt": "dpkg",
        "provides": "Debian package queries (optional on non-Debian)",
    },
    "dmidecode": {
        "tier": "enricher",
        "apt": "dmidecode",
        "provides": "lsmobo deep fields (ports/onboard/chassis serials); optional",
    },
    "smartctl": {
        "tier": "enricher",
        "apt": "smartmontools",
        "provides": "SATA/SAS SMART health (NVMe often covered via sysfs)",
    },
    "ethtool": {
        "tier": "enricher",
        "apt": "ethtool",
        "provides": "permanent NIC MAC",
    },
    "lspci": {
        "tier": "enricher",
        "apt": "pciutils",
        "provides": "friendly PCI device names",
    },
    "sensors": {
        "tier": "enricher",
        "apt": "lm-sensors",
        "provides": "lm-sensors temps (sysfs hwmon usually enough)",
    },
    "mdadm": {
        "tier": "enricher",
        "apt": "mdadm",
        "provides": "software RAID detail",
    },
    "nvidia-smi": {
        "tier": "vendor",
        "apt": None,
        "provides": "GPU live metrics, UUID/serial, topo, ECC",
        "install_hint": (
            "Install an NVIDIA driver that provides /usr/bin/nvidia-smi "
            "(e.g. ubuntu-drivers, your site CUDA/driver stack, or apt nvidia-driver-XXX)."
        ),
    },
    "nvcc": {
        "tier": "vendor",
        "apt": None,
        "provides": "CUDA toolkit compiler version (lscuda)",
        "install_hint": (
            "Optional: install CUDA toolkit (nvcc), e.g. from NVIDIA CUDA packages "
            "or /usr/local/cuda — not required for most lsstack checks."
        ),
    },
    "rocm-smi": {
        "tier": "vendor",
        "apt": None,
        "provides": "optional AMD live enrich (lsdriver / lsgpu); sysfs is enough",
        "install_hint": (
            "Optional: install ROCm so /opt/rocm/bin/rocm-smi or /usr/bin/rocm-smi exists. "
            "Not required; amdgpu sysfs covers inventory. Not auto-installed."
        ),
    },
}


def probe_dependencies() -> dict[str, Any]:
    """Status for every known external binary."""
    items: list[dict[str, Any]] = []
    for name in sorted(SYSTEM_BINARIES.keys()):
        meta = _DEPENDENCY_META.get(name, {})
        path = system_binary(name)
        status = "ok" if path else "missing"
        items.append({
            "name": name,
            "status": status,
            "path": path,
            "tier": meta.get("tier", "enricher"),
            "apt": meta.get("apt"),
            "provides": meta.get("provides"),
            "install_hint": meta.get("install_hint"),
            "candidates": list(SYSTEM_BINARIES.get(name) or ()),
        })
    missing = [i for i in items if i["status"] == "missing"]
    apt_pkgs = sorted({
        i["apt"] for i in missing
        if i.get("apt") and i.get("tier") in ("base", "enricher")
    })
    vendor_hints = [
        i["install_hint"] for i in missing
        if i.get("tier") == "vendor" and i.get("install_hint")
    ]
    apt_cmd = None
    if apt_pkgs:
        apt_cmd = "sudo apt update && sudo apt install -y " + " ".join(apt_pkgs)
    return {
        "items": items,
        "missing": [i["name"] for i in missing],
        "apt_packages": apt_pkgs,
        "apt_install_command": apt_cmd,
        "vendor_hints": vendor_hints,
    }


def format_deps_report(deps: dict[str, Any] | None = None) -> str:
    """Human-readable dependency report with install one-liner."""
    deps = deps or probe_dependencies()
    lines: list[str] = []
    lines.append("lsstack external dependencies (full-path probes)")
    lines.append("")
    lines.append(f"  {'Name':<14} {'Status':<8} {'Tier':<10} Path / note")
    lines.append(f"  {'-'*14} {'-'*8} {'-'*10} {'-'*40}")
    for i in deps.get("items") or []:
        path = i.get("path") or "—"
        note = ""
        if i["status"] == "missing" and i.get("apt"):
            note = f"  → apt: {i['apt']}"
        elif i["status"] == "missing" and i.get("install_hint"):
            note = "  → see vendor hint below"
        lines.append(
            f"  {i['name']:<14} {i['status']:<8} {str(i.get('tier') or ''):<10} {path}{note}"
        )
    lines.append("")
    missing = deps.get("missing") or []
    if not missing:
        lines.append("  All known tools present.")
    else:
        lines.append(f"  Missing ({len(missing)}): {', '.join(missing)}")
        if deps.get("apt_install_command"):
            lines.append("")
            lines.append("  Install missing apt packages (Ubuntu/Debian):")
            lines.append(f"    {deps['apt_install_command']}")
            lines.append("")
            lines.append("  Or:  lsstack --deps-install")
        for hint in deps.get("vendor_hints") or []:
            lines.append("")
            lines.append(f"  Vendor: {hint}")
    lines.append("")
    lines.append("  Policy: install from distro / NVIDIA / ROCm only, not pip/npm. See docs/SECURITY.md.")
    return "\n".join(lines) + "\n"


def missing_enricher_footer(deps: dict[str, Any] | None = None) -> str | None:
    """One-liner for end of human report when enrichers are missing."""
    deps = deps or probe_dependencies()
    apt_missing = [
        i for i in (deps.get("items") or [])
        if i["status"] == "missing" and i.get("tier") == "enricher" and i.get("apt")
    ]
    vendor_missing = [
        i for i in (deps.get("items") or [])
        if i["status"] == "missing" and i.get("tier") == "vendor"
    ]
    if not apt_missing and not vendor_missing:
        return None
    parts = []
    if apt_missing and deps.get("apt_install_command"):
        pkgs = " ".join(sorted({i["apt"] for i in apt_missing if i.get("apt")}))
        parts.append(f"Missing enrichers ({', '.join(i['name'] for i in apt_missing)}). Install: sudo apt install -y {pkgs}")
    if vendor_missing:
        names = ", ".join(i["name"] for i in vendor_missing)
        parts.append(f"Missing vendor tools ({names}) — see: lsstack --deps")
    parts.append("Details: lsstack --deps")
    return "  ".join(parts)


def run_apt_install(deps: dict[str, Any] | None = None) -> int:
    """Install missing apt packages. Returns process exit code (0 ok)."""
    deps = deps or probe_dependencies()
    pkgs = deps.get("apt_packages") or []
    if not pkgs:
        print("No missing apt-backed packages to install.", file=sys.stderr)
        for hint in deps.get("vendor_hints") or []:
            print(f"Note: {hint}", file=sys.stderr)
        return 0

    apt = "/usr/bin/apt"
    if not os.path.isfile(apt):
        apt = shutil.which("apt") or ""
    if not apt or not os.path.isfile(apt):
        print("apt not found; run on Ubuntu/Debian or install packages manually:", file=sys.stderr)
        print(" ", deps.get("apt_install_command"), file=sys.stderr)
        return 1

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("Need root for apt install. Re-run with sudo, e.g.:", file=sys.stderr)
        print(f"  sudo {sys.argv[0] if sys.argv else 'lsstack'} --deps-install", file=sys.stderr)
        print("Or:", file=sys.stderr)
        print(f"  {deps.get('apt_install_command')}", file=sys.stderr)
        return 1

    print(f"Installing: {' '.join(pkgs)}", file=sys.stderr)
    # Non-interactive; user explicitly passed --deps-install
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        r1 = subprocess.run([apt, "update"], env=env, timeout=300)
        if r1.returncode != 0:
            return r1.returncode
        r2 = subprocess.run([apt, "install", "-y", *pkgs], env=env, timeout=600)
        return r2.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"apt failed: {e}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    """CLI: python3 -m lsstack deps [--install]."""
    import argparse

    from lsstack import __version__

    ap = argparse.ArgumentParser(description="Probe lsstack external dependencies.")
    ap.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"lsStack {__version__}",
    )
    ap.add_argument(
        "--install",
        action="store_true",
        help="Install missing apt packages (same as lsstack --deps-install; needs root).",
    )
    args = ap.parse_args(argv)
    deps = probe_dependencies()
    print(format_deps_report(deps))
    if args.install:
        sys.exit(run_apt_install(deps))
