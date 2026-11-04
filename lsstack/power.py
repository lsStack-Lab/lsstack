"""lspower: live draw (GPU/RAPL), rails, PSU config, estimated TDP vs headroom.

Estimated platform balance detail stays in `lsstack` (report); this module is the
power-focused human view (+ optional -w watch).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from lsstack.common import read_file, sudo_full_results_note, system_binary

_RAPL = "/sys/class/powercap"
_POWER_SUPPLY = "/sys/class/power_supply"
_HWMON = "/sys/class/hwmon"

# domain_id -> (monotonic_time, energy_uj) for watch-mode watts
_last_energy: dict[str, tuple[float, int]] = {}


def _rapl_domains() -> list[dict]:
    """RAPL zones under powercap (package + subdomains)."""
    out: list[dict] = []
    if not os.path.isdir(_RAPL):
        return out
    for name in sorted(os.listdir(_RAPL)):
        if not re.match(r"intel-rapl(:\d+)+$", name):
            continue
        base = os.path.join(_RAPL, name)
        label = (read_file(os.path.join(base, "name")) or name).strip()
        enabled_raw = read_file(os.path.join(base, "enabled"))
        enabled = None
        if enabled_raw is not None and enabled_raw.strip() in ("0", "1"):
            enabled = enabled_raw.strip() == "1"

        limits_w: list[float] = []
        try:
            files = os.listdir(base)
        except OSError:
            files = []
        for fname in sorted(files):
            m = re.match(r"constraint_(\d+)_power_limit_uw$", fname)
            if not m:
                continue
            raw = read_file(os.path.join(base, fname))
            if raw and raw.isdigit():
                limits_w.append(round(int(raw) / 1_000_000.0, 1))

        energy_uj = None
        energy_note = None
        energy_path = os.path.join(base, "energy_uj")
        if os.path.isfile(energy_path):
            try:
                with open(energy_path, encoding="utf-8") as f:
                    raw_e = f.read().strip()
                if raw_e.isdigit():
                    energy_uj = int(raw_e)
            except PermissionError:
                energy_note = "needs sudo for live watts (energy_uj)"
            except OSError:
                energy_note = "unreadable"

        watts = None
        if energy_uj is not None:
            now = time.monotonic()
            prev = _last_energy.get(name)
            _last_energy[name] = (now, energy_uj)
            if prev is not None:
                dt = now - prev[0]
                de = energy_uj - prev[1]
                if dt > 0.05 and de >= 0:
                    watts = round((de / dt) / 1_000_000.0, 1)

        out.append({
            "id": name,
            "name": label,
            "enabled": enabled,
            "limits_w": limits_w,
            "energy_uj": energy_uj,
            "energy_note": energy_note,
            "watts": watts,
        })
    return out


def _acpi_supplies() -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(_POWER_SUPPLY):
        return out
    for name in sorted(os.listdir(_POWER_SUPPLY)):
        base = os.path.join(_POWER_SUPPLY, name)
        typ = (read_file(os.path.join(base, "type")) or "").strip()
        status = (read_file(os.path.join(base, "status")) or "").strip() or None
        online = (read_file(os.path.join(base, "online")) or "").strip() or None
        out.append({"name": name, "type": typ or None, "status": status, "online": online})
    return out


def _nvidia_power() -> tuple[list[dict], str | None]:
    """Live NVIDIA draw via nvidia-smi, plus a note when there is none."""
    exe = system_binary("nvidia-smi")
    if not exe:
        return [], "nvidia-smi not installed"
    try:
        r = subprocess.run(
            [
                exe,
                "--query-gpu=index,name,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return [], "nvidia-smi returned no GPUs"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return [], "nvidia-smi failed"
    rows = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue

        def _num(s: str) -> float | None:
            if not s or s.upper() == "N/A":
                return None
            try:
                return float(s)
            except ValueError:
                return None

        rows.append({
            "index": parts[0],
            "name": parts[1] if len(parts) > 1 else "GPU",
            "draw_w": _num(parts[2]),
            "limit_w": _num(parts[3]) if len(parts) > 3 else None,
        })
    if not rows:
        return [], "nvidia-smi returned no GPUs"
    return rows, None


def _mv_to_volts(raw: str) -> float | None:
    try:
        v = float(raw.strip())
    except ValueError:
        return None
    # hwmon in*_input is millivolts for these chips
    if abs(v) >= 50:
        return round(v / 1000.0, 3)
    return round(v, 3)


def _voltage_rails() -> list[dict]:
    """Board rails from hwmon (prefer asus_wmi / asusec labels)."""
    if not os.path.isdir(_HWMON):
        return []
    prefer = ("asus_wmi_sensors", "asusec", "asus")
    by_label: dict[str, dict] = {}
    for hw in sorted(os.listdir(_HWMON)):
        base = os.path.join(_HWMON, hw)
        name = (read_file(os.path.join(base, "name")) or hw).strip()
        try:
            files = os.listdir(base)
        except OSError:
            continue
        for fname in files:
            if not re.match(r"in\d+_input$", fname):
                continue
            idx = fname.split("_")[0]
            label = (read_file(os.path.join(base, f"{idx}_label")) or idx).strip()
            volts = _mv_to_volts(read_file(os.path.join(base, fname)) or "")
            if volts is None:
                continue
            # Skip implausible CPU-core-scale junk from some WMI nodes (<0.5V labeled core)
            lab_l = label.lower()
            if "core" in lab_l and volts < 0.5:
                continue
            # Collapse "CPU Core Voltage" vs "CPU Core"
            norm = re.sub(r"\s+voltage$", "", lab_l).strip()
            pri = prefer.index(name) if name in prefer else 9
            cur = by_label.get(norm)
            if cur is None or pri < cur["priority"]:
                by_label[norm] = {
                    "label": label.replace(" Voltage", "").replace(" voltage", ""),
                    "volts": volts,
                    "hwmon": name,
                    "priority": pri,
                }
    return sorted(
        ({k: v for k, v in x.items() if k != "priority"} for x in by_label.values()),
        key=lambda r: r["label"].lower(),
    )


def get_power_info() -> dict:
    from lsstack.budget import estimate_system_budget

    budget = estimate_system_budget()
    gpu_power, gpu_power_note = _nvidia_power()
    return {
        "psu": budget.get("psu"),
        "psu_watts": budget.get("psu_watts"),
        "budget": budget,
        "headroom": budget.get("headroom"),
        "rapl": _rapl_domains(),
        "gpu_power": gpu_power,
        "gpu_power_note": gpu_power_note,
        "rails": _voltage_rails(),
        "acpi_supplies": _acpi_supplies(),
    }


# Educational notes: same wrap target as lstopology Why/Prompt.
_NOTE_WRAP = 100


def _wrap_words(text: str, width: int) -> list[str]:
    """Word-wrap text to at most width characters per line."""
    text = (text or "").strip()
    if not text:
        return []
    if width < 20:
        width = 20
    lines: list[str] = []
    for para in text.splitlines() or [text]:
        para = para.strip()
        if not para:
            continue
        words = para.split()
        cur: list[str] = []
        cur_len = 0
        for word in words:
            add = len(word) + (1 if cur else 0)
            if cur and cur_len + add > width:
                lines.append(" ".join(cur))
                cur = [word]
                cur_len = len(word)
            else:
                cur.append(word)
                cur_len += add
        if cur:
            lines.append(" ".join(cur))
    return lines or [text]


def _kv(label: str, value: str | None, width: int = 14, *, wrap: int | None = None) -> None:
    if value is None or value == "":
        return
    if wrap is None:
        print(f"  {label:>{width}}  {value}")
        return
    lines = _wrap_words(str(value), wrap)
    if not lines:
        return
    print(f"  {label:>{width}}  {lines[0]}")
    pad = " " * (2 + width + 2)
    for line in lines[1:]:
        print(f"{pad}{line}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def print_power_human(
    info: dict | None = None,
    *,
    watching: bool = False,
    heading: bool = True,
    color: bool = False,
) -> None:
    from lsstack.color import headroom_status, looks_like_privilege_note, privilege_note

    info = info if info is not None else get_power_info()
    budget = info.get("budget") or {}
    rapl = info.get("rapl") or []
    gpus = info.get("gpu_power") or []
    rails = info.get("rails") or []
    acpi = info.get("acpi_supplies") or []
    headroom = info.get("headroom") or budget.get("headroom")
    blines = budget.get("lines") or []

    labels = [
        "PSU", "Model", "ATX", "Eff", "Summary", "Ports", "Total", "Spec", "Est",
        "Headroom", "Note",
        "24-pin motherboard (often 20+4 cable)", "CPU / EPS (4+4 or 8-pin)",
        "SATA power", "Molex / peripheral", "GPU 6+2 PCIe", "GPU 12V-2x6 (12VHPWR)",
        "GPU", "RAPL", "+12V", "+5V", "3VSB", "CPU Core",
    ]
    labels.extend(L.get("label") or "" for L in blines)
    labels.extend(f"GPU {g.get('index')}" for g in gpus)
    labels.extend(r.get("name") or r.get("id") or "" for r in rapl)
    labels.extend(r.get("label") or "" for r in rails)
    labels.extend(s.get("name") or "" for s in acpi)
    w = _label_width(*labels)

    if heading:
        print("\nPower")

    _section("Design budget")
    from lsstack.psu_roster import candor_note, format_plugs_lines, format_plugs_short

    psu_obj = info.get("psu") or (info.get("budget") or {}).get("psu")
    psu = info.get("psu_watts")
    if psu is None and psu_obj:
        psu = psu_obj.get("watts")
    psu_src = (info.get("budget") or {}).get("psu_source") or (info.get("headroom") or {}).get("psu_source")
    if psu is not None:
        src = psu_src or "config"
        _kv("PSU", f"{psu} W nameplate (from {src})", w)
        if psu_obj:
            if psu_obj.get("model"):
                _kv("Model", str(psu_obj["model"]), w)
            _kv("ATX", str(psu_obj.get("atx") or "unknown"), w)
            if psu_obj.get("eff"):
                _kv("Eff", str(psu_obj["eff"]) + " (shown only; not derated)", w)
            plugs = psu_obj.get("plugs") or {}
            if plugs:
                _section("Ports (manufacturer / datasheet)")
                for label, val in format_plugs_lines(plugs):
                    _kv(label, val, w)
                _kv("Summary", format_plugs_short(plugs), w)
                _kv(
                    "Note",
                    "ports on the PSU, not cables in the box — verify exact model on vendor site",
                    w,
                    wrap=_NOTE_WRAP,
                )
            else:
                _kv(
                    "Ports",
                    "not declared — lslab set-psu (look up model: 24-pin, CPU, SATA, GPU…)",
                    w,
                )
    else:
        _kv("PSU", "not set — lslab enroll / set-psu, or psu_watts= in ~/.lsstack.conf", w)

    _section("TDP")
    for L in blines:
        kind = L.get("kind") or "est"
        detail = L.get("detail")
        val = f"{L['watts']} W  [{kind}]"
        if detail:
            if looks_like_privilege_note(detail):
                detail = privilege_note(str(detail), enabled=color)
            val = f"{val}  {detail}"
        _kv(L.get("label") or L.get("key"), val, w)

    total = budget.get("total_w")
    if total is not None:
        _kv(
            "Total",
            f"{total} W  ({budget.get('spec_w', 0)} W spec + {budget.get('est_w', 0)} W est)",
            w,
        )

    if headroom is not None:
        ok = bool(headroom.get("ok"))
        status = headroom_status(
            ok,
            "OK" if ok else "may be undersized",
            enabled=color,
        )
        _kv(
            "Headroom",
            f"{headroom['pct_of_psu']:.0f}% of PSU — {status}",
            w,
        )
    elif info.get("psu_watts") is None and total:
        _kv("Note", "lslab enroll (or psu_watts=) to check headroom", w)
    _kv("Note", "spec = datasheet TDP; est = class allowance × counted devices", w)
    lab = (info.get("budget") or {}).get("lab") or {}
    if lab.get("board_build_matches") and not lab.get("build"):
        _kv(
            "Note",
            "board matches lab build(s): "
            + ", ".join(lab["board_build_matches"])
            + " — lslab adopt",
            w,
        )

    _section("Live")
    if gpus:
        for g in gpus:
            draw = g.get("draw_w")
            lim = g.get("limit_w")
            name = g.get("name") or "GPU"
            if draw is None and lim is None:
                val = f"{name}: N/A"
            elif lim is not None:
                val = f"{name}: {draw:.1f} W / {lim:.0f} W limit" if draw is not None else f"{name}: — / {lim:.0f} W limit"
            else:
                val = f"{name}: {draw:.1f} W"
            label = "GPU" if len(gpus) == 1 else f"GPU {g.get('index')}"
            _kv(label, val, w)
    else:
        _kv("GPU", info.get("gpu_power_note") or "no NVIDIA draw", w)

    if rapl:
        for r in rapl:
            name = r.get("name") or r.get("id")
            parts = []
            if r.get("watts") is not None:
                parts.append(f"{r['watts']} W")
            elif r.get("energy_uj") is not None:
                parts.append("energy ok — use -w for watts")
            elif r.get("energy_note"):
                note = r["energy_note"]
                if looks_like_privilege_note(note):
                    note = privilege_note(note, enabled=color)
                parts.append(note)
            if r.get("limits_w"):
                parts.append("limits " + ", ".join(f"{x} W" for x in r["limits_w"]))
            elif not parts:
                parts.append("no power-limit constraints on this platform")
            if r.get("enabled") is False:
                parts.append("cap disabled")
            _kv(name, "; ".join(parts), w)
    else:
        _kv("RAPL", "none under /sys/class/powercap", w)

    if rails:
        _section("Rails")
        for r in rails:
            _kv(r["label"], f"{r['volts']:.3f} V", w)

    if acpi:
        _section("ACPI supplies")
        for s in acpi:
            extra = s.get("status") or (
                f"online={s['online']}" if s.get("online") is not None else ""
            )
            bits = " ".join(p for p in (s.get("type") or "", extra) if p)
            _kv(s["name"], bits or "—", w)

    if not watching:
        _section("Notes")
        sudo_note = sudo_full_results_note("lspower")
        if sudo_note:
            _kv("Note", privilege_note(sudo_note, enabled=color), w, wrap=_NOTE_WRAP)
        _kv("Note", candor_note(), w, wrap=_NOTE_WRAP)
        _kv(
            "Note",
            "watch: lspower -w (clears screen, default 2s; -w N to set interval)",
            w,
            wrap=_NOTE_WRAP,
        )
        _kv("Note", "combined thermal+power live → lswatch", w, wrap=_NOTE_WRAP)
        _kv("Note", "temps/fans → lsthermal", w, wrap=_NOTE_WRAP)
        _kv(
            "Note",
            "lab PSU/builds → lslab status; custom SKU → lscatalog add psu",
            w,
            wrap=_NOTE_WRAP,
        )

    print()


def _help_text() -> str:
    return """lspower — live draw, rails, PSU ports, estimated TDP vs headroom

