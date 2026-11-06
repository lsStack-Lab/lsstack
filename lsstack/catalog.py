"""lscatalog: lab overlay catalog — add GPU/NIC/PSU rows without editing shipped JSON.

Writes under ~/.lsstack/catalog/ (or LSSTACK_CATALOG / catalog_dir= in ~/.lsstack.conf).
Overlay wins over shipped config at load_json() time.

Agent assist: ``lscatalog prompt`` / post-``misses`` asks brand+model, prints a
fill-in form (C&P into an LLM or datasheet), then imports the pasted answers.
``lscatalog add KIND --from-text`` still works for file/stdin import.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from lsstack.common import (
    catalog_dir,
    config_dir,
    gpu_spec_key,
    load_json,
    lookup_gpu_specs,
    lookup_nic_specs,
    normalize_pci_hex,
    write_catalog_overlay,
)


_LABEL_W = 14
_STATUS_W = 6  # width of "[ok]" / "[miss]" / "[na]" cell


def _kv(label: str, value: str | None, width: int = _LABEL_W) -> None:
    """Right-justified label column (same convention as lslab / lspower)."""
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _status(tag: str) -> str:
    raw = f"[{tag}]"
    return raw + (" " * max(0, _STATUS_W - len(raw)))


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except EOFError:
        raw = ""
    if not raw and default is not None:
        return str(default)
    return raw


def _prompt_yes(msg: str, *, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"{msg} [{d}]: ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes")


def _list_live_gpus() -> list[dict]:
    from lsstack.gpu import get_gpu_info

    gpus, _ = get_gpu_info()
    return gpus


def _list_live_nics() -> list[dict]:
    from lsstack.network import get_nic_devices

    return get_nic_devices()


def cmd_path(_args: argparse.Namespace) -> int:
    cdir = catalog_dir()
    print("\nCatalog overlay")
    _kv("Dir", cdir)
    _kv("Exists", "yes" if os.path.isdir(cdir) else "no")
    _kv("Note", "Shipped config is separate (LSSTACK_CONFIG). Overlay wins on same key.")
    print()
    return 0


def _read_json_file(path: str) -> Any:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _catalog_layers(filename: str) -> tuple[dict, dict, dict]:
    """Return (shipped, overlay, merged) dicts for a catalog JSON name."""
    shipped = _read_json_file(os.path.join(config_dir(), filename))
    if not isinstance(shipped, dict):
        shipped = {}
    cdir = catalog_dir()
    overlay = _read_json_file(os.path.join(cdir, filename))
    if overlay is None and filename.endswith(".json"):
        stem = filename[: -len(".json")]
        overlay = _read_json_file(os.path.join(cdir, f"{stem}.overlay.json"))
    if not isinstance(overlay, dict):
        overlay = {}
    merged = load_json(filename) or {}
    if not isinstance(merged, dict):
        merged = {}
    return shipped, overlay, merged


def _src_tag(key: str, shipped: dict, overlay: dict) -> str:
    in_o = key in overlay
    in_s = key in shipped
    if in_o and in_s:
        return "[lab] "  # overlay wins
    if in_o:
        return "[lab] "
    return "[ship]"


def _list_psu() -> None:
    from lsstack.psu_roster import format_plugs_short

    shipped, overlay, merged = _catalog_layers("psu_roster.json")
    print("  PSU")
    if not merged:
        _kv("Note", "empty")
        return
    rows = sorted(
        ((k, v) for k, v in merged.items() if isinstance(v, dict)),
        key=lambda kv: (
            kv[1].get("watts") or 0,
            kv[1].get("brand") or "",
            kv[1].get("model") or "",
            kv[0],
        ),
    )
    w = max(_LABEL_W, max((len(k) for k, _ in rows), default=_LABEL_W))
    for key, row in rows:
        brand = row.get("brand") or ""
        model = row.get("model") or ""
        watts = row.get("watts")
        atx = row.get("atx") or "?"
        plugs = format_plugs_short(row.get("plugs"))
        eff = row.get("eff") or ""
        bits = [
            f"{watts} W" if watts is not None else "? W",
            f"{brand} {model}".strip(),
            f"ATX {atx}",
        ]
        if plugs:
            bits.append(plugs)
        if eff:
            bits.append(str(eff))
        _kv(key, f"{_src_tag(key, shipped, overlay)}  {' · '.join(bits)}", width=w)
    ship_only = len(set(shipped) - set(overlay))
    _kv("Count", f"{len(rows)}  (lab={len(overlay)}, shipped-only={ship_only})", width=w)


def _pci_row_summary(kind: str, row: dict) -> str:
    model = row.get("model") or ""
    if kind == "gpu":
        size = row.get("size_gib")
        tdp = row.get("tdp_watts")
        bus = row.get("bus_interface") or ""
        bits = [model]
        if size is not None:
            bits.append(f"{size} GiB")
        if tdp is not None:
            bits.append(f"{tdp} W")
        if bus:
            bits.append(str(bus))
    else:
        speed = row.get("port_speed_gbps")
        ports = row.get("ports")
        rdma = row.get("rdma") or ""
        bits = [model]
        if speed is not None:
            bits.append(f"{speed} Gbps")
        if ports is not None:
            bits.append(f"{ports}p")
        if rdma and rdma != "none":
            bits.append(str(rdma))
    return " · ".join(b for b in bits if b)


def _list_pci(kind: str, *, show_all_shipped: bool) -> None:
    filename = "gpu_pci_id.json" if kind == "gpu" else "nic_pci_id.json"
    label = "GPU" if kind == "gpu" else "NIC"
    shipped, overlay, merged = _catalog_layers(filename)
    print(f"  {label}")
    if not merged:
        _kv("Note", "empty")
        return

    # GPUs: default to lab rows only (shipped set is huge); --all dumps everything.
    # NICs: always show full merged list (small).
    if kind == "gpu" and not show_all_shipped:
        lab_rows = sorted(
            ((k, v) for k, v in overlay.items() if isinstance(v, dict)),
            key=lambda kv: kv[0],
        )
        w = max(_LABEL_W, max((len(k) for k, _ in lab_rows), default=_LABEL_W))
        if lab_rows:
            for key, row in lab_rows:
                _kv(
                    key,
                    f"{_src_tag(key, shipped, overlay)}  {_pci_row_summary(kind, row)}",
                    width=w,
                )
        else:
            _kv("Lab", "(none yet — lscatalog add gpu / prompt)", width=w)
        ship_only = len(set(shipped) - set(overlay))
        _kv(
            "Count",
            f"lab={len(lab_rows)}, shipped={len(shipped)} "
            f"(shipped-only={ship_only}; full dump: lscatalog list gpu --all)",
            width=w,
        )
        return

    rows = sorted(
        ((k, v) for k, v in merged.items() if isinstance(v, dict)),
        key=lambda kv: kv[0],
    )
    w = max(_LABEL_W, max((len(k) for k, _ in rows), default=_LABEL_W))
    for key, row in rows:
        _kv(
            key,
            f"{_src_tag(key, shipped, overlay)}  {_pci_row_summary(kind, row)}",
            width=w,
        )
    ship_only = len(set(shipped) - set(overlay))
    _kv("Count", f"{len(rows)}  (lab={len(overlay)}, shipped-only={ship_only})", width=w)


def cmd_list(args: argparse.Namespace) -> int:
    kinds = [args.kind] if getattr(args, "kind", None) else ["psu", "gpu", "nic"]
    show_all = bool(getattr(args, "all", False))
    print("\nCatalog")
    _kv("Overlay", catalog_dir())
    _kv("Shipped", config_dir())
    _kv("Note", "[lab] = your overlay (wins); [ship] = package only")
    print()
    for kind in kinds:
        if kind == "psu":
            _list_psu()
        else:
            _list_pci(kind, show_all_shipped=show_all)
        print()
    return 0


def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "psu"


def _split_brand_model(name: str) -> tuple[str, str]:
    """Best-effort split of 'Brand Model…' into (brand, model)."""
    name = (name or "").strip()
    if not name:
        return "", ""
    parts = name.split(None, 1)
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[1]


def _build_agent_form(
    kind: str,
    *,
    brand: str = "",
    model: str = "",
    vendor: str = "",
    device: str = "",
) -> str:
    """Minimal key:value form for an LLM (or datasheet) to fill. No product lore."""
    if kind == "psu":
        have_id = bool(brand.strip() and model.strip())
        key = _slug(f"{brand}-{model}") if have_id else ""
        return (
            "Fill every blank from the manufacturer PSU datasheet. "
            "Reply with key: value lines only (no JSON, no markdown).\n"
            "Keep identity lines unchanged.\n"
            "\n"
            "Ports = physical sockets on the PSU (not cables in the box).\n"
            "If the datasheet labels CPU / PCIe / SATA / Molex counts, use those numbers.\n"
            "If sockets are shared (e.g. 8-pin CPU-or-PCIe, or SATA/Molex bank):\n"
            "  allocate the physical socket total across the fields so the numbers sum to\n"
            "  what is on the panel (datasheet split if given; else even split), and explain\n"
            "  the sharing in notes. Never put 0 on a type that physically exists.\n"
            "0 means the PSU truly has none of that connector. Prefer a best estimate over 0.\n"
            "atx: 2|3.0|3.1. eff: white|bronze|silver|gold|platinum|titanium|unknown.\n"
            "Required: ≥1× 24-pin, ≥1× cpu, ≥1 GPU lead (6+2 and/or 12V-2x6).\n"
            "datasheet: required — direct URL to the manufacturer datasheet/spec PDF or product page.\n"
            "\n"
            f"key: {key}\n"
            f"brand: {brand}\n"
            f"model: {model}\n"
            "watts:\n"
            "atx:\n"
            "eff:\n"
            "24-pin:\n"
            "cpu:\n"
            "sata:\n"
            "molex:\n"
            "6+2:\n"
            "12V-2x6:\n"
            "datasheet:\n"
            "notes:\n"
        )
    if kind == "gpu":
        vendor = (normalize_pci_hex(vendor) or vendor or "").lower()
        device = (normalize_pci_hex(device) or device or "").lower()
        return (
            "Fill every blank for this GPU. "
            "Reply with key: value lines only (no JSON, no markdown).\n"
            "Keep vendor/device/model if set. size_gib = VRAM GiB. "
            "bus_interface e.g. PCIe 4.0 (blank if unknown).\n"
            "datasheet: required — direct URL to the manufacturer datasheet/spec PDF or product page.\n"
            "\n"
            f"vendor: {vendor}\n"
            f"device: {device}\n"
            f"model: {model}\n"
            "size_gib:\n"
            "tdp_watts:\n"
            "pcie_lanes:\n"
            "bus_interface:\n"
            "datasheet:\n"
            "notes:\n"
        )
    vendor = (normalize_pci_hex(vendor) or vendor or "").lower()
    device = (normalize_pci_hex(device) or device or "").lower()
    return (
        "Fill every blank for this NIC. "
        "Reply with key: value lines only (no JSON, no markdown).\n"
        "Keep vendor/device/model if set. "
        "port_speed_gbps numeric. rdma: none|roce|ib. tdp_watts blank if unknown.\n"
        "datasheet: required — direct URL to the manufacturer datasheet/spec PDF or product page.\n"
        "\n"
        f"vendor: {vendor}\n"
        f"device: {device}\n"
        f"model: {model}\n"
        "port_speed_gbps:\n"
        "ports:\n"
        "rdma:\n"
        "tdp_watts:\n"
        "datasheet:\n"
        "notes:\n"
    )


def _print_howto_and_form(kind: str, form: str) -> None:
    label = {"psu": "PSU", "gpu": "GPU", "nic": "NIC"}[kind]
    print()
    print(f"--- {label} form (C&P into an LLM, or fill from the datasheet) ---")
    print(
        "ChatGPT / Claude / Gemini / Copilot / Grok — paste the block below, get blanks filled."
    )
    print()
    print(form.rstrip())
    print()


def _read_pasted_answers() -> str:
    print("Paste the filled form as multiple lines (keep each key: value on its own line).")
    print("When the paste is done, press Ctrl-D to save (not Enter — Enter only adds a blank line).")
    print("Or press Ctrl-D now with nothing pasted to skip.")
    try:
        return sys.stdin.read()
    except KeyboardInterrupt:
        print()
        return ""


def _warn_if_no_datasheet(obj: dict) -> None:
    notes = str(obj.get("notes") or "")
    if "datasheet:" not in notes.lower() and "http" not in notes.lower():
        print(
            "Note: no datasheet URL found — prefer a manufacturer link next time.",
            file=sys.stderr,
        )


def _import_pasted(kind: str, raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        print("Skipped (no answers pasted).")
        return 0
    try:
        obj = _parse_import_blob(kind, raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Bad import: {e}", file=sys.stderr)
        return 1
    if not isinstance(obj, dict):
        print("Import root must be an object.", file=sys.stderr)
        return 1
    _warn_if_no_datasheet(obj)
    if kind == "psu":
        return _apply_psu_json(obj)
    if kind == "gpu":
        return _apply_gpu_json(obj)
    return _apply_nic_json(obj)


def _run_prompt_session(
    kind: str,
    *,
    brand: str = "",
    model: str = "",
    vendor: str = "",
    device: str = "",
    collect: bool = True,
) -> int:
    """Print howto + form; optionally collect pasted answers and write overlay."""
    form = _build_agent_form(
        kind, brand=brand, model=model, vendor=vendor, device=device
    )
    _print_howto_and_form(kind, form)
    if not collect:
        print("When ready, import with:")
        print(f"  lscatalog add {kind} --from-text -")
        print(f"  # or: lscatalog add {kind} --from-text reply.txt")
        return 0
    if not sys.stdin.isatty():
        print("Non-interactive stdin — not waiting for answers.")
        print(f"Import later: lscatalog add {kind} --from-text reply.txt")
        return 0
    return _import_pasted(kind, _read_pasted_answers())


def _gather_psu_identity(*, name_hint: str = "") -> tuple[str, str] | None:
    brand_d, model_d = _split_brand_model(name_hint)
    brand = _prompt("Manufacturer / brand", brand_d or None)
    model = _prompt("Exact model", model_d or None)
    if not brand or not model:
        print("Need manufacturer and model.", file=sys.stderr)
        return None
    return brand, model


def _gather_gpu_identity(
    *,
    name_hint: str = "",
    vendor: str = "",
    device: str = "",
    model_hint: str = "",
) -> tuple[str, str, str] | None:
    """Return (vendor, device, model) or None."""
    if not vendor:
        vendor = _prompt("PCI vendor (4 hex, e.g. 10de)", "10de")
    if not device:
        device = _prompt("PCI device (4 hex)", None)
    vendor = normalize_pci_hex(vendor) or ""
    device = normalize_pci_hex(device) or ""
    if not vendor or not device:
        print("Need vendor and device.", file=sys.stderr)
        return None
    default_model = model_hint or name_hint or f"{vendor}:{device}"
    model = _prompt("Marketing / board model", default_model)
    if not model:
        print("Need a model name.", file=sys.stderr)
        return None
    return vendor, device, model


def _gather_nic_identity(
    *,
    name_hint: str = "",
    vendor: str = "",
    device: str = "",
    model_hint: str = "",
) -> tuple[str, str, str] | None:
    if not vendor:
        vendor = _prompt("PCI vendor (4 hex, e.g. 15b3)", "15b3")
    if not device:
        device = _prompt("PCI device (4 hex)", None)
    vendor = normalize_pci_hex(vendor) or ""
    device = normalize_pci_hex(device) or ""
    if not vendor or not device:
        print("Need vendor and device.", file=sys.stderr)
        return None
    default_model = model_hint or name_hint or f"{vendor}:{device}"
    model = _prompt("Marketing model", default_model)
    if not model:
        print("Need a model name.", file=sys.stderr)
        return None
    return vendor, device, model


def cmd_prompt(args: argparse.Namespace) -> int:
    """Ask identity, print C&P form, collect answers into the lab overlay."""
    kind = args.kind
    hint = (args.name or "").strip()
    tty = sys.stdin.isatty()

    if kind == "psu":
        if tty:
            ident = _gather_psu_identity(name_hint=hint)
            if not ident:
                return 1
            brand, model = ident
        else:
            brand, model = _split_brand_model(hint)
        return _run_prompt_session(kind, brand=brand, model=model, collect=tty)

    if kind == "gpu":
        if tty:
            ident = _gather_gpu_identity(name_hint=hint)
            if not ident:
                return 1
            vendor, device, model = ident
        else:
            vendor, device, model = "", "", hint
        return _run_prompt_session(
            kind, vendor=vendor, device=device, model=model, collect=tty
        )

    # nic
    if tty:
        ident = _gather_nic_identity(name_hint=hint)
        if not ident:
            return 1
        vendor, device, model = ident
    else:
        vendor, device, model = "", "", hint
    return _run_prompt_session(
        kind, vendor=vendor, device=device, model=model, collect=tty
    )


def _offer_gap_prompts(
    gpu_misses: list[dict],
    nic_misses: list[dict],
    *,
    offer_psu: bool,
    psu_gap_note: str | None = None,
) -> int:
    """After misses report: optionally run prompt sessions for gaps."""
    if not sys.stdin.isatty():
        return 0
    n = len(gpu_misses) + len(nic_misses) + (1 if offer_psu else 0)
    if n == 0:
        return 0

    bits: list[str] = []
    if gpu_misses:
        bits.append(f"{len(gpu_misses)} GPU miss(es)")
    if nic_misses:
        bits.append(f"{len(nic_misses)} NIC miss(es)")
    if offer_psu:
        bits.append("PSU miss")
    print(f"Gaps: {', '.join(bits)}.")
    if not _prompt_yes("Run catalog prompts for these now?", default=True):
        print("OK — later: lscatalog prompt psu|gpu|nic")
        return 0

    rc = 0
    for g in gpu_misses:
        key = gpu_spec_key(g.get("vendor"), g.get("device"))
        name = (g.get("specs") or {}).get("model") or g.get("lspci_name") or key
        print(f"\nGPU gap: {key}  {name}")
        if not _prompt_yes(f"Fill catalog form for GPU {key}?", default=True):
            continue
        vendor = normalize_pci_hex(g.get("vendor")) or ""
        device = normalize_pci_hex(g.get("device")) or ""
        ident = _gather_gpu_identity(
            vendor=vendor, device=device, model_hint=name
        )
        if not ident:
            rc = 1
            continue
        v, d, model = ident
        code = _run_prompt_session("gpu", vendor=v, device=d, model=model)
        if code:
            rc = code

    for n_dev in nic_misses:
        key = gpu_spec_key(n_dev.get("vendor"), n_dev.get("device"))
        name = n_dev.get("model") or key
        print(f"\nNIC gap: {key}  {name}")
        if not _prompt_yes(f"Fill catalog form for NIC {key}?", default=True):
            continue
        vendor = normalize_pci_hex(n_dev.get("vendor")) or ""
        device = normalize_pci_hex(n_dev.get("device")) or ""
        ident = _gather_nic_identity(
            vendor=vendor, device=device, model_hint=name
        )
        if not ident:
            rc = 1
            continue
        v, d, model = ident
        code = _run_prompt_session("nic", vendor=v, device=d, model=model)
        if code:
            rc = code

    if offer_psu:
        print(
            "\nPSU gap: "
            + (
                psu_gap_note
                or "not declared in lslab yet "
                "(kernel never sees PSUs — enroll or add a catalog SKU)."
            )
        )
        if _prompt_yes("Fill catalog form for a PSU SKU?", default=True):
            ident = _gather_psu_identity()
            if ident:
                brand, model = ident
                code = _run_prompt_session("psu", brand=brand, model=model)
                if code:
                    rc = code
            else:
                rc = 1
    return rc


def cmd_misses(_args: argparse.Namespace) -> int:
    gpu_db = load_json("gpu_pci_id.json") or {}
    nic_db = load_json("nic_pci_id.json") or {}
    print("\nCatalog misses")
    _kv("Scope", "GPU/NIC: live vs shipped+overlay; PSU: lab host vs overlay pick list")
    print()

    gpu_misses: list[dict] = []
    gpus = _list_live_gpus()
    if not gpus:
        _kv("GPU", f"{_status('na')}  none detected")
    else:
        for g in gpus:
            key = gpu_spec_key(g.get("vendor"), g.get("device"))
            hit = lookup_gpu_specs(gpu_db, g.get("vendor"), g.get("device"))
            name = (g.get("specs") or {}).get("model") or g.get("lspci_name") or key
            _kv("GPU", f"{_status('ok' if hit else 'miss')}  {key}  {name}")
            if not hit:
                gpu_misses.append(g)

    nic_misses: list[dict] = []
    nics = _list_live_nics()
    if not nics:
        _kv("NIC", f"{_status('na')}  none detected")
    else:
        for n in nics:
            key = gpu_spec_key(n.get("vendor"), n.get("device"))
            hit = lookup_nic_specs(nic_db, n.get("vendor"), n.get("device"))
            name = n.get("model") or key
            _kv("NIC", f"{_status('ok' if hit else 'miss')}  {key}  {name}")
            if not hit:
                nic_misses.append(n)

    # PSU is never in sysfs. Lab host row is not the overlay pick list.
    from lsstack import lab_inventory as lab
    from lsstack.psu_roster import format_psu_summary, lookup_psu

    lab_power = lab.resolve_lab_power()
    psu_obj = lab_power.get("psu")
    psu_watts = lab_power.get("psu_watts")
    src = lab_power.get("psu_source") or "lab"
    catalog_hit = False
    if psu_obj:
        queries = [
            psu_obj.get("roster_key"),
            psu_obj.get("model"),
            f"{psu_obj.get('brand') or ''} {psu_obj.get('model') or ''}".strip(),
        ]
        catalog_hit = any(q and lookup_psu(str(q)) for q in queries)

    offer_psu = True
    psu_gap_note = None
    if psu_watts is not None and catalog_hit:
        _kv(
            "PSU",
            f"{_status('ok')}  {format_psu_summary(psu_obj)} (from {src}; in overlay/roster)",
        )
        offer_psu = False
    elif psu_watts is not None:
        summary = format_psu_summary(psu_obj) if psu_obj else f"{psu_watts} W"
        _kv(
            "PSU",
            f"{_status('miss')}  {summary} (from {src}; host-only — "
            "not in overlay pick list)",
        )
        psu_gap_note = (
            f"{summary} is on this host only. Add it to the overlay so "
            "lslab enroll lists it on the next machine."
        )
    elif lab_power.get("enrolled"):
        _kv(
            "PSU",
            f"{_status('miss')}  host enrolled but no PSU — "
            "lslab set-psu, or lscatalog prompt psu",
        )
    else:
        _kv(
            "PSU",
            f"{_status('miss')}  not in lab inventory — "
            "lslab enroll, or lscatalog prompt psu then set-psu",
        )
    print()
    return _offer_gap_prompts(
        gpu_misses, nic_misses, offer_psu=offer_psu, psu_gap_note=psu_gap_note
    )


_PSU_PLUG_KEYS = ("24-pin", "cpu", "sata", "molex", "6+2", "12V-2x6")
_EFF_HELP = "bronze|silver|gold|platinum|titanium|unknown"


def _parse_eff(raw: str | None) -> str | None:
    """Require an explicit 80 PLUS tier. Blank must not become gold."""
    from lsstack.psu_roster import EFF_VALUES, normalize_eff

    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return None
    eff = normalize_eff(s)
    if eff not in EFF_VALUES:
        return None
    if eff == "unknown" and s.lower() not in ("unknown", "unk", "?"):
        return None
    return eff


def _strip_fence(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _read_raw_blob(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def _normalize_form_key(key: str) -> str | None:
    """Map a form label to a canonical catalog field name, or None to skip."""
    k = key.strip()
    if not k:
        return None
    low = k.lower()
    # Plug labels (preserve canonical casing used by psu_roster)
    plug_map = {p.lower(): p for p in _PSU_PLUG_KEYS}
    if low in plug_map:
        return plug_map[low]
    if " " in low:
        return None
    if not re.fullmatch(r"[a-z0-9_./+-]+", low):
        return None
    return low


def _parse_kv_form(raw: str) -> dict[str, Any]:
    """Parse Gemini-friendly ``key: value`` lines into a flat dict."""
    raw = _strip_fence(raw)
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            continue
        key, _, val = s.partition(":")
        canon = _normalize_form_key(key)
        if not canon:
            continue
        val = val.strip()
        if canon in out and canon == "notes":
            out[canon] = f"{out[canon]} {val}".strip()
        else:
            out[canon] = val
    return out


def _notes_with_datasheet(notes: Any, datasheet: Any) -> str | None:
    """Fold datasheet URL into notes for storage (no separate schema field)."""
    n = (str(notes).strip() if notes is not None else "") or ""
    d = (str(datasheet).strip() if datasheet is not None else "") or ""
    if d:
        n = f"{n} datasheet: {d}".strip() if n else f"datasheet: {d}"
    return n or None


def _kv_to_catalog_obj(kind: str, flat: dict[str, Any]) -> dict[str, Any]:
    """Map flat key:value form into the dict shape apply_* expects."""
    notes = _notes_with_datasheet(flat.get("notes"), flat.get("datasheet"))
    if kind == "psu":
        plugs = {}
        for pk in _PSU_PLUG_KEYS:
            if pk in flat and str(flat[pk]).strip() != "":
                plugs[pk] = flat[pk]
        return {
            "key": flat.get("key"),
            "brand": flat.get("brand"),
            "model": flat.get("model"),
            "watts": flat.get("watts"),
            "atx": flat.get("atx"),
            "eff": flat.get("eff"),
            "plugs": plugs,
            "notes": notes,
        }
    if kind == "gpu":
        return {
            "vendor": flat.get("vendor"),
            "device": flat.get("device"),
            "model": flat.get("model"),
            "size_gib": flat.get("size_gib"),
            "tdp_watts": flat.get("tdp_watts") or None,
            "pcie_lanes": flat.get("pcie_lanes") or None,
            "bus_interface": flat.get("bus_interface") or None,
            "notes": notes,
        }
    # nic
    tdp = flat.get("tdp_watts")
    if tdp is not None and str(tdp).strip() == "":
        tdp = None
    return {
        "vendor": flat.get("vendor"),
        "device": flat.get("device"),
        "model": flat.get("model"),
        "port_speed_gbps": flat.get("port_speed_gbps"),
        "ports": flat.get("ports"),
        "rdma": flat.get("rdma"),
        "tdp_watts": tdp,
        "notes": notes,
    }


def _parse_import_blob(kind: str, raw: str) -> dict[str, Any]:
    """Accept key:value form (preferred) or JSON object."""
    raw_st = _strip_fence(raw)
    # JSON path if it looks like an object
    if raw_st.lstrip().startswith("{"):
        obj = json.loads(raw_st)
        if not isinstance(obj, dict):
            raise ValueError("JSON root must be an object")
        if kind == "psu" and "watts" not in obj and len(obj) == 1:
            inner = next(iter(obj.values()))
            if isinstance(inner, dict):
                if "key" not in inner:
                    inner = {**inner, "key": next(iter(obj.keys()))}
                obj = inner
        notes = _notes_with_datasheet(obj.get("notes"), obj.get("datasheet"))
        if notes is not None:
            obj = {**obj, "notes": notes}
        obj.pop("datasheet", None)
        return obj
    flat = _parse_kv_form(raw_st)
    if not flat:
        raise ValueError("no key: value lines found")
    return _kv_to_catalog_obj(kind, flat)


def _apply_psu_json(obj: dict) -> int:
    from lsstack.psu_roster import ATX_VALUES, PLUG_ORDER, normalize_atx

    brand = (obj.get("brand") or "").strip()
    model = (obj.get("model") or "").strip()
    key = (obj.get("key") or "").strip() or _slug(f"{brand}-{model}")
    if not brand or not model:
        print("JSON needs brand and model.", file=sys.stderr)
        return 1
    try:
        watts_i = int(float(obj["watts"]))
    except (KeyError, TypeError, ValueError):
        print("JSON needs numeric watts.", file=sys.stderr)
        return 1
    atx = normalize_atx(str(obj.get("atx") or ""))
    if atx not in ATX_VALUES or atx == "unknown":
        print("JSON atx must be 2|3.0|3.1.", file=sys.stderr)
        return 1
    plugs_in = obj.get("plugs") or {}
    if not isinstance(plugs_in, dict):
        print("JSON plugs must be an object.", file=sys.stderr)
        return 1
    plugs: dict = {}
    for pk in PLUG_ORDER:
        try:
            n = int(float(plugs_in.get(pk, 0) or 0))
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            plugs[pk] = n
    if plugs.get("24-pin", 0) < 1 or plugs.get("cpu", 0) < 1:
        print("Need ≥1× 24-pin and ≥1× CPU/EPS.", file=sys.stderr)
        return 1
    if plugs.get("6+2", 0) + plugs.get("12V-2x6", 0) < 1:
        print("Need at least one GPU power lead (6+2 and/or 12V-2x6).", file=sys.stderr)
        return 1
    eff = _parse_eff(obj.get("eff"))
    if not eff:
        print(f"JSON needs eff: {_EFF_HELP} (blank is not gold).", file=sys.stderr)
        return 1
    row = {
        "key": key,
        "brand": brand,
        "model": model,
        "watts": watts_i,
        "atx": atx,
        "plugs": plugs,
        "eff": eff,
        "notes": (obj.get("notes") or "lab overlay").strip() or "lab overlay",
    }
    path = write_catalog_overlay("psu_roster.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    return _after_psu_overlay_written(key, row)


def _after_psu_overlay_written(key: str, row: dict) -> int:
    """Clarify roster is live; offer to apply the new SKU on this host."""
    from lsstack.psu_roster import (
        apply_psu_to_record,
        format_psu_summary,
        roster_row_to_psu,
    )

    print("In lab catalog now — lslab enroll / set-psu will list it (no reload needed).")
    if not sys.stdin.isatty():
        return 0
    if not _prompt_yes(
        "Apply this PSU to this host now (like lslab set-psu)?",
        default=True,
    ):
        print("OK — later: lslab set-psu   or   lslab enroll")
        return 0

    from lsstack import lab_inventory as lab

    data = lab.load_inventory()
    ident = lab.detect_identity()
    host_id = ident.get("system_id") or ident.get("hostname")
    found = lab.find_host(data, system_id=host_id, hostname=host_id)
    if not found:
        print(f"Host not enrolled ({host_id or 'unknown'}).")
        if _prompt_yes("Run lslab enroll now (will use this PSU)?", default=True):
            from lsstack.lslab import cmd_enroll

            # roster_key alone prefills from the overlay row inside enroll.
            return cmd_enroll(["--psu-key", key])
        print("OK — later: lslab enroll")
        return 0

    psu = roster_row_to_psu(row)
    k, h = found
    h = apply_psu_to_record(h, psu)
    lab.upsert_host(data, h)
    inv_path = lab.save_inventory(data)
    print(f"Host {k!r} PSU {format_psu_summary(psu)} → {inv_path}")
    return 0


def _apply_gpu_json(obj: dict) -> int:
    vendor = normalize_pci_hex(obj.get("vendor")) or ""
    device = normalize_pci_hex(obj.get("device")) or ""
    if not vendor or not device:
        print("JSON needs vendor and device (4 hex).", file=sys.stderr)
        return 1
    key = f"{vendor}:{device}"
    model = (obj.get("model") or "").strip() or key
    row: dict = {
        "vendor": vendor,
        "device": device,
        "model": model,
        "notes": (obj.get("notes") or "lab overlay").strip() or "lab overlay",
    }
    if obj.get("size_gib") is not None:
        try:
            row["size_gib"] = int(float(obj["size_gib"]))
        except (TypeError, ValueError):
            print("Bad size_gib", file=sys.stderr)
            return 1
    if obj.get("tdp_watts") is not None:
        try:
            row["tdp_watts"] = int(float(obj["tdp_watts"]))
        except (TypeError, ValueError):
            pass
    if obj.get("pcie_lanes") is not None:
        try:
            row["pcie_lanes"] = int(float(obj["pcie_lanes"]))
        except (TypeError, ValueError):
            pass
    bus = obj.get("bus_interface")
    if bus:
        row["bus_interface"] = str(bus).strip()
    elif obj.get("pcie_gen") is not None:
        row["bus_interface"] = f"PCIe {obj['pcie_gen']}.0"
    path = write_catalog_overlay("gpu_pci_id.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    print("lsgpu / lsstack will see it on next run (overlay wins).")
    return 0


def _apply_nic_json(obj: dict) -> int:
    vendor = normalize_pci_hex(obj.get("vendor")) or ""
    device = normalize_pci_hex(obj.get("device")) or ""
    if not vendor or not device:
        print("JSON needs vendor and device (4 hex).", file=sys.stderr)
        return 1
    key = f"{vendor}:{device}"
    model = (obj.get("model") or "").strip() or key
    try:
        speed_v: float | int = float(obj.get("port_speed_gbps", 1))
        if speed_v == int(speed_v):
            speed_v = int(speed_v)
    except (TypeError, ValueError):
        print("Bad port_speed_gbps", file=sys.stderr)
        return 1
    try:
        ports_v = int(float(obj.get("ports", 1)))
    except (TypeError, ValueError):
        ports_v = 1
    rdma = str(obj.get("rdma") or "none").lower()
    if rdma not in ("none", "roce", "ib"):
        print("rdma must be none|roce|ib", file=sys.stderr)
        return 1
    tdp = obj.get("tdp_watts")
    if tdp is not None:
        try:
            tdp = int(float(tdp))
        except (TypeError, ValueError):
            tdp = None
    row = {
        "vendor": vendor,
        "device": device,
        "model": model,
        "port_speed_gbps": speed_v,
        "ports": ports_v,
        "rdma": rdma,
        "tdp_watts": tdp,
        "notes": (obj.get("notes") or "lab overlay").strip() or "lab overlay",
    }
    path = write_catalog_overlay("nic_pci_id.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    print("lsnetwork / lsstack will see it on next run (overlay wins).")
    return 0


def _add_from_text(kind: str, path: str) -> int:
    """Import key:value form or JSON from FILE (``-`` = stdin)."""
    try:
        raw = _read_raw_blob(path)
        obj = _parse_import_blob(kind, raw)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"Bad import: {e}", file=sys.stderr)
        return 1
    if not isinstance(obj, dict):
        print("Import root must be an object.", file=sys.stderr)
        return 1
    _warn_if_no_datasheet(obj)
    if kind == "psu":
        return _apply_psu_json(obj)
    if kind == "gpu":
        return _apply_gpu_json(obj)
    return _apply_nic_json(obj)


def _add_psu_interactive() -> int:
    from lsstack.psu_roster import (
        ATX_VALUES,
        PLUG_LABELS,
        PLUG_ORDER,
        cable_teach_blurb,
        lookup_psu,
        normalize_atx,
    )

    brand = _prompt("Brand", "")
    model = _prompt("Model", "")
    if not brand or not model:
        print("Need brand and model.", file=sys.stderr)
        return 1
    key = _prompt("Roster key (slug)", _slug(f"{brand}-{model}"))
    if lookup_psu(key):
        print(f"Note: key {key!r} already exists in shipped+overlay; overlay will replace it.")
    watts = _prompt("Watts (nameplate)", "")
    try:
        watts_i = int(float(watts))
    except ValueError:
        print("Bad watts", file=sys.stderr)
        return 1
    atx = normalize_atx(_prompt("ATX (2|3.0|3.1)", "3.0"))
    if atx not in ATX_VALUES or atx == "unknown":
        if atx == "unknown":
            print("Roster rows need a concrete ATX (2|3.0|3.1), not unknown.", file=sys.stderr)
            return 1
    print()
    print(cable_teach_blurb())
    print()
    defaults = {
        "24-pin": "1",
        "cpu": "2",
        "sata": "8",
        "molex": "0",
        "6+2": "4",
        "12V-2x6": "0",
    }
    plugs: dict = {}
    for pk in PLUG_ORDER:
        raw = _prompt(
            f"  {PLUG_LABELS.get(pk, pk)} ports (datasheet)",
            defaults.get(pk, "0"),
        )
        try:
            n = int(float(raw or 0))
        except ValueError:
            n = 0
        if n > 0:
            plugs[pk] = n
    if plugs.get("24-pin", 0) < 1 or plugs.get("cpu", 0) < 1:
        print("Need ≥1× 24-pin and ≥1× CPU/EPS on a roster row.", file=sys.stderr)
        return 1
    if plugs.get("6+2", 0) + plugs.get("12V-2x6", 0) < 1:
        print("Need at least one GPU power lead (6+2 and/or 12V-2x6).", file=sys.stderr)
        return 1
    eff = _parse_eff(_prompt(f"Efficiency ({_EFF_HELP})"))
    if not eff:
        print(f"eff must be {_EFF_HELP} (blank is not gold).", file=sys.stderr)
        return 1
    notes = _prompt("Notes", "lab overlay")
    row = {
        "key": key,
        "brand": brand,
        "model": model,
        "watts": watts_i,
        "atx": atx,
        "plugs": plugs,
        "eff": eff,
        "notes": notes,
    }
    path = write_catalog_overlay("psu_roster.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    return _after_psu_overlay_written(key, row)


def _add_gpu_interactive(from_live: bool) -> int:
    gpus = _list_live_gpus() if from_live else []
    misses = [
        g
        for g in gpus
        if not lookup_gpu_specs(load_json("gpu_pci_id.json"), g.get("vendor"), g.get("device"))
    ]
    chosen = None
    if from_live:
        pool = misses or gpus
        if not pool:
            print("No GPUs detected.", file=sys.stderr)
            return 1
        print("Select GPU:")
        for i, g in enumerate(pool):
            key = gpu_spec_key(g.get("vendor"), g.get("device"))
            name = g.get("lspci_name") or (g.get("specs") or {}).get("model") or key
            print(f"  [{i}] {key}  {name}")
        idx = _prompt("Index", "0")
        try:
            chosen = pool[int(idx)]
        except (ValueError, IndexError):
            print("Invalid index", file=sys.stderr)
            return 1

    if chosen:
        vendor = normalize_pci_hex(chosen.get("vendor")) or ""
        device = normalize_pci_hex(chosen.get("device")) or ""
        default_model = (
            (chosen.get("specs") or {}).get("model")
            or chosen.get("lspci_name")
            or f"{vendor}:{device}"
        )
        default_size = ""
        ns = chosen.get("nvidia_smi") or {}
        if ns.get("memory_total"):
            try:
                default_size = str(int(round(float(ns["memory_total"]) / 1024)))
            except (TypeError, ValueError):
                pass
    else:
        vendor = normalize_pci_hex(_prompt("Vendor (4 hex, e.g. 10de)")) or ""
        device = normalize_pci_hex(_prompt("Device (4 hex)")) or ""
        default_model = ""
        default_size = ""

    if not vendor or not device:
        print("Need vendor and device.", file=sys.stderr)
        return 1
    key = f"{vendor}:{device}"
    model = _prompt("Model", default_model)
    size = _prompt("Size GiB (VRAM)", default_size)
    tdp = _prompt("TDP watts (optional)", "")
    lanes = _prompt("PCIe lanes (optional)", "")
    gen = _prompt("PCIe gen digit e.g. 4 (optional)", "")
    notes = _prompt("Notes (optional)", "lab overlay")

    row: dict = {
        "vendor": vendor,
        "device": device,
        "model": model,
        "notes": notes,
    }
    if size:
        try:
            row["size_gib"] = int(float(size))
        except ValueError:
            print("Bad size_gib", file=sys.stderr)
            return 1
    if tdp:
        try:
            row["tdp_watts"] = int(float(tdp))
        except ValueError:
            pass
    if lanes:
        try:
            row["pcie_lanes"] = int(float(lanes))
        except ValueError:
            pass
    if gen:
        # store on bus_interface for consistency with GPU rows
        row["bus_interface"] = f"PCIe {gen}.0"

    path = write_catalog_overlay("gpu_pci_id.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    print("lsgpu / lsstack will see it on next run (overlay wins).")
    return 0


def _add_nic_interactive(from_live: bool) -> int:
    nics = _list_live_nics() if from_live else []
    misses = [
        n
        for n in nics
        if not lookup_nic_specs(load_json("nic_pci_id.json"), n.get("vendor"), n.get("device"))
    ]
    chosen = None
    if from_live:
        pool = misses or nics
        if not pool:
            print("No PCI NICs detected.", file=sys.stderr)
            return 1
        print("Select NIC:")
        for i, n in enumerate(pool):
            key = gpu_spec_key(n.get("vendor"), n.get("device"))
            print(f"  [{i}] {key}  {n.get('model') or ''}")
        idx = _prompt("Index", "0")
        try:
            chosen = pool[int(idx)]
        except (ValueError, IndexError):
            print("Invalid index", file=sys.stderr)
            return 1

    if chosen:
        vendor = normalize_pci_hex(chosen.get("vendor")) or ""
        device = normalize_pci_hex(chosen.get("device")) or ""
        default_model = chosen.get("model") or f"{vendor}:{device}"
        default_speed = ""
        if chosen.get("speed_mbps"):
            try:
                mbps = int(chosen["speed_mbps"])
                default_speed = str(mbps / 1000 if mbps >= 1000 else mbps / 1000)
                if mbps >= 1000:
                    default_speed = str(int(mbps // 1000)) if mbps % 1000 == 0 else str(mbps / 1000)
            except (TypeError, ValueError):
                pass
    else:
        vendor = normalize_pci_hex(_prompt("Vendor (4 hex, e.g. 15b3)")) or ""
        device = normalize_pci_hex(_prompt("Device (4 hex)")) or ""
        default_model = ""
        default_speed = ""

    if not vendor or not device:
        print("Need vendor and device.", file=sys.stderr)
        return 1
    key = f"{vendor}:{device}"
    model = _prompt("Model", default_model)
    speed = _prompt("Port speed Gbps (1, 2.5, 10, 25, 100…)", default_speed or "1")
    ports = _prompt("Ports", "1")
    rdma = _prompt("RDMA (none|roce|ib)", "none").lower()
    if rdma not in ("none", "roce", "ib"):
        print("rdma must be none|roce|ib", file=sys.stderr)
        return 1
    notes = _prompt("Notes (optional)", "lab overlay")

    try:
        speed_v: float | int = float(speed)
        if speed_v == int(speed_v):
            speed_v = int(speed_v)
    except ValueError:
        print("Bad port_speed_gbps", file=sys.stderr)
        return 1
    try:
        ports_v = int(float(ports))
    except ValueError:
        ports_v = 1

    row = {
        "vendor": vendor,
        "device": device,
        "model": model,
        "port_speed_gbps": speed_v,
        "ports": ports_v,
        "rdma": rdma,
        "tdp_watts": None,
        "notes": notes,
    }
    path = write_catalog_overlay("nic_pci_id.json", {key: row})
    print(f"Wrote overlay entry {key} → {path}")
    print("lsnetwork / lsstack will see it on next run (overlay wins).")
    return 0


def _help_text() -> str:
    return """lscatalog — local overlay for GPU, NIC, and PSU SKUs (lab / workstation)

