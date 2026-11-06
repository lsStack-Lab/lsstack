"""lsthermal: temperatures and fans from sysfs (nvidia-smi and amdgpu when present). Voltages → lspower.

CPU color uses catalog ``tjmax_c`` when present (Workstation SKUs, including
PRO / Threadripper / Intel HEDT). Other sensors use lab first-look bands.
Server rows later.

FUTURE(server-profile): rack nodes should N/A ATX PSU/ports/80% headroom and
keep inventory + ECC + NUMA. Not this module. Workstation-lab stays the product.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from lsstack.common import read_file, system_binary, want_notes

_THERMAL = "/sys/class/thermal"
_HWMON = "/sys/class/hwmon"


def _millidegree_to_c(raw: str | None) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip()) / 1000.0
    except ValueError:
        return None


def _temp_plausible(temp_c: float | None) -> bool:
    """Drop missing, uninitialized (0°C), absurd cold/hot readings."""
    if temp_c is None:
        return False
    if temp_c <= 0.5 or temp_c >= 125.0:
        return False
    return True


def get_thermal_zones() -> list[dict]:
    """Thermal zones from /sys/class/thermal/thermal_zone*."""
    out: list[dict] = []
    if not os.path.isdir(_THERMAL):
        return out
    for name in sorted(os.listdir(_THERMAL)):
        if not name.startswith("thermal_zone"):
            continue
        path = os.path.join(_THERMAL, name)
        typ = read_file(os.path.join(path, "type")) or "unknown"
        temp_c = _millidegree_to_c(read_file(os.path.join(path, "temp")))
        if not _temp_plausible(temp_c):
            continue
        out.append({
            "id": name,
            "type": typ.strip() if typ else "unknown",
            "temp_c": temp_c,
        })
    return out


def get_hwmon_temps() -> list[dict]:
    """Temp inputs from /sys/class/hwmon (plausible only)."""
    out: list[dict] = []
    if not os.path.isdir(_HWMON):
        return out
    for hw in sorted(os.listdir(_HWMON)):
        base = os.path.join(_HWMON, hw)
        name = (read_file(os.path.join(base, "name")) or hw).strip()
        try:
            files = os.listdir(base)
        except OSError:
            continue
        for fname in sorted(files):
            if not re.match(r"temp\d+_input$", fname):
                continue
            idx = fname.split("_")[0]
            label = read_file(os.path.join(base, f"{idx}_label")) or idx
            temp_c = _millidegree_to_c(read_file(os.path.join(base, fname)))
            if not _temp_plausible(temp_c):
                continue
            out.append({
                "hwmon": name,
                "node": hw,
                "label": (label or idx).strip(),
                "temp_c": temp_c,
            })
    return out


def get_hwmon_fans() -> list[dict]:
    """Fan RPM from /sys/class/hwmon (non-zero only)."""
    out: list[dict] = []
    if not os.path.isdir(_HWMON):
        return out
    for hw in sorted(os.listdir(_HWMON)):
        base = os.path.join(_HWMON, hw)
        name = (read_file(os.path.join(base, "name")) or hw).strip()
        try:
            files = os.listdir(base)
        except OSError:
            continue
        for fname in sorted(files):
            if not re.match(r"fan\d+_input$", fname):
                continue
            idx = fname.split("_")[0]
            label = read_file(os.path.join(base, f"{idx}_label")) or idx
            raw = read_file(os.path.join(base, fname))
            try:
                rpm = int(float(str(raw).strip())) if raw else 0
            except ValueError:
                continue
            if rpm <= 0:
                continue
            out.append({
                "hwmon": name,
                "label": (label or idx).strip(),
                "rpm": rpm,
            })
    return out


def get_nvidia_temps() -> list[dict]:
    """GPU temps from nvidia-smi when available."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [
                exe,
                "--query-gpu=index,name,temperature.gpu,fan.speed",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return []
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                temp = int(float(parts[2])) if parts[2] and parts[2].upper() != "N/A" else None
            except ValueError:
                temp = None
            fan = parts[3] if len(parts) > 3 else None
            if fan and fan.upper() == "N/A":
                fan = None
            rows.append({
                "index": parts[0],
                "name": parts[1],
                "temp_c": temp,
                "fan_pct": fan,
            })
        return rows
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _norm_label(label: str) -> str:
    s = label.lower().strip()
    s = re.sub(r"\s+temperature$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _pick_summary(hw_temps: list[dict], nvidia: list[dict]) -> list[dict]:
    """Deduped role → reading for Summary. Prefer better chips over ASUS duplicates."""
    by_role: dict[str, dict] = {}

    def _consider(role: str, temp_c: float, source: str, priority: int) -> None:
        cur = by_role.get(role)
        if cur is None or priority < cur["priority"]:
            by_role[role] = {
                "role": role,
                "temp_c": temp_c,
                "source": source,
                "priority": priority,
            }

    for h in hw_temps:
        hw = (h.get("hwmon") or "").lower()
        lab = _norm_label(h.get("label") or "")
        temp = h["temp_c"]
        src = f"{h['hwmon']}:{h['label']}"

        if hw == "k10temp" and lab in ("tctl", "tdie"):
            _consider("CPU", temp, src, 0)
            continue
        if hw == "nvme" or lab == "composite":
            if "nvme" in hw or lab == "composite":
                _consider("NVMe", temp, src, 1 if hw == "nvme" else 5)
                continue
        if lab in ("cpu", "cpu temperature") and "opt" not in lab:
            # k10temp already preferred; asus_wmi over asusec
            pri = 2 if "wmi" in hw else 4
            _consider("CPU", temp, src, pri)
        elif lab in ("motherboard", "motherboard temperature", "mb"):
            pri = 2 if "wmi" in hw else 4
            _consider("Motherboard", temp, src, pri)
        elif lab in ("chipset", "chipset temperature"):
            # asusec chipset often real; asus_wmi often 0 (filtered already)
            pri = 2 if hw == "asusec" else 3
            _consider("Chipset", temp, src, pri)
        elif lab in ("vrm", "vrm temperature"):
            _consider("VRM", temp, src, 3)

    # amdgpu: edge ≈ nvidia temperature.gpu; junction is hotspot (used if no edge)
    amd_by_node: dict[str, dict] = {}
    for h in hw_temps:
        if (h.get("hwmon") or "").lower() != "amdgpu":
            continue
        lab = _norm_label(h.get("label") or "")
        if lab not in ("edge", "junction"):
            continue
        node = h.get("node") or "amdgpu"
        pri = 0 if lab == "edge" else 1
        cur = amd_by_node.get(node)
        if cur is None or pri < cur["priority"]:
            amd_by_node[node] = {
                "temp_c": h["temp_c"],
                "source": f"{h['hwmon']}:{h['label']}",
                "priority": pri,
            }

    nvidia_ok = [g for g in nvidia if g.get("temp_c") is not None]
    if not nvidia_ok and amd_by_node:
        nodes = sorted(amd_by_node)
        for i, node in enumerate(nodes):
            rec = amd_by_node[node]
            role = "GPU" if len(nodes) == 1 else f"GPU {i}"
            by_role[role] = {
                "role": role,
                "temp_c": rec["temp_c"],
                "source": rec["source"],
                "priority": rec["priority"],
                "name": "amdgpu",
            }

    for g in nvidia:
        if g.get("temp_c") is None:
            continue
        idx = g.get("index", "0")
        role = "GPU" if len(nvidia) == 1 else f"GPU {idx}"
        fan = g.get("fan_pct")
        by_role[role] = {
            "role": role,
            "temp_c": float(g["temp_c"]),
            "source": g.get("name") or "nvidia-smi",
            "priority": 0,
            "fan_pct": fan,
            "name": g.get("name"),
        }

    order = ["CPU", "GPU", "Motherboard", "Chipset", "VRM", "NVMe"]
    ordered: list[dict] = []
    seen: set[str] = set()
    for role in order:
        if role in by_role:
            ordered.append(by_role[role])
            seen.add(role)
    for role, rec in by_role.items():
        if role.startswith("GPU") and role not in seen:
            ordered.append(rec)
            seen.add(role)
    return ordered


def _dedupe_fans(fans: list[dict]) -> list[dict]:
    """Prefer asus_wmi fan labels; drop duplicate OPT/zero already filtered."""
    by_label: dict[str, dict] = {}
    for f in fans:
        lab = _norm_label(f.get("label") or "")
        hw = (f.get("hwmon") or "").lower()
        pri = 0 if "wmi" in hw else 2
        cur = by_label.get(lab)
        if cur is None or pri < cur["priority"]:
            by_label[lab] = {
                "label": f.get("label") or lab,
                "rpm": f["rpm"],
                "hwmon": f.get("hwmon"),
                "priority": pri,
            }
    return sorted(by_label.values(), key=lambda x: x["label"].lower())


def get_thermal_info() -> dict:
    zones = get_thermal_zones()
    hw_temps = get_hwmon_temps()
    hw_fans = get_hwmon_fans()
    nvidia = get_nvidia_temps()
    summary = _pick_summary(hw_temps, nvidia)
    fans = _dedupe_fans(hw_fans)
    tjmax = _cpu_tjmax_c()
    rows = []
    for s in summary:
        rec = {k: v for k, v in s.items() if k != "priority"}
        if rec.get("role") == "CPU" and tjmax is not None:
            rec["tjmax_c"] = tjmax
        y, r, src = _temp_thresholds(rec.get("role") or "", rec.get("tjmax_c"))
        rec["temp_yellow_c"] = round(y, 1)
        rec["temp_red_c"] = round(r, 1)
        rec["temp_band_source"] = src
        rows.append(rec)
    return {
        "summary": rows,
        "fans": [
            {"label": f["label"], "rpm": f["rpm"], "hwmon": f.get("hwmon")}
            for f in fans
        ],
        "thermal_zones": zones,
        "hwmon_temps": hw_temps,
        "nvidia": nvidia,
    }


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


# Lab first-look (yellow, red) °C when the catalog has no Tjmax.
# NVMe/board complain earlier than a CPU die; VRM FETs run hotter.
_LAB_TEMP_BANDS = {
    "CPU": (80.0, 90.0),
    "GPU": (80.0, 90.0),
    "NVMe": (70.0, 80.0),
    "Motherboard": (70.0, 80.0),
    "Chipset": (70.0, 80.0),
    "VRM": (90.0, 105.0),
}


def _cpu_tjmax_c() -> int | None:
    """Workstation catalog Tjmax for the live CPU, or None if not filled yet."""
    from lsstack.common import lookup_cpu_specs

    blob = read_file("/proc/cpuinfo") or ""
    model = None
    for line in blob.splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[-1].strip()
            break
    specs = lookup_cpu_specs(model) if model else None
    if not specs:
        return None
    v = specs.get("tjmax_c")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _temp_thresholds(
    role: str, tjmax_c: int | None
) -> tuple[float, float, str]:
    """Return (yellow_c, red_c, source). CPU uses 85%/95% of Tjmax when known."""
    if role == "CPU" and tjmax_c:
        return (0.85 * tjmax_c, 0.95 * tjmax_c, "catalog_tjmax")
    key = "GPU" if (role or "").startswith("GPU") else (role or "CPU")
    y, r = _LAB_TEMP_BANDS.get(key, (80.0, 90.0))
    return (y, r, "lab")


def _fmt_c(temp_c: float | int | None) -> str:
    if temp_c is None:
        return "—"
    if isinstance(temp_c, float) and not temp_c.is_integer():
        return f"{temp_c:.1f}°C"
    return f"{int(temp_c)}°C"


def _color_temp(
    temp_c: float | int | None,
    text: str,
    *,
    enabled: bool,
    yellow_c: float,
    red_c: float,
) -> str:
    """Green below yellow, yellow to red, red at/above red_c."""
    if not enabled or temp_c is None:
        return text
    from lsstack.color import green, red, yellow

    t = float(temp_c)
    if t >= red_c:
        return red(text, enabled=True)
    if t >= yellow_c:
        return yellow(text, enabled=True)
    return green(text, enabled=True)


def print_thermal_human(
    info: dict | None = None,
    *,
    watching: bool = False,
    heading: bool = True,
    color: bool = False,
    notes: bool = True,
) -> None:
    """Print thermal human report. watching=True omits -w / lswatch tips."""
    info = info if info is not None else get_thermal_info()
    summary = info.get("summary") or []
    fans = info.get("fans") or []
    zones = info.get("thermal_zones") or []

    labels = ["CPU", "GPU", "Motherboard", "Chipset", "VRM", "NVMe", "Note"]
    labels.extend(s["role"] for s in summary)
    labels.extend(f["label"] for f in fans)
    labels.extend(z["type"] for z in zones)
    w = _label_width(*labels)

    if heading:
        print("\nThermal")
    _section("Summary")
    if not summary:
        _kv("Note", "no plausible temperature sensors found", w)
    else:
        for s in summary:
            y, r, _src = _temp_thresholds(s.get("role") or "", s.get("tjmax_c"))
            raw = _fmt_c(s.get("temp_c"))
            val = _color_temp(
                s.get("temp_c"), raw, enabled=color, yellow_c=y, red_c=r,
            )
            if s.get("fan_pct") is not None:
                val = f"{val}, fan {s['fan_pct']}%"
            if s.get("name") and s["role"].startswith("GPU"):
                val = f"{val}  {s['name']}"
            _kv(s["role"], val, w)

    _section("Fans")
    if not fans:
        _kv("Note", "no running fans reported in hwmon (0 RPM omitted)", w)
    else:
        for f in fans:
            _kv(f["label"], f"{f['rpm']} RPM", w)

    if zones and not watching:
        _section("Zones")
        for z in zones:
            _kv(z["type"], f"{z['id']}: {_fmt_c(z.get('temp_c'))}", w)

    if notes and not watching:
        _section("Notes")
        cpu_row = next((s for s in summary if s.get("role") == "CPU"), None)
        if cpu_row and cpu_row.get("temp_band_source") == "catalog_tjmax":
            _kv(
                "Note",
                f"CPU color from catalog Tjmax {cpu_row['tjmax_c']}°C "
                f"(yellow {cpu_row['temp_yellow_c']:.0f}°C, "
                f"red {cpu_row['temp_red_c']:.0f}°C). Not k10temp temp_max.",
                w,
            )
        else:
            _kv(
                "Note",
                "CPU color is a lab first-look (yellow 80°C, red 90°C); "
                "catalog Tjmax not on this SKU yet.",
                w,
            )
        _kv(
            "Note",
            "Other sensors: lab bands (NVMe/board 70/80, GPU 80/90, VRM 90/105), "
            "not a JEDEC/ATX standard. Green = below yellow.",
            w,
        )
        _kv(
            "Note",
            "watch: lsthermal -w (clears screen, default 2s; -w N to set interval)",
            w,
        )
        _kv("Note", "combined thermal+power live → lswatch", w)

    print()


def _help_text() -> str:
    return """lsthermal — temperatures and fans for lab / workstation builds

What
  Reports CPU/board/NVMe temps from sysfs hwmon, GPU from nvidia-smi or amdgpu
  (edge, else junction), running fan RPM, and kernel thermal zones.

Why
  Builders need a first look at die and board heat without lm-sensors charts,
  and whether a GPU is cooking while the case fans look idle.

How
  Run on the host. 0°C / absurd readings are dropped. Temps: green below the
  yellow band, yellow then red. CPU uses catalog Tjmax when present
  (Workstation catalog, including PRO/HEDT); otherwise a lab 80/90.
  Watch: lsthermal -w.
  Combined thermal+power → lswatch. Voltages → lspower. Lean data → --json.

Usage: lsthermal [options]
  --json, -j       JSON
  -w, --watch [N]  Refresh live (default 2s)
  --no-notes       Hide the Notes section
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    from lsstack.color import color_enabled
    from lsstack.watchutil import parse_watch_args, run_watch

    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.
    if "-h" in args or "--help" in args:
        print(_help_text())
        return
    args, watch_sec = parse_watch_args(args)
    use_color = color_enabled(args)

    if ("--json" in args or "-j" in args) and watch_sec is not None:
        print("Error: --json cannot be used with -w/--watch", file=sys.stderr)
        sys.exit(2)

    if "--json" in args or "-j" in args:
        print(json.dumps(get_thermal_info(), indent=2))
        return

    if watch_sec is not None:
        run_watch(
            lambda: print_thermal_human(watching=True, color=use_color),
            watch_sec,
            title="lsthermal",
        )
        return

    print_thermal_human(watching=False, color=use_color, notes=want_notes(args))


if __name__ == "__main__":
    main()
