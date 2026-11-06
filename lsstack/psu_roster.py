"""PSU roster lookup + lab/BOM PSU object helpers (planner-ready schema).

Headroom math uses nameplate watts only. Efficiency is stored for education;
do not soft-derate in the engine.

Declared connectors (plugs{}) are **manufacturer port counts for the exact model**,
not cables in the box (modular kits often ship fewer cables than rear ports).
Look the model up on the vendor site / datasheet. Full ATX story — not GPU-only:
  24-pin (motherboard), cpu (EPS 4+4/8-pin), sata, molex (peripheral),
  6+2 (PCIe GPU), 12V-2x6 (modern GPU; 12VHPWR alias).
"""

from __future__ import annotations

import re
from typing import Any

from lsstack.common import load_json

ATX_VALUES = ("2", "3.0", "3.1", "unknown")
EFF_VALUES = ("bronze", "silver", "gold", "platinum", "titanium", "unknown")

# Canonical plug keys (learning / display order)
PLUG_24PIN = "24-pin"
PLUG_CPU = "cpu"
PLUG_SATA = "sata"
PLUG_MOLEX = "molex"
PLUG_6P2 = "6+2"
PLUG_12V = "12V-2x6"

PLUG_ORDER = (PLUG_24PIN, PLUG_CPU, PLUG_SATA, PLUG_MOLEX, PLUG_6P2, PLUG_12V)
PLUG_LABELS = {
    # 20+4 is cable construction; modern boards use all 24 — still one 24-pin port
    PLUG_24PIN: "24-pin motherboard",
    PLUG_CPU: "CPU / EPS",
    PLUG_SATA: "SATA power",
    PLUG_MOLEX: "Molex / peripheral",
    PLUG_6P2: "GPU 6+2 PCIe",
    PLUG_12V: "GPU 12V-2x6",
}
# Flavor notes after the count so they do not pad the label column
PLUG_NOTES = {
    PLUG_24PIN: "often 20+4 cable",
    PLUG_CPU: "4+4 or 8-pin",
    PLUG_12V: "12VHPWR",
}

# CSV column → canonical key
CSV_PLUG_COLS = (
    ("plug_24pin", PLUG_24PIN),
    ("plug_cpu", PLUG_CPU),
    ("plug_sata", PLUG_SATA),
    ("plug_molex", PLUG_MOLEX),
    ("plug_6p2", PLUG_6P2),
    ("plug_12v2x6", PLUG_12V),
)

# Voice lock: candid, dry, useful. Do not print "blunt" in user-facing copy.
_CANDOR_NOTES = (
    "Nameplate watts are a handshake with marketing, not a lab bench. "
    "Budget math still uses the sticker. Brochure GPU/PSU peaks are best-case "
    "fiction; real efficiency lives at mid-load."
)

_CABLE_TEACH = (
    "Connectors (from the manufacturer, not the cable bag):\n"
    "  Look up your exact PSU model on the vendor site or datasheet and enter port\n"
    "  counts there. Modular units often ship fewer cables than they have ports —\n"
    "  counting the bag understates what the brick can do.\n"
    "  Categories: 24-pin motherboard (cable is often a 20+4 that locks together;\n"
    "  modern boards use all 24 — still count as one 24-pin port), CPU/EPS (4+4 or\n"
    "  8-pin), SATA, Molex/peripheral, GPU 6+2 PCIe, GPU 12V-2x6 (aka 12VHPWR).\n"
    "  GPU ports are only part of the story."
)


def candor_note() -> str:
    return _CANDOR_NOTES


def cable_teach_blurb() -> str:
    return _CABLE_TEACH


def load_roster() -> dict[str, dict]:
    data = load_json("psu_roster.json") or {}
    return data if isinstance(data, dict) else {}


def lookup_psu(key_or_model: str | None) -> dict | None:
    """Lookup by roster key (exact) or case-insensitive model / brand+model substring."""
    if not key_or_model:
        return None
    q = key_or_model.strip()
    if not q:
        return None
    roster = load_roster()
    if q in roster and isinstance(roster[q], dict):
        return dict(roster[q])
    q_l = q.lower()
    for row in roster.values():
        if not isinstance(row, dict):
            continue
        key = (row.get("key") or "").lower()
        model = (row.get("model") or "").lower()
        brand = (row.get("brand") or "").lower()
        label = f"{brand} {model}".strip()
        if q_l == key or q_l == model or q_l == label:
            return dict(row)
    hits = []
    for row in roster.values():
        if not isinstance(row, dict):
            continue
        label = f"{row.get('brand') or ''} {row.get('model') or ''}".strip().lower()
        if q_l in label or q_l in (row.get("key") or "").lower():
            hits.append(row)
    if len(hits) == 1:
        return dict(hits[0])
    return None


