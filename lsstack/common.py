"""Shared helpers: config paths, JSON overlay, DMI cleanup, PCI/CPU lookup, system binaries."""

import json
import os
import re

# Explicit full paths for external binaries. We never invoke by name (PATH) to avoid
# substitution attacks. Install these from your distro only (e.g. apt install dmidecode).
# Do not use pip/npm or unknown sources for these tools.
# Public catalog (name → candidate full paths). Prefer distro-installed locations only.
SYSTEM_BINARIES = {
    "dmidecode": ("/usr/sbin/dmidecode", "/usr/bin/dmidecode"),
    "smartctl": ("/usr/sbin/smartctl", "/usr/bin/smartctl"),
    "lsblk": ("/usr/bin/lsblk",),
    "df": ("/usr/bin/df",),
    "mdadm": ("/usr/sbin/mdadm", "/usr/bin/mdadm"),
    "nvidia-smi": ("/usr/bin/nvidia-smi",),
    "rocm-smi": ("/usr/bin/rocm-smi", "/opt/rocm/bin/rocm-smi"),
    "lscpu": ("/usr/bin/lscpu",),
    "nproc": ("/usr/bin/nproc",),
    "sensors": ("/usr/bin/sensors",),
    "modinfo": ("/usr/sbin/modinfo", "/usr/bin/modinfo"),
    "dpkg": ("/usr/bin/dpkg",),
    "lspci": ("/usr/sbin/lspci", "/usr/bin/lspci"),
    "ip": ("/usr/sbin/ip", "/usr/bin/ip", "/bin/ip"),
    "nvcc": ("/usr/local/cuda/bin/nvcc", "/usr/bin/nvcc"),
    "ethtool": ("/usr/sbin/ethtool", "/usr/bin/ethtool"),
}