What
  Adds missing GPU/NIC/PSU rows under ~/.lsstack/catalog/. Overlay wins over
  shipped config. Does not replace lslab enroll.

Why
  Pick lists and reports only see SKUs in the catalog. A PSU typed at enroll
  stays on that host until you add it here.

How
  misses — live GPU/NIC vs catalog; lab PSU vs overlay pick list.
  prompt KIND — fill-in form (datasheet or paste into an LLM), then import.
  add KIND — interactive, --from-text FILE, or GPU/NIC --from-live.
  list [psu|gpu|nic] — [lab] overlay wins. GPU defaults to lab rows
  (full shipped dump: list gpu --all).

Usage: lscatalog <command> [options]
  path                    Overlay directory
  list [psu|gpu|nic] [--all]
  misses                  Live GPU/NIC + lab PSU vs catalog
  prompt psu|gpu|nic [--name MODEL]
  add psu|gpu|nic         Interactive (no form)
  add psu|gpu|nic --from-text FILE
  add gpu|nic --from-live
  --version, -V           Print lsStack version
  -h, --help              This help

Env: LSSTACK_CATALOG=/path   Conf: catalog_dir= in ~/.lsstack.conf
Default dir: ~/.lsstack/catalog/
"""


def _usage(*, error: bool = False) -> None:
    text = _help_text()
    print(text, file=sys.stderr if error else sys.stdout)


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        _usage()
        raise SystemExit(0)
    if not args or args[0] not in ("path", "list", "misses", "prompt", "add"):
        _usage(error=True)
        raise SystemExit(2)

    ap = argparse.ArgumentParser(prog="lscatalog", add_help=False)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_path = sub.add_parser("path", add_help=False)
    p_path.set_defaults(func=cmd_path)

    p_list = sub.add_parser("list", add_help=False)
    p_list.add_argument(
        "kind",
        nargs="?",
        choices=("gpu", "nic", "psu"),
        default=None,
    )
    p_list.add_argument(
        "--all",
        action="store_true",
        help="Include full shipped GPU list (large)",
    )
    p_list.set_defaults(func=cmd_list)

    p_miss = sub.add_parser("misses", add_help=False)
    p_miss.set_defaults(func=cmd_misses)

    p_prompt = sub.add_parser("prompt", add_help=False)
    p_prompt.add_argument("kind", choices=("gpu", "nic", "psu"))
    p_prompt.add_argument("--name", "-n", default="")
    p_prompt.set_defaults(func=cmd_prompt)

    p_add = sub.add_parser("add", add_help=False)
    p_add.add_argument("kind", choices=("gpu", "nic", "psu"))
    p_add.add_argument("--from-live", action="store_true")
    p_add.add_argument("--from-text", metavar="FILE")
    p_add.add_argument("--from-json", metavar="FILE", help=argparse.SUPPRESS)
    p_add.set_defaults(func=None)

    ns = ap.parse_args(args)
    if ns.cmd == "add":
        blob = ns.from_text or ns.from_json
        if ns.from_live and ns.kind == "psu":
            print(
                "Error: --from-live is GPU/NIC only (kernel cannot see PSUs)",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if blob:
            raise SystemExit(_add_from_text(ns.kind, blob))
        if ns.kind == "gpu":
            raise SystemExit(_add_gpu_interactive(ns.from_live))
        if ns.kind == "nic":
            raise SystemExit(_add_nic_interactive(ns.from_live))
        raise SystemExit(_add_psu_interactive())
    raise SystemExit(ns.func(ns))


if __name__ == "__main__":
    main()