def normalize_plug_type(raw: str) -> str | None:
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    if s in ("24-pin", "24pin", "atx24", "mb", "motherboard"):
        return PLUG_24PIN
    if s in ("cpu", "eps", "eps12v", "4+4", "4+4pin", "cpu8", "cpu8pin"):
        return PLUG_CPU
    if s in ("sata",):
        return PLUG_SATA
    if s in ("molex", "peripheral", "periph", "4pin"):
        return PLUG_MOLEX
    if s in ("6+2", "6p2", "6+2pin", "pcie6+2", "gpu6+2"):
        return PLUG_6P2
    if s in ("12v-2x6", "12v2x6", "12vhpwr", "12vhpwr/2x6", "pcie5", "16pin", "16-pin"):
        return PLUG_12V
    return None


def parse_plugs(spec: str | dict | None) -> dict[str, int]:
    """Parse SPEC or dict → canonical counts (omit zeros)."""
    if spec is None or spec == "":
        return {}
    if isinstance(spec, dict):
        out: dict[str, int] = {}
        for k, v in spec.items():
            typ = normalize_plug_type(str(k))
            if not typ and str(k) in PLUG_ORDER:
                typ = str(k)
            if not typ:
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[typ] = n
        return out
    out: dict[str, int] = {}
    for part in re.split(r"[;,]", str(spec)):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            a, b = part.split(":", 1)
        elif "x" in part.lower():
            m = re.match(r"(\d+)\s*x\s*(.+)", part, re.I)
            if not m:
                continue
            b, a = m.group(1), m.group(2)
        else:
            continue
        typ = normalize_plug_type(a.strip())
        if not typ:
            continue
        try:
            n = int(float(b.strip()))
        except ValueError:
            continue
        if n > 0:
            out[typ] = out.get(typ, 0) + n
    return out


def plugs_to_spec(plugs: dict | None) -> str:
    if not plugs:
        return ""
    return ",".join(f"{k}:{plugs[k]}" for k in PLUG_ORDER if plugs.get(k))


def format_plugs(plugs: dict | None) -> str:
    if not plugs:
        return "—"
    parts = []
    for k in PLUG_ORDER:
        if plugs.get(k):
            parts.append(f"{plugs[k]}× {k}")
    for k, v in sorted(plugs.items()):
        if k not in PLUG_ORDER and v:
            parts.append(f"{v}× {k}")
    return ", ".join(parts) if parts else "—"


def format_plugs_short(plugs: dict | None) -> str:
    """Compact roster-list line: mb/cpu · SATA · GPU…"""
    if not plugs:
        return "—"
    bits = []
    mb = plugs.get(PLUG_24PIN, 0)
    cpu = plugs.get(PLUG_CPU, 0)
    if mb or cpu:
        bits.append(f"mb×{mb or 0}+cpu×{cpu or 0}")
    if plugs.get(PLUG_SATA):
        bits.append(f"SATA×{plugs[PLUG_SATA]}")
    if plugs.get(PLUG_MOLEX):
        bits.append(f"Molex×{plugs[PLUG_MOLEX]}")
    gpu = []
    if plugs.get(PLUG_6P2):
        gpu.append(f"{plugs[PLUG_6P2]}×6+2")
    if plugs.get(PLUG_12V):
        gpu.append(f"{plugs[PLUG_12V]}×12V-2x6")
    if gpu:
        bits.append("GPU " + "+".join(gpu))
    return " · ".join(bits) if bits else "—"


def format_plugs_lines(plugs: dict | None) -> list[tuple[str, str]]:
    """(label, value) rows for lspower — manufacturer port counts."""
    plugs = plugs or {}
    rows = []
    for k in PLUG_ORDER:
        label = PLUG_LABELS.get(k, k)
        n = plugs.get(k, 0)
        val = str(n) if n else "0"
        note = PLUG_NOTES.get(k)
        if note:
            val = f"{val}  ({note})"
        rows.append((label, val))
    return rows


