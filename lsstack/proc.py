"""lsproc: CPU details. Reads /proc/cpuinfo, optional lscpu/DMI Type 4; lookup in cpu_specs_wkstn.json. Named lsproc to avoid clashing with system lscpu."""

import json
import re
import subprocess
import sys

from lsstack.color import color_enabled, privilege_note
from lsstack.common import lookup_cpu_specs, parse_cpu_model, read_file, sudo_full_results_note, system_binary


def _model_name_from_cpuinfo() -> str | None:
    for line in (read_file("/proc/cpuinfo") or "").splitlines():
        if line.lower().startswith("model name"):
            val = line.split(":", 1)[1].strip()
            if val:
                return val
    return None


def _resolve_model_name(lscpu: dict[str, str]) -> tuple[str, str]:
    """Pick a lookup-friendly model string. Prefer cpuinfo; fall back to lscpu (Arm/Grace)."""
    cpuinfo = _model_name_from_cpuinfo()
    if cpuinfo:
        return cpuinfo, "cpuinfo"
    for key in ("Model name", "BIOS Model name", "Model Name"):
        val = (lscpu.get(key) or "").strip()
        if val and val.upper() != "N/A":
            return val, f"lscpu:{key}"
    # Some Arm kernels expose a Hardware / Implementer line instead.
    for key in ("Hardware", "Model"):
        val = (lscpu.get(key) or "").strip()
        if val and val.upper() != "N/A" and not val.isdigit():
            return val, f"lscpu:{key}"
    return "Unknown", "none"


def _live_package_cores(lscpu: dict[str, str]) -> int | None:
    """Physical cores per package from lscpu (Core(s) per socket)."""
    raw = (lscpu.get("Core(s) per socket") or "").strip()
    if not raw or raw.upper() == "N/A":
        return None
    try:
        n = int(float(raw))
        return n if n > 0 else None
    except ValueError:
        return None


def _run_lscpu() -> dict[str, str]:
    """Run lscpu once and return a dict of field name -> value."""
    exe = system_binary("lscpu")
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout:
            return {}
        result = {}
        for line in out.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                result[k.strip()] = v.strip()
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}


def _run_nproc() -> str:
    exe = system_binary("nproc")
    if not exe:
        return "N/A"
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "N/A"


def _socket_designations() -> list[str]:
    """All Socket Designation values from SMBIOS Type 4 (multi-socket)."""
    from lsstack.dmi import processor_socket_designations

    return processor_socket_designations()


def _run_sensors() -> tuple[dict[str, str], list[tuple[int, str]]]:
    """Run sensors once. Returns (summary_dict, per_core_list).
    Summary: first line for CPU Core Voltage, CPU Core, CPU Temperature.
    Per-core: all 'CPU Core N:' lines as (index, value) sorted by index."""
    exe = system_binary("sensors")
    if not exe:
        return {}, []
    try:
        out = subprocess.run(
            [exe],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout:
            return {}, []
        labels = ("CPU Core Voltage:", "CPU Core:", "CPU Temperature:")
        want_keys = ("CPU Core Voltage", "CPU Core", "CPU Temperature")
        result = {}
        per_core: list[tuple[int, str]] = []
        for line in out.stdout.splitlines():
            s = line.strip()
            if not s or ":" not in s:
                continue
            # Per-core: "CPU Core 0:", "CPU Core 1:", ...
            m = re.match(r"CPU Core (\d+):", s)
            if m:
                idx = int(m.group(1))
                raw = s.split(":", 1)[1].strip()
                parts = raw.split()
                val = (parts[0] + " " + parts[1]) if len(parts) >= 2 else raw
                per_core.append((idx, val))
                continue
            for i, label in enumerate(labels):
                key = want_keys[i]
                if key in result:
                    continue
                if s.startswith(label):
                    raw = s.split(":", 1)[1].strip()
                    parts = raw.split()
                    if key == "CPU Temperature" and parts:
                        result[key] = parts[0]
                    elif len(parts) >= 2:
                        result[key] = parts[0] + " " + parts[1]
                    else:
                        result[key] = raw
                    break
        per_core.sort(key=lambda x: x[0])
        return result, per_core
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}, []


def _format_frequency(mhz_str: str) -> str:
    """Format MHz string as GHz if >= 1000, else MHz."""
    if not mhz_str or mhz_str == "N/A":
        return "N/A"
    try:
        val = float(mhz_str.replace(",", ".").strip())
        if val >= 1000:
            return "%.1f GHz" % (val / 1000)
        return "%.0f MHz" % val
    except (ValueError, TypeError):
        return mhz_str


