"""Expansion headroom — report-only free watts / PCIe lanes / open slots.

Composes budget + topology + DMI slots for live hosts (and later lsbuild BOMs).
No automatic “typical dual-GPU” finding — facts only.
"""

from __future__ import annotations

from typing import Any


_NOTE_WRAP = 100


def _wrap_words(text: str, width: int = _NOTE_WRAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if width < 20:
        width = 20
    lines: list[str] = []
    words = text.split()
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


def _power_free(budget: dict | None) -> dict[str, Any]:
    budget = budget or {}
    psu = budget.get("psu_watts")
    used = budget.get("total_w")
    out: dict[str, Any] = {
        "psu_watts": psu,
        "budget_w": used,
        "free_w": None,
        "pct_of_psu_used": None,
        "psu_source": budget.get("psu_source"),
    }
    if psu is not None and used is not None and psu > 0:
        free = int(psu) - int(used)
        out["free_w"] = free
        out["pct_of_psu_used"] = round(100.0 * int(used) / int(psu), 1)
    return out


def _lane_free(topo: dict | None) -> dict[str, Any]:
    roots_in = (topo or {}).get("roots") or []
    roots_out: list[dict] = []
    total_free = 0
    any_budget = False
    oversub_n = 0
    for r in roots_in:
        max_w = r.get("max_width")
        consumed = r.get("consumed_width")
        free = None
        if max_w is not None and consumed is not None:
            any_budget = True
            free = int(max_w) - int(consumed)
            if free < 0:
                free = 0
            total_free += free
        if r.get("oversubscribed"):
            oversub_n += 1
        roots_out.append({
            "bdf": r.get("bdf"),
            "max_width": max_w,
            "consumed_width": consumed,
            "free_width": free,
            "oversubscribed": bool(r.get("oversubscribed")),
        })
    return {
        "roots": roots_out,
        "free_lanes_sum": total_free if any_budget else None,
        "oversubscribed_roots": oversub_n,
        "available": any_budget,
    }


def _slot_free(dmi_slots: list | None, populated: list | None = None) -> dict[str, Any]:
    """Classify DMI Type 9 slots: Available vs In Use / other."""
    slots = dmi_slots or []
    open_list: list[str] = []
    used_list: list[str] = []
    other_list: list[str] = []
    for s in slots:
        des = (s.get("designation") or "?").strip()
        usage = (s.get("usage") or "").strip().lower()
        typ = (s.get("type") or "").strip()
        label = des if not typ else f"{des} ({typ})"
        if usage == "available":
            open_list.append(label)
        elif usage in ("in use", "in-use", "occupied"):
            used_list.append(label)
        else:
            other_list.append(label)
    return {
        "open": open_list,
        "used": used_list,
        "other": other_list,
        "open_count": len(open_list),
        "used_count": len(used_list),
        "dmi_count": len(slots),
        "populated_endpoints": len(populated or []),
        "available": bool(slots),
    }


def compute_expansion(
    *,
    budget: dict | None = None,
    topology: dict | None = None,
    dmi_slots: list | None = None,
    populated: list | None = None,
) -> dict[str, Any]:
    """Build expansion headroom block for lsstack report / JSON."""
    if budget is None:
        from lsstack.budget import estimate_system_budget
        budget = estimate_system_budget()
    if topology is None:
        from lsstack.topology import get_topology_info
        topology = get_topology_info()
    if dmi_slots is None:
        from lsstack.slots import _type9_slots, get_populated_endpoints
        dmi_slots = _type9_slots()
        if populated is None:
            populated = get_populated_endpoints()

    return {
        "power": _power_free(budget),
        "lanes": _lane_free(topology),
        "slots": _slot_free(dmi_slots, populated),
        "note": (
            "Report-only free resources (not a dual-GPU oracle). "
            "More detail: lspower, lstopology, lsslots."
        ),
    }


def print_expansion_human(exp: dict | None = None) -> None:
    exp = exp if exp is not None else compute_expansion()
    power = exp.get("power") or {}
    lanes = exp.get("lanes") or {}
    slots = exp.get("slots") or {}

    labels = [
        "Power", "Lanes", "Slots", "Open", "Used", "Note", "Root",
    ]
    for r in lanes.get("roots") or []:
        labels.append(str(r.get("bdf") or "root"))
    w = max(14, *(len(x) for x in labels))

    print("\n" + "=" * 60)
    print("  Expansion headroom")
    print("=" * 60)

    # Power
    if power.get("free_w") is not None:
        free = power["free_w"]
        used = power.get("budget_w")
        psu = power.get("psu_watts")
        pct = power.get("pct_of_psu_used")
        src = power.get("psu_source") or "?"
        sign = f"+{free}" if free >= 0 else str(free)
        _kv("Power", f"{sign} W free of {psu} W PSU ({pct:.0f}% budget used)", w)
        _kv("Budget", f"{used} W design  (from {src})", w)
    else:
        _kv("Power", "N/A — lslab enroll / set-psu to declare capacity", w)

    # Lanes
    if lanes.get("available"):
        free_sum = lanes.get("free_lanes_sum")
        over = lanes.get("oversubscribed_roots") or 0
        extra = f"; {over} root(s) oversubscribed" if over else ""
        _kv("Lanes", f"{free_sum} free across CPU roots (max - consumed){extra}", w)
        for r in lanes.get("roots") or []:
            bdf = r.get("bdf") or "?"
            mx, c, f = r.get("max_width"), r.get("consumed_width"), r.get("free_width")
            if f is None:
                _kv(bdf, f"max x{mx if mx is not None else '?'}  consumed ? — incomplete", w)
            else:
                flag = "  OVERSUB" if r.get("oversubscribed") else ""
                _kv(bdf, f"max x{mx}  consumed x{c}  free x{f}{flag}", w)
    else:
        _kv("Lanes", "N/A — no CPU-root lane budgets from lstopology yet", w)

    # Slots
    if slots.get("available"):
        _kv(
            "Slots",
            f"{slots.get('open_count', 0)} open / {slots.get('used_count', 0)} in use "
            f"({slots.get('dmi_count', 0)} DMI Type 9)",
            w,
        )
        open_s = slots.get("open") or []
        if open_s:
            _kv("Open", ", ".join(open_s[:8]) + ("…" if len(open_s) > 8 else ""), w)
        used_s = slots.get("used") or []
        if used_s:
            _kv("Used", ", ".join(used_s[:8]) + ("…" if len(used_s) > 8 else ""), w)
    else:
        _kv(
            "Slots",
            "N/A — needs sudo for DMI Type 9 (see lsslots)",
            w,
        )

    _kv("Note", exp.get("note"), w, wrap=_NOTE_WRAP)
    print()