def normalize_atx(raw: str | None) -> str:
    """Map user/datasheet ATX strings to 2 | 3.0 | 3.1 | unknown.

    ATX 2.2/2.3/2.4 etc. all store as ``2`` (2.x family). Decimals on 2.x do not
    change our lab checks; ATX 3.0 vs 3.1 does (transients / 12V-2x6 story).
    """
    s = (raw or "").strip().lower().replace(" ", "")
    if s in ("unknown", "unk", "?"):
        return "unknown"
    s = s.removeprefix("atx")
    if s in ATX_VALUES:
        return s
    # 2, 2.0, 2.4, 2.52 → 2 (pre–ATX 3 family)
    if re.fullmatch(r"2(\.\d+)?", s):
        return "2"
    if s in ("3", "3.0"):
        return "3.0"
    if s == "3.1":
        return "3.1"
    # bare "3.x" typos beyond 3.1 stay unknown rather than inventing tiers
    return "unknown"


def normalize_eff(raw: str | None) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip().lower()
    if s in EFF_VALUES:
        return s
    if "tita" in s:
        return "titanium"
    if "plat" in s:
        return "platinum"
    if "gold" in s:
        return "gold"
    if "silv" in s:
        return "silver"
    if "bronz" in s:
        return "bronze"
    return "unknown"


def roster_row_to_psu(row: dict) -> dict[str, Any]:
    plugs = row.get("plugs")
    if not isinstance(plugs, dict):
        plugs = parse_plugs(plugs)
    model = row.get("model") or ""
    brand = row.get("brand") or ""
    label = f"{brand} {model}".strip()
    return normalize_psu_object({
        "watts": row.get("watts"),
        "model": label or None,
        "roster_key": row.get("key"),
        "atx": row.get("atx") or "unknown",
        "plugs": plugs,
        "eff": row.get("eff"),
    })


def normalize_psu_object(raw: dict | None, *, watts_only: int | None = None) -> dict[str, Any] | None:
    """Return canonical PSU object or None if no watts."""
    if watts_only is not None and (raw is None or not raw):
        return {
            "watts": int(watts_only),
            "model": None,
            "roster_key": None,
            "atx": "unknown",
            "plugs": {},
            "eff": None,
        }
    if not raw and watts_only is None:
        return None
    data = dict(raw or {})
    w = data.get("watts")
    if w is None and watts_only is not None:
        w = watts_only
    if w is None:
        return None
    try:
        watts = int(w)
    except (TypeError, ValueError):
        return None
    atx = normalize_atx(data.get("atx"))
    plugs = parse_plugs(data.get("plugs"))
    eff = normalize_eff(data.get("eff"))
    model = data.get("model")
    if model is not None:
        model = str(model).strip() or None
    rk = data.get("roster_key")
    if rk is not None:
        rk = str(rk).strip() or None
    return {
        "watts": watts,
        "model": model,
        "roster_key": rk,
        "atx": atx if atx in ATX_VALUES else "unknown",
        "plugs": plugs,
        "eff": eff,
    }


def psu_from_record(rec: dict | None) -> dict[str, Any] | None:
    """Extract PSU object from host/build record (psu dict or legacy psu_watts)."""
    if not rec:
        return None
    if isinstance(rec.get("psu"), dict):
        obj = normalize_psu_object(rec["psu"])
        if obj:
            return obj
    if rec.get("psu_watts") is not None:
        try:
            return normalize_psu_object(None, watts_only=int(rec["psu_watts"]))
        except (TypeError, ValueError):
            return None
    return None


def apply_psu_to_record(rec: dict, psu: dict[str, Any]) -> dict:
    """Write canonical psu + mirrored psu_watts onto host/build dict."""
    obj = normalize_psu_object(psu)
    if not obj:
        raise ValueError("psu requires watts")
    rec = dict(rec)
    rec["psu"] = obj
    rec["psu_watts"] = obj["watts"]
    return rec


def format_psu_summary(psu: dict | None) -> str:
    if not psu:
        return "not set"
    bits = [f"{psu['watts']} W"]
    if psu.get("model"):
        bits.append(str(psu["model"]))
    bits.append(f"ATX {psu.get('atx') or 'unknown'}")
    bits.append(format_plugs_short(psu.get("plugs")))
    if psu.get("eff"):
        bits.append(str(psu["eff"]))
    return " · ".join(bits)


def list_roster_sorted() -> list[dict]:
    rows = [v for v in load_roster().values() if isinstance(v, dict)]
    return sorted(rows, key=lambda r: (r.get("watts") or 0, r.get("brand") or "", r.get("model") or ""))