def _nvcc_from_cuda_home() -> str | None:
    """$CUDA_HOME/bin/nvcc (or CUDA_PATH) when that path is an executable file."""
    root = (os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH") or "").strip()
    if not root or not os.path.isabs(root):
        return None
    path = os.path.join(root, "bin", "nvcc")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def system_binary(name: str) -> str | None:
    """Return full path to a system binary if it exists. Prefer distro-installed paths only."""
    if name == "nvcc":
        env_nvcc = _nvcc_from_cuda_home()
        if env_nvcc:
            return env_nvcc
    candidates = SYSTEM_BINARIES.get(name)
    if not candidates:
        return None
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def read_file(path: str, default: str | None = None) -> str | None:
    """Read first line or full file from path (e.g. /proc, /sys). Returns default if missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip() or default
    except OSError:
        return default


_DMI_ID = "/sys/class/dmi/id"


def read_dmi_id(*names: str) -> dict[str, str | None]:
    """Read one or more /sys/class/dmi/id attributes. Missing/unreadable → None."""
    if not names:
        names = (
            "bios_vendor", "bios_version", "bios_date", "bios_release",
            "board_vendor", "board_name", "board_version", "board_serial", "board_asset_tag",
            "sys_vendor", "product_name", "product_version", "product_serial",
            "product_uuid", "product_family", "product_sku",
            "chassis_vendor", "chassis_type", "chassis_version", "chassis_serial", "chassis_asset_tag",
        )
    return {n: read_file(os.path.join(_DMI_ID, n)) for n in names}


# SMBIOS OEM placeholders — treat as unset for human/JSON cleanup
_DMI_PLACEHOLDERS = frozenset({
    "",
    "default string",
    "default",
    "to be filled by o.e.m.",
    "to be filled by oem.",
    "system product name",
    "system manufacturer",
    "system version",
    "system serial number",
    "chassis manufacture",
    "chassis manufacturer",
    "none",
    "n/a",
    "na",
    "not specified",
    "not available",
    "unknown",
    "o.e.m.",
    "oem",
})

# SMBIOS Type 3 chassis_type codes (common subset + recent)
_CHASSIS_TYPES = {
    1: "Other",
    2: "Unknown",
    3: "Desktop",
    4: "Low Profile Desktop",
    5: "Pizza Box",
    6: "Mini Tower",
    7: "Tower",
    8: "Portable",
    9: "Laptop",
    10: "Notebook",
    11: "Hand Held",
    12: "Docking Station",
    13: "All in One",
    14: "Sub Notebook",
    15: "Space-saving",
    16: "Lunch Box",
    17: "Main Server Chassis",
    18: "Expansion Chassis",
    19: "SubChassis",
    20: "Bus Expansion Chassis",
    21: "Peripheral Chassis",
    22: "RAID Chassis",
    23: "Rack Mount Chassis",
    24: "Sealed-case PC",
    25: "Multi-system Chassis",
    26: "Compact PCI",
    27: "Advanced TCA",
    28: "Blade",
    29: "Blade Enclosure",
    30: "Tablet",
    31: "Convertible",
    32: "Detachable",
    33: "IoT Gateway",
    34: "Embedded PC",
    35: "Mini PC",
    36: "Stick PC",
}


def is_dmi_placeholder(value: str | None) -> bool:
    """True when a DMI string is an OEM filler rather than real inventory data."""
    if value is None:
        return True
    t = value.strip().lower()
    if t in _DMI_PLACEHOLDERS:
        return True
    # ASUS / AMI often use "Default string" variants
    if t.startswith("default string"):
        return True
    return False


def dmi_or_none(value: str | None) -> str | None:
    """Return value, or None if missing/placeholder."""
    if is_dmi_placeholder(value):
        return None
    return value.strip() if value else None


def chassis_type_name(code: str | int | None) -> str | None:
    """Map SMBIOS chassis_type integer (or digit string) to a label."""
    if code is None:
        return None
    try:
        n = int(str(code).strip())
    except (TypeError, ValueError):
        return str(code).strip() or None
    return _CHASSIS_TYPES.get(n, f"Type {n}")


def effective_home() -> str:
    """Home directory for lsstack user data (~/.lsstack, ~/.lsstack.conf).

    When running as root via sudo, prefer ``SUDO_USER``'s home so lab inventory /
    catalog overlays stay with the human operator (not /root/.lsstack).
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sudo_user = (os.environ.get("SUDO_USER") or "").strip()
        if sudo_user and sudo_user != "root":
            try:
                import pwd

                return pwd.getpwnam(sudo_user).pw_dir
            except (KeyError, ImportError, OSError):
                pass
    return os.path.expanduser("~")


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# python3 -m lsstack <subsystem> → public command name
_LS_COMMANDS = {
    "proc": "lsproc",
    "gpu": "lsgpu",
    "memory": "lsram",
    "disk": "lsdisk",
    "nvme": "lsnvme",
    "network": "lsnetwork",
    "power": "lspower",
    "thermal": "lsthermal",
    "lswatch": "lswatch",
    "lslab": "lslab",
    "catalog": "lscatalog",
    "slots": "lsslots",
    "slot": "lsslots",
    "topology": "lstopology",
    "mobo": "lsmobo",
    "bios": "lsbios",
    "driver": "lsdriver",
    "cuda": "lscuda",
    "iommu": "lsiommu",
    "virt": "lsvirt",
    "report": "lsstack",
    "deps": "lsstack",
}


def ls_command_name(command: str | None = None) -> str:
    """Public ls* name for this process (wrapper or ``python3 -m lsstack <sub>``)."""
    if command:
        return command
    import sys

    base = os.path.basename(sys.argv[0] or "")
    if base.startswith("ls"):
        return base
    if len(sys.argv) >= 2 and sys.argv[1] in _LS_COMMANDS:
        return _LS_COMMANDS[sys.argv[1]]
    return "lsstack"


def sudo_full_results_note(command: str | None = None) -> str | None:
    """PATH-safe sudo line for commands that can need root. None if already root."""
    if is_root():
        return None
    cmd = ls_command_name(command)
    return f'sudo "$(command -v {cmd})"  for full results'


def want_notes(args: list[str] | None) -> bool:
    """False when the caller asked to omit the Notes footer (lsstack aggregator)."""
    return "--no-notes" not in (args or [])


def print_version() -> None:
    from lsstack import __version__

    print(f"lsStack {__version__}")


def conf_path() -> str:
    return os.path.join(effective_home(), ".lsstack.conf")


def default_catalog_dir() -> str:
    return os.path.join(effective_home(), ".lsstack", "catalog")


def conf_value(key: str) -> str | None:
    """Read key=value from ~/.lsstack.conf (first match; SUDO_USER-aware)."""
    path = conf_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    val = v.strip().strip('"').strip("'")
                    return val or None
    except OSError:
        return None
    return None


def config_dir():
    """Directory containing generated JSON (e.g. gpu_pci_id.json). Prefer ../config/ from package root."""
    return os.environ.get("LSSTACK_CONFIG", os.path.join(os.path.dirname(__file__), "..", "config"))


def catalog_dir() -> str:
    """Lab overlay catalog directory (local JSON fragments; overlay wins at load).

    Order: LSSTACK_CATALOG, catalog_dir= in ~/.lsstack.conf, else ~/.lsstack/catalog
    (SUDO_USER-aware under sudo).
    """
    env = os.environ.get("LSSTACK_CATALOG")
    if env:
        return os.path.expanduser(env)
    conf = conf_value("catalog_dir")
    if conf:
        return os.path.expanduser(conf)
    return default_catalog_dir()


def _read_json_file(path: str):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _deep_merge_dict(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _merge_ram_specs(base: list, overlay: list) -> list:
    """Overlay rows replace base rows with the same vendor+part_number; extras append."""
    def _key(row: dict) -> tuple[str, str]:
        return (
            (row.get("vendor") or row.get("Vendor") or "").strip().lower(),
            (row.get("part_number") or row.get("Part#") or "").strip().upper(),
        )

    out: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for row in base:
        if not isinstance(row, dict):
            continue
        k = _key(row)
        if k not in out:
            order.append(k)
        out[k] = row
    for row in overlay:
        if not isinstance(row, dict):
            continue
        k = _key(row)
        if k not in out:
            order.append(k)
        out[k] = row
    return [out[k] for k in order]


def _merge_catalog(name: str, base, overlay):
    """Merge lab overlay onto shipped config. Overlay wins on conflict."""
    if overlay is None:
        return base
    if base is None:
        return overlay
    if name in ("gpu_pci_id.json", "nic_pci_id.json", "psu_roster.json") and isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        out.update(overlay)
        return out
    if name == "ram_specs.json" and isinstance(base, list) and isinstance(overlay, list):
        return _merge_ram_specs(base, overlay)
    if name == "cpu_specs_wkstn.json" and isinstance(base, dict) and isinstance(overlay, dict):
        return _deep_merge_dict(base, overlay)
    if isinstance(base, dict) and isinstance(overlay, dict):
        return _deep_merge_dict(base, overlay)
    return overlay


def load_json(name):
    """Load JSON from config dir, then merge ~/.lsstack/catalog overlay (if any).

    Overlay files: <catalog_dir>/<name> or <name> with .overlay.json suffix
    (e.g. gpu_pci_id.overlay.json). Lab overlay wins on the same key.
    """
    base = _read_json_file(os.path.join(config_dir(), name))
    cdir = catalog_dir()
    overlay = _read_json_file(os.path.join(cdir, name))
    if overlay is None and name.endswith(".json"):
        stem = name[: -len(".json")]
        overlay = _read_json_file(os.path.join(cdir, f"{stem}.overlay.json"))
    return _merge_catalog(name, base, overlay)


def write_catalog_overlay(name: str, data) -> str:
    """Write overlay JSON under catalog_dir(). Returns path written."""
    cdir = catalog_dir()
    os.makedirs(cdir, mode=0o755, exist_ok=True)
    path = os.path.join(cdir, name)
    existing = _read_json_file(path)
    if existing is None and name.endswith(".json"):
        alt = os.path.join(cdir, name[: -len(".json")] + ".overlay.json")
        if os.path.isfile(alt):
            path = alt
            existing = _read_json_file(path)
    merged = _merge_catalog(name, existing if existing is not None else (
        {} if name.endswith("pci_id.json") or name == "cpu_specs_wkstn.json" else (
            [] if name == "ram_specs.json" else {}
        )
    ), data)
    if name in ("gpu_pci_id.json", "nic_pci_id.json", "psu_roster.json") and isinstance(data, dict):
        # Persist lab file as existing overlay ∪ new keys (not full shipped merge).
        store = dict(existing) if isinstance(existing, dict) else {}
        store.update(data)
        merged = store
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    return path


def normalize_pci_hex(raw: str | None) -> str | None:
    """Normalize a PCI vendor or device id to 4-digit lowercase hex (strip 0x)."""
    s = (raw or "").strip().lower().replace("0x", "")
    if not s:
        return None
    if len(s) <= 4 and all(c in "0123456789abcdef" for c in s):
        return s.zfill(4)
    return None


def gpu_spec_key(vendor: str | None, device: str | None) -> str | None:
    """JSON key for gpu_pci_id.json: 'vendor:device' (e.g. 10de:28b0)."""
    v = normalize_pci_hex(vendor)
    d = normalize_pci_hex(device)
    if not v or not d:
        return None
    return f"{v}:{d}"


def lookup_gpu_specs(data: dict | None, vendor: str | None, device: str | None) -> dict | None:
    """Lookup gpu_pci_id.json entry by vendor+device. No device-only fallback."""
    if not data:
        return None
    key = gpu_spec_key(vendor, device)
    if not key:
        return None
    row = data.get(key)
    return row if isinstance(row, dict) else None


def lookup_nic_specs(data: dict | None, vendor: str | None, device: str | None) -> dict | None:
    """Lookup nic_pci_id.json by vendor:device (same key scheme as GPU)."""
    return lookup_gpu_specs(data, vendor, device)


def parse_cpu_model(full_model: str) -> tuple[str | None, str | None, str | None]:
    """Return (mfg, family, model) for cpu_specs JSON lookup."""
    if not full_model or full_model == "Unknown":
        return (None, None, None)
    full = full_model.strip()

    if full.startswith("AMD ") or full.upper().startswith("AMD "):
        mfg = "AMD"
        m = re.match(r"AMD\s+Ryzen\s+Threadripper\s+PRO\s+(\d+[A-Z0-9]*)", full, re.I)
        if m:
            return (mfg, "Ryzen Threadripper PRO", m.group(1))
        m = re.match(r"AMD\s+Ryzen\s+Threadripper\s+(\d+[A-Z0-9]*)", full, re.I)
        if m:
            return (mfg, "Ryzen Threadripper", m.group(1))
        # Live lscpu: "AMD Ryzen 5 PRO 4650G ..." (PRO sits between family and model).
        m = re.match(r"AMD\s+Ryzen\s+([3579])\s+PRO\s+([0-9]+[A-Z0-9]*)", full, re.I)
        if m:
            return (mfg, "Ryzen " + m.group(1) + " PRO", m.group(2))
        m = re.match(r"AMD\s+Ryzen\s+([3579])\s+([0-9]+[A-Z0-9]*)", full, re.I)
        if m:
            family = "Ryzen " + m.group(1)
            if re.search(r"\bPRO\b", full, re.I):
                family += " PRO"
            return (mfg, family, m.group(2))
        m = re.match(r"AMD\s+EPYC\s+([A-Z0-9]+)", full, re.I)
        if m:
            return (mfg, "EPYC", m.group(1))
        m = re.match(r"AMD\s+Athlon\s+PRO\s+([0-9]+[A-Z0-9]*)", full, re.I)
        if m:
            return (mfg, "Athlon PRO", m.group(1))
        m = re.match(r"AMD\s+Athlon\s+([0-9]+[A-Z0-9]*)", full, re.I)
        if m:
            family = "Athlon PRO" if re.search(r"\bPRO\b", full, re.I) else "Athlon"
            return (mfg, family, m.group(1))
        return (mfg, None, None)

    # Intel (tolerate (R) trademark marks from lscpu /cpuinfo)
    if re.match(r"Intel\b", full, re.I):
        mfg = "Intel"
        cleaned = re.sub(r"\(R\)|\(TM\)|\(C\)", "", full, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        m = re.match(r"Intel\s+Core\s+(i[3579])[- ]*(\d+[A-Z0-9]*)", cleaned, re.I)
        if m:
            return (mfg, "Core " + m.group(1), m.group(2))
        m = re.match(r"Intel\s+Core\s+([579])\s+Ultra\s+(\d+[A-Z0-9]*)", cleaned, re.I)
        if m:
            return (mfg, "Core " + m.group(1) + " Ultra", m.group(2))
        # lscpu: "Intel Core Ultra 7 265K" (Ultra before the 5/7/9)
        m = re.match(r"Intel\s+Core\s+Ultra\s+([579])\s+(\d+[A-Z0-9]*)", cleaned, re.I)
        if m:
            return (mfg, "Core " + m.group(1) + " Ultra", m.group(2))
        m = re.match(r"Intel\s+(Pentium|Celeron)\s+([A-Z0-9]+)", cleaned, re.I)
        if m:
            return (mfg, m.group(1), m.group(2))
        # Xeon W-#### first (CSV models are W-2295), then Gold/Silver/… ####, then bare SKU
        m = re.search(r"Xeon\s+(W-\d{3,5}[A-Z0-9]*)", cleaned, re.I)
        if m:
            return (mfg, "Xeon", m.group(1))
        m = re.search(
            r"Xeon\s+(?:Gold|Silver|Platinum|Bronze)\s+([A-Z]?\d{3,5}[A-Z0-9+]*)",
            cleaned,
            re.I,
        )
        if m:
            return (mfg, "Xeon", m.group(1))
        m = re.search(r"Xeon\s+([A-Z]?\d{3,5}[A-Z0-9+]*)", cleaned, re.I)
        if m:
            return (mfg, "Xeon", m.group(1))
        return (mfg, None, None)

    # NVIDIA Grace (lscpu often "NVIDIA Grace" / "Grace")
    if re.search(r"\bGrace\b", full, re.I) and (
        re.search(r"NVIDIA|Grace\s+CPU|GH200|Grace\s+Hopper|Superchip", full, re.I)
        or full.strip().upper().startswith("GRACE")
    ):
        mfg = "NVIDIA"
        if re.search(r"Superchip|144", full, re.I):
            return (mfg, "Grace", "Superchip")
        if re.search(r"GH200|Hopper", full, re.I):
            return (mfg, "Grace", "GH200")
        if re.search(r"\bC1\b", full, re.I):
            return (mfg, "Grace", "C1")
        return (mfg, "Grace", "C1")

    if re.search(r"\bAmpere\b", full, re.I):
        mfg = "Ampere"
        cleaned = re.sub(r"\(R\)|\(TM\)|\(C\)", " ", full, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned)
        if re.search(r"Altra\s+Max", cleaned, re.I):
            family = "Altra Max"
        elif re.search(r"\bAltra\b", cleaned, re.I):
            family = "Altra"
        else:
            family = "Altra"
        m = re.search(r"\b([MQ]\d{2,3}-\d{2})\b", cleaned, re.I)
        if m:
            return (mfg, family, m.group(1).upper())
        return (mfg, family, None)

    return (None, None, None)


def lookup_cpu_specs(full_model: str, data: dict | None = None) -> dict | None:
    """Lookup cpu_specs_wkstn.json by model string. Tries Workstation then Server."""
    if data is None:
        data = load_json("cpu_specs_wkstn.json")
    if not data:
        return None
    mfg, family, model = parse_cpu_model(full_model)
    if not mfg or not family or not model:
        return None
    for market in ("Workstation", "Server"):
        leaf = (
            data.get(market, {})
            .get("manufacturers", {})
            .get(mfg, {})
            .get(family, {})
            .get(model)
        )
        if isinstance(leaf, dict):
            return leaf
    return None