def get_cpu_info() -> dict:
    """Run lscpu/DMI Type 4/sensors once; return full dict for human and JSON output."""
    lscpu = _run_lscpu()
    model_name, model_source = _resolve_model_name(lscpu)
    parsed = parse_cpu_model(model_name)
    specs = lookup_cpu_specs(model_name)
    memory_channels = str(specs.get("memory_channels", "Unknown")) if specs else "Unknown"

    nproc_val = _run_nproc()
    socket_designations = _socket_designations()
    sensors, sensors_per_core = _run_sensors()

    def lscpu_f(field: str) -> str:
        return lscpu.get(field, "N/A")

    socket_designation_str = ", ".join(socket_designations) if socket_designations else "N/A"
    max_mhz = lscpu_f("CPU max MHz")
    min_mhz = lscpu_f("CPU min MHz")

    live_cores = _live_package_cores(lscpu)
    spec_cores = None
    if specs and specs.get("cores") is not None:
        try:
            spec_cores = int(float(specs["cores"]))
        except (TypeError, ValueError):
            spec_cores = None
    cores_mismatch = (
        live_cores is not None
        and spec_cores is not None
        and live_cores != spec_cores
    )

    specs_notes: list[str] = []
    if specs is None:
        key = (
            f"{parsed[0]}/{parsed[1]}/{parsed[2]}"
            if parsed[0] and parsed[1] and parsed[2]
            else model_name
        )
        specs_notes.append(
            f"No curated specs for {key} (not in lab catalog). Live lscpu data still shown."
        )
    else:
        if parsed[0] == "NVIDIA" and parsed[1] == "Grace":
            specs_notes.append(
                "Grace memory_channels is DDR5-equivalent for balance heuristics; "
                "memory is on-package LPDDR5X (not DIMM slots)."
            )
        if cores_mismatch:
            specs_notes.append(
                f"spec database says {spec_cores} cores/package; "
                f"live lscpu shows {live_cores} cores/socket"
            )

    return {
        "model_name": model_name,
        "model_source": model_source,
        "parsed": {"mfg": parsed[0], "family": parsed[1], "model": parsed[2]},
        "vendor_id": lscpu_f("Vendor ID"),
        "memory_channels": memory_channels,
        "architecture": lscpu_f("Architecture"),
        "cpu_op_mode": lscpu_f("CPU op-mode(s)"),
        "address_sizes": lscpu_f("Address sizes"),
        "byte_order": lscpu_f("Byte Order"),
        "cpus": nproc_val,
        "on_line_cpus": lscpu_f("On-line CPU(s) list"),
        "threads_per_core": lscpu_f("Thread(s) per core"),
        "cores_per_socket": lscpu_f("Core(s) per socket"),
        "sockets": lscpu_f("Socket(s)"),
        "socket_designation": socket_designation_str,
        "cpu_family": lscpu_f("CPU family"),
        "model": lscpu_f("Model"),
        "stepping": lscpu_f("Stepping"),
        "cpu_max_mhz": _format_frequency(max_mhz),
        "cpu_min_mhz": _format_frequency(min_mhz),
        "bogo_mips": lscpu_f("BogoMIPS"),
        "l1d_cache": lscpu_f("L1d cache"),
        "l1i_cache": lscpu_f("L1i cache"),
        "l2_cache": lscpu_f("L2 cache"),
        "l3_cache": lscpu_f("L3 cache"),
        "numa_nodes": lscpu_f("NUMA node(s)"),
        "numa_node0_cpus": lscpu_f("NUMA node0 CPU(s)"),
        "virtualization": lscpu_f("Virtualization"),
        "cpu_core_voltage": sensors.get("CPU Core Voltage", "N/A"),
        "cpu_core": sensors.get("CPU Core", "N/A"),
        "cpu_temperature": sensors.get("CPU Temperature", "N/A"),
        "sensors_per_core": sensors_per_core,  # list of (index, value)
        "specs": specs,
        "specs_notes": specs_notes,
    }