What
  Reports PSU nameplate and manufacturer port counts, a design budget (spec vs
  estimated watts), live NVIDIA GPU draw, RAPL package energy, board voltage
  rails, and ACPI supplies.

Why
  Builders need to know whether the brick can feed the board and GPUs, and
  whether live draw looks sane, before a boot loop or a cooked 12V-2x6.

How
  Design budget matches lsstack (report). Live RAPL watts need two samples
  (lspower -w) and sudo when energy_uj is locked. Live GPU watts are NVIDIA
  only; AMD inventory → lsgpu. Temps → lsthermal. Lean data → --json.

Usage: lspower [options]
  --json, -j       JSON
  -w, --watch [N]  Refresh live (default 2s)
  -nc, --no-color  Disable ANSI color
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    from lsstack.color import color_enabled
    from lsstack.watchutil import parse_watch_args, run_watch

    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return
    args, watch_sec = parse_watch_args(args)
    use_color = color_enabled(args)

    if ("--json" in args or "-j" in args) and watch_sec is not None:
        print("Error: --json cannot be used with -w/--watch", file=sys.stderr)
        sys.exit(2)

    if "--json" in args or "-j" in args:
        print(json.dumps(get_power_info(), indent=2))
        return

    if watch_sec is not None:
        # Prime RAPL energy sample so first refresh can show watts after interval
        get_power_info()

        def _tick() -> None:
            print_power_human(watching=True, color=use_color)

        run_watch(_tick, watch_sec, title="lspower")
        return

    print_power_human(watching=False, color=use_color)


if __name__ == "__main__":
    main()