def _kv(label: str, value: str | None, width: int = 18) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def main(args: list[str] | None = None) -> None:
    args = args or []
    if not read_file("/proc/cpuinfo"):
        print("Error: /proc/cpuinfo not found", file=sys.stderr)
        sys.exit(1)
    info = get_cpu_info()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2))
        return

    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.
    color = color_enabled(args)
    specs = info.get("specs") or {}
    per_core = info.get("sensors_per_core") or []
    notes = info.get("specs_notes") or []

    labels = [
        "Model", "Vendor", "Topology", "Memory channels", "Architecture", "Virtualization",
        "TDP", "Base clock (spec)", "Cores (spec)", "L3 (spec)", "Channels (spec)",
        "PCIe (CPU package)", "Match", "Note",
        "Max", "Min", "L1d", "L1i", "L2", "L3",
        "NUMA nodes", "NUMA node0 CPUs", "Socket label", "Family / model",
        "Temperature", "Core voltage",
        "Op-modes", "Address sizes", "Byte order", "Online CPUs", "BogoMIPS",
    ]
    labels.extend(f"Core {idx}" for idx, _ in per_core)
    w = _label_width(*labels)

    print("\nCPU")
    _section("Summary")
    _kv("Model", info["model_name"], w)
    _kv("Vendor", info["vendor_id"], w)
    _kv(
        "Topology",
        f"{info['sockets']} socket(s), {info['cores_per_socket']} cores/socket, "
        f"{info['threads_per_core']} threads/core, {info['cpus']} CPUs",
        w,
    )
    _kv("Memory channels", info["memory_channels"], w)
    _kv("Architecture", info["architecture"], w)
    _kv("Virtualization", info["virtualization"], w)

    _section("Spec database")
    if specs:
        if specs.get("tdp_watts") is not None:
            _kv("TDP", f"{specs['tdp_watts']} W", w)
        if specs.get("ghz") is not None:
            _kv("Base clock (spec)", f"{specs['ghz']} GHz", w)
        if specs.get("cores") is not None:
            _kv("Cores (spec)", str(specs["cores"]), w)
        if specs.get("l3Cache"):
            _kv("L3 (spec)", str(specs["l3Cache"]), w)
        if specs.get("memory_channels") is not None:
            _kv("Channels (spec)", str(specs["memory_channels"]), w)
        lanes, gen = specs.get("pcie_lanes"), specs.get("pcie_gen")
        if lanes is not None and gen is not None:
            _kv("PCIe (CPU package)", f"{lanes} lanes Gen{gen}", w)
            _kv("Note", "see lstopology for available lanes", w)
    for note in notes:
        _kv("Note", note, w)

    _section("Frequency & cache")
    _kv("Max", info["cpu_max_mhz"], w)
    _kv("Min", info["cpu_min_mhz"], w)
    _kv("L1d", info["l1d_cache"], w)
    _kv("L1i", info["l1i_cache"], w)
    _kv("L2", info["l2_cache"], w)
    _kv("L3", info["l3_cache"], w)

    _section("NUMA / socket")
    _kv("NUMA nodes", info["numa_nodes"], w)
    _kv("NUMA node0 CPUs", info["numa_node0_cpus"], w)
    sock = info["socket_designation"]
    if sock == "N/A":
        _kv(
            "Socket label",
            privilege_note("— (needs sudo for DMI Type 4)", enabled=color),
            w,
        )
    else:
        _kv("Socket label", sock, w)
    _kv("Family / model", f"{info['cpu_family']} / {info['model']} stepping {info['stepping']}", w)

    # Sensors: one voltage line — prefer ~0.5–2.0 V core readings over mV noise
    def _pick_volt(*cands: str | None) -> str | None:
        scored: list[tuple[int, str]] = []
        for c in cands:
            if not c or c == "N/A":
                continue
            s = c.strip()
            if re.search(r"mV\b", s, re.I):
                scored.append((0, s))  # usually not package core V
                continue
            m = re.search(r"([\d.]+)\s*V\b", s, re.I)
            if m:
                try:
                    v = float(m.group(1))
                except ValueError:
                    scored.append((1, s))
                    continue
                scored.append((3 if 0.5 <= v <= 2.0 else 2, s))
            else:
                scored.append((1, s))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        return scored[0][1]

    volt = _pick_volt(info.get("cpu_core_voltage"), info.get("cpu_core"))
    temp = info.get("cpu_temperature")
    if volt or (temp and temp != "N/A") or per_core:
        _section("Sensors")
        if temp and temp != "N/A":
            _kv("Temperature", temp, w)
        if volt:
            _kv("Core voltage", volt, w)
        for idx, val in per_core:
            _kv(f"Core {idx}", val, w)
    elif system_binary("sensors") is None:
        _section("Sensors")
        _kv("Note", "lm-sensors not installed (sysfs thermal: lsthermal)", w)

    _section("Details")
    _kv("Op-modes", info["cpu_op_mode"], w)
    _kv("Address sizes", info["address_sizes"], w)
    _kv("Byte order", info["byte_order"], w)
    _kv("Online CPUs", info["on_line_cpus"], w)
    _kv("BogoMIPS", info["bogo_mips"], w)
    sudo_note = sudo_full_results_note("lsproc")
    if sudo_note:
        _section("Notes")
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    print()


if __name__ == "__main__":
    main(sys.argv[1:] if len(sys.argv) > 1 else [])
