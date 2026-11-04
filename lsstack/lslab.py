"""lslab: lab inventory CLI — builds + hosts (PSU object / platform). No hand-editing required."""

from __future__ import annotations

import json
import re
import sys

from lsstack import lab_inventory as lab
from lsstack.psu_roster import (
    ATX_VALUES,
    EFF_VALUES,
    PLUG_LABELS,
    PLUG_ORDER,
    apply_psu_to_record,
    cable_teach_blurb,
    format_plugs_short,
    format_psu_summary,
    list_roster_sorted,
    normalize_atx,
    normalize_eff,
    normalize_psu_object,
    parse_plugs,
    roster_row_to_psu,
)


def _usage() -> None:
    print(
        """lslab — lab inventory for shared builds (PSU object, platform watts) across hosts.

Manage a JSON catalog of builds and enrolled systems so lspower / lsstack can
resolve PSU headroom without hand-editing ~/.lsstack.conf on every machine.
Enroll once per host = lab book entry (id + PSU ± shared build), not a full PC
scan. Live hardware stays on lsstack / ls*. Reuse a build when boards match.
`lslab enroll` prompts for PSU (roster prefill). Custom enroll entries stay on
that host only — run `lscatalog add psu` (or add gpu/nic) so the SKU appears in
future pick lists.

Usage:
  lslab status
  lslab path
  lslab list [--json]
  lslab show <system_id|hostname|build:<name>>
  lslab enroll [--system-id ID] [--psu N] [--psu-atx ATX] [--psu-model NAME]
               [--psu-plugs SPEC] [--psu-eff TIER] [--build NAME] [--mkbuild NAME] [--yes]
  lslab mkbuild <name> [--psu N] [--psu-atx ATX] … [--platform N] [--from-this-host]
  lslab set-psu [<N>] [--host ID] [--build NAME]   # interactive if N omitted on TTY
  lslab set-build <NAME> [--host ID]
  lslab same-as <OTHER> [--host ID]
  lslab adopt [--build NAME] [--yes]

PSU plugs SPEC = manufacturer port counts (not cables in the box):
  24-pin:1,cpu:2,sata:8,molex:0,6+2:4,12V-2x6:1
  Look up the exact model on the vendor site. Keys: 24-pin, cpu, sata, molex, 6+2, 12V-2x6
ATX: 2 | 3.0 | 3.1 | unknown (required). 2.x datasheet values (e.g. 2.4) store as 2.
Eff: bronze|silver|gold|platinum|titanium|unknown (stored for education; not derated)

Env: LSSTACK_LAB=/path/to/lab_inventory.json
Conf: lab_inventory=/path in ~/.lsstack.conf; psu_watts= overrides nameplate only
Default file: ~/.lsstack/lab_inventory.json
""",
        file=sys.stderr,
    )


def _prompt(msg: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{msg}{suffix}: ").strip()
    except EOFError:
        val = ""
    if not val and default is not None:
        return default
    return val


def _prompt_yes(msg: str, *, yes_flag: bool, default: bool = True) -> bool:
    if yes_flag:
        return True
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"{msg} [{d}]: ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes")


def _parse_int(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    return int(s)


def _parse_psu_flags(args: list[str]) -> tuple[dict, list[str]]:
    """Pull --psu* flags; return (partial psu dict, remaining args)."""
    out: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--psu" and i + 1 < len(args):
            out["watts"] = int(args[i + 1])
            i += 2
            continue
        if a == "--psu-atx" and i + 1 < len(args):
            out["atx"] = args[i + 1]
            i += 2
            continue
        if a == "--psu-model" and i + 1 < len(args):
            out["model"] = args[i + 1]
            i += 2
            continue
        if a == "--psu-plugs" and i + 1 < len(args):
            out["plugs"] = args[i + 1]
            i += 2
            continue
        if a == "--psu-eff" and i + 1 < len(args):
            out["eff"] = args[i + 1]
            i += 2
            continue
        if a == "--psu-key" and i + 1 < len(args):
            out["roster_key"] = args[i + 1]
            i += 2
            continue
        rest.append(a)
        i += 1
    return out, rest


def _prompt_psu_object(
    *,
    seed: dict | None = None,
    yes: bool = False,
    required: bool = True,
) -> dict | None:
    """Interactive (or flag-seeded) full PSU object. ATX required incl. unknown."""
    seed = dict(seed or {})
    if yes:
        if seed.get("watts") is None:
            print("Error: --psu N required with --yes", file=sys.stderr)
            return None
        atx = normalize_atx(seed.get("atx") or "unknown")
        obj = normalize_psu_object({
            "watts": seed["watts"],
            "model": seed.get("model"),
            "roster_key": seed.get("roster_key"),
            "atx": atx,
            "plugs": parse_plugs(seed.get("plugs")),
            "eff": seed.get("eff"),
        })
        return obj

    if not sys.stdin.isatty() and seed.get("watts") is None:
        print("Error: --psu required in non-interactive mode", file=sys.stderr)
        return None

    # Roster pick
    if seed.get("roster_key") and not seed.get("watts"):
        from lsstack.psu_roster import lookup_psu
        row = lookup_psu(seed["roster_key"])
        if row:
            seed = {**roster_row_to_psu(row), **{k: v for k, v in seed.items() if v is not None}}

    if sys.stdin.isatty() and seed.get("watts") is None:
        rows = list_roster_sorted()
        print(
            "\nPSU roster (pick index, or c=custom). "
            "Port summary = mb/cpu · drives · GPU (datasheet typical — verify your model).\n"
            "Missing from this list? Custom (c) saves on this host only; "
            "lscatalog add psu puts it in the pick list for later enrolls:"
        )
        for i, r in enumerate(rows):
            plugs = format_plugs_short(r.get("plugs"))
            print(
                f"  [{i:2}] {r.get('watts')} W  ATX {r.get('atx')}  "
                f"{r.get('brand')} {r.get('model')}  {plugs}"
            )
        pick = _prompt("Roster index (or c)", "c")
        if pick.lower() not in ("c", "custom", ""):
            try:
                row = rows[int(pick)]
                seed = {**roster_row_to_psu(row), **seed}
                print(
                    f"  Prefill from {row.get('key')} — confirm against the manufacturer "
                    "page for your exact model (ports ≠ cables in the box)."
                )
            except (ValueError, IndexError):
                print("  Invalid index; entering custom.", file=sys.stderr)

    watts = seed.get("watts")
    if watts is None:
        watts = _parse_int(_prompt("PSU watts (nameplate)", None))
    elif sys.stdin.isatty():
        watts = _parse_int(_prompt("PSU watts (nameplate)", str(watts)))
    if watts is None:
        if required:
            print("Error: psu watts required", file=sys.stderr)
        return None

    model = seed.get("model")
    if sys.stdin.isatty():
        model = _prompt("PSU model (optional)", model or "") or None
    roster_key = seed.get("roster_key")

    atx = seed.get("atx")
    if sys.stdin.isatty():
        raw_atx = _prompt(
            "ATX gen (2|3.0|3.1|unknown; 2.4→2)",
            (atx if atx else "unknown"),
        )
        if raw_atx != normalize_atx(raw_atx) and normalize_atx(raw_atx) == "2" and re.search(
            r"2\.\d", raw_atx
        ):
            print(f"  (stored as ATX 2 — 2.x family; datasheet {raw_atx.strip()!r} is fine)")
        atx = raw_atx
    atx = normalize_atx(atx or "unknown")
    if atx not in ATX_VALUES:
        print(f"Error: atx must be one of {ATX_VALUES}", file=sys.stderr)
        return None

    pd = parse_plugs(seed.get("plugs"))
    if sys.stdin.isatty():
        print()
        print(cable_teach_blurb())
        print()
        plugs: dict = {}
        for key in PLUG_ORDER:
            default = str(pd.get(key, 0))
            label = PLUG_LABELS.get(key, key)
            n = _parse_int(_prompt(f"  {label} ports (datasheet)", default))
            if n is not None and n > 0:
                plugs[key] = n
        if plugs.get("24-pin", 0) < 1 or plugs.get("cpu", 0) < 1:
            print(
                "Warning: a normal ATX/SFX unit lists ≥1× 24-pin and ≥1× CPU/EPS on the "
                "datasheet. Re-check the manufacturer page for this model.",
                file=sys.stderr,
            )
    else:
        plugs_raw = seed.get("plugs")
        if isinstance(plugs_raw, str):
            plugs = parse_plugs(plugs_raw)
        else:
            plugs = pd

    eff = seed.get("eff")
    if sys.stdin.isatty():
        eff = _prompt("Efficiency (gold|platinum|…|unknown, optional)", normalize_eff(eff) or "")
    eff = normalize_eff(eff) if eff else None
    if eff and eff not in EFF_VALUES:
        print(f"Error: eff must be one of {EFF_VALUES}", file=sys.stderr)
        return None

    return normalize_psu_object({
        "watts": watts,
        "model": model,
        "roster_key": roster_key,
        "atx": atx,
        "plugs": plugs,
        "eff": eff,
    })


def cmd_path(_args: list[str]) -> int:
    p = lab.inventory_path(for_write=True)
    print(p)
    return 0


def cmd_status(_args: list[str]) -> int:
    r = lab.resolve_lab_power()
    ident = lab.detect_identity()
    print("\nLab inventory")
    print(f"  {'Path':>14}  {r['inventory_path']}")
    print(f"  {'Exists':>14}  {'yes' if r['inventory_exists'] else 'no'}")
    print(f"  {'Hostname':>14}  {ident.get('hostname') or '—'}")
    print(f"  {'System ID':>14}  {ident.get('system_id') or '— (pass --system-id on enroll)'}")
    print(f"  {'Board':>14}  {ident.get('board_vendor') or ''} {ident.get('board_name') or ''}".rstrip())
    print(f"  {'Enrolled':>14}  {'yes' if r['enrolled'] else 'no'}")
    print(f"  {'Build':>14}  {r.get('build') or '—'}")
    psu = r.get("psu")
    if psu is not None:
        print(f"  {'PSU':>14}  {format_psu_summary(psu)} (from {r.get('psu_source')})")
    else:
        print(f"  {'PSU':>14}  not set — lslab enroll / set-psu / mkbuild")
    plat = r.get("platform_watts")
    if plat is not None:
        print(f"  {'Platform':>14}  {plat} W (from {r.get('platform_source')})")
    matches = r.get("board_build_matches") or []
    if matches and not r.get("build"):
        print(f"  {'Suggest':>14}  board matches build(s): {', '.join(matches)} — try: lslab adopt")
    print()
    return 0


def cmd_list(args: list[str]) -> int:
    as_json = "--json" in args or "-j" in args
    data = lab.load_inventory()
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    builds = data.get("builds") or {}
    hosts = data.get("hosts") or {}
    print("\nBuilds")
    if not builds:
        print("  (none)")
    else:
        for name, b in sorted(builds.items()):
            from lsstack.psu_roster import psu_from_record
            psu = psu_from_record(b)
            mb = b.get("motherboard") or b.get("board_name") or "—"
            print(f"  {name:20}  {format_psu_summary(psu):40}  board {mb}")
    print("\nHosts")
    if not hosts:
        print("  (none)")
    else:
        for sid, h in sorted(hosts.items()):
            print(
                f"  {sid:20}  host {h.get('hostname') or '—'}  "
                f"build {h.get('build') or h.get('same_as') or '—'}  "
                f"board {h.get('board_name') or '—'}"
            )
    print()
    return 0


def cmd_show(args: list[str]) -> int:
    if not args:
        _usage()
        return 2
    target = args[0]
    data = lab.load_inventory()
    if target.startswith("build:"):
        name = target.split(":", 1)[1]
        b = (data.get("builds") or {}).get(name)
        if not b:
            print(f"Build not found: {name}", file=sys.stderr)
            return 1
        print(json.dumps({"name": name, **b}, indent=2))
        return 0
    found = lab.find_host(data, system_id=target, hostname=target)
    if not found:
        b = (data.get("builds") or {}).get(target)
        if b:
            print(json.dumps({"name": target, **b}, indent=2))
            return 0
        print(f"Not found: {target}", file=sys.stderr)
        return 1
    k, h = found
    print(json.dumps({"key": k, **h}, indent=2))
    return 0


def cmd_mkbuild(args: list[str]) -> int:
    if not args or args[0].startswith("-"):
        print(
            "Usage: lslab mkbuild <name> [--psu N] [--psu-atx ATX] [--platform N] [--from-this-host]",
            file=sys.stderr,
        )
        return 2
    name = args[0]
    psu_flags, rest = _parse_psu_flags(args[1:])
    platform = None
    from_host = "--from-this-host" in rest
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--platform" and i + 1 < len(rest):
            platform = int(rest[i + 1])
            i += 2
            continue
        if a == "--from-this-host":
            i += 1
            continue
        i += 1

    ident = lab.detect_identity()
    build: dict = {}
    if from_host:
        if ident.get("board_name"):
            build["motherboard"] = ident["board_name"]
        if ident.get("board_vendor"):
            build["board_vendor"] = ident["board_vendor"]

    psu = _prompt_psu_object(seed=psu_flags, yes=False, required=True)
    if psu is None:
        return 2
    build = apply_psu_to_record(build, psu)
    if platform is not None:
        build["platform_watts"] = platform

    data = lab.load_inventory()
    lab.upsert_build(data, name, build)
    path = lab.save_inventory(data)
    print(f"Build {name!r} saved → {path}")
    print(f"  PSU: {format_psu_summary(psu)}")
    return 0


def cmd_enroll(args: list[str]) -> int:
    system_id = None
    build = None
    mkbuild = None
    yes = "--yes" in args or "-y" in args
    psu_flags, rest = _parse_psu_flags(args)
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--system-id" and i + 1 < len(rest):
            system_id = rest[i + 1]
            i += 2
            continue
        if a == "--build" and i + 1 < len(rest):
            build = rest[i + 1]
            i += 2
            continue
        if a == "--mkbuild" and i + 1 < len(rest):
            mkbuild = rest[i + 1]
            i += 2
            continue
        if a in ("--yes", "-y"):
            i += 1
            continue
        i += 1

    print(
        "\n"
        "enroll = register this host in the lab inventory (identity + PSU, and\n"
        "optionally a shared build). It does not scan the whole PC — CPU/GPU/RAM/\n"
        "disks come from lsstack / ls* on each run. Declared PSU lives in\n"
        f"{lab.inventory_path(for_write=False)}.\n"
        "\n"
        "Pick-list tip: choosing a roster index reuses a known SKU. A custom PSU\n"
        "(or GPU/NIC) typed only at enroll is saved on this host — it will not show\n"
        "up for the next machine. Add to future pick lists:\n"
        "  lscatalog prompt psu --name 'Corsair HX750'   # cut-paste into Gemini/LLM\n"
        "  lscatalog add psu --from-text -               # paste key: value reply, Ctrl-D\n"
        "  (same pattern: prompt/add for gpu and nic; or interactive: lscatalog add psu)\n"
        "Next: lsstack or lspower to compare live hardware to that declaration.\n"
    )

    ident = lab.detect_identity()
    system_id = system_id or ident.get("system_id")
    if not system_id:
        if yes:
            print("Error: no system_id detected; pass --system-id", file=sys.stderr)
            return 2
        system_id = _prompt("System ID (required)", None)
    if not system_id:
        print("Error: system_id required", file=sys.stderr)
        return 2

    data = lab.load_inventory()
    matches = lab.builds_matching_board(data, ident.get("board_name"))
    psu_obj = None

    if mkbuild:
        psu_obj = _prompt_psu_object(seed=psu_flags, yes=yes, required=True)
        if psu_obj is None:
            return 2
        brec = apply_psu_to_record({
            "motherboard": ident.get("board_name"),
            "board_vendor": ident.get("board_vendor"),
        }, psu_obj)
        lab.upsert_build(data, mkbuild, brec)
        build = mkbuild

    if build is None and matches:
        if len(matches) == 1:
            cand = matches[0][0]
            if _prompt_yes(f"Board matches build {cand!r}. Use it?", yes_flag=yes, default=True):
                build = cand
        else:
            names = ", ".join(n for n, _ in matches)
            if not yes:
                pick = _prompt(f"Board matches builds [{names}]. Build name (or empty)", "")
                build = pick or None

    if build is None and psu_obj is None and not psu_flags.get("watts"):
        if yes:
            print("Error: pass --build, --mkbuild, or --psu with --yes", file=sys.stderr)
            return 2
        if _prompt_yes("Create a new build from this host?", yes_flag=False, default=True):
            bname = _prompt("Build name", "default")
            psu_obj = _prompt_psu_object(seed=psu_flags, yes=False, required=True)
            if not bname or psu_obj is None:
                print("Error: build name and psu required", file=sys.stderr)
                return 2
            brec = apply_psu_to_record({
                "motherboard": ident.get("board_name"),
                "board_vendor": ident.get("board_vendor"),
            }, psu_obj)
            lab.upsert_build(data, bname, brec)
            build = bname
        else:
            psu_obj = _prompt_psu_object(seed=psu_flags, yes=False, required=True)
            if psu_obj is None:
                return 2
    elif psu_flags.get("watts") is not None and psu_obj is None and build is None:
        psu_obj = _prompt_psu_object(seed=psu_flags, yes=yes, required=True)
        if psu_obj is None:
            return 2

    host = {
        "system_id": system_id,
        "hostname": ident.get("hostname"),
        "board_name": ident.get("board_name"),
        "board_vendor": ident.get("board_vendor"),
        "product_name": ident.get("product_name"),
    }
    if build:
        host["build"] = build
    # Host owns PSU when no build, or explicit --psu with --build
    if psu_obj is not None:
        if not build:
            host = apply_psu_to_record(host, psu_obj)
        elif not mkbuild and "--psu" in args and "--build" in args:
            host = apply_psu_to_record(host, psu_obj)

    lab.upsert_host(data, host)
    path = lab.save_inventory(data)
    print(f"Enrolled {system_id!r} → {path}")
    if build:
        print(f"  build: {build}")
    r = lab.resolve_lab_power(system_id=system_id)
    if r.get("psu") is not None:
        print(f"  PSU: {format_psu_summary(r['psu'])} (from {r.get('psu_source')})")
        psu = r.get("psu") or {}
        if not psu.get("roster_key"):
            model = psu.get("model") or "Your PSU"
            print(
                "  Note: this PSU is not on the roster pick list. "
                f"Try: lscatalog prompt psu --name {model!r} "
                "then lscatalog add psu --from-text -"
            )
    return 0



def cmd_set_psu(args: list[str]) -> int:
    psu_flags, rest = _parse_psu_flags(args)
    # positional watts: set-psu 850
    if rest and not rest[0].startswith("-"):
        try:
            psu_flags["watts"] = int(rest[0])
            rest = rest[1:]
        except ValueError:
            pass
    host_id = None
    build = None
    i = 0
    while i < len(rest):
        if rest[i] == "--host" and i + 1 < len(rest):
            host_id = rest[i + 1]
            i += 2
            continue
        if rest[i] == "--build" and i + 1 < len(rest):
            build = rest[i + 1]
            i += 2
            continue
        i += 1

    data = lab.load_inventory()
    from lsstack.psu_roster import psu_from_record

    def _merge_psu(existing_rec: dict | None) -> dict | None:
        prev = psu_from_record(existing_rec) or {}
        # Watts-only non-interactive: preserve ATX/plugs/model from existing.
        if (
            not sys.stdin.isatty()
            and psu_flags.get("watts") is not None
            and set(psu_flags.keys()) <= {"watts"}
            and prev
        ):
            merged = dict(prev)
            merged["watts"] = psu_flags["watts"]
            return normalize_psu_object(merged)
        seed = {**prev, **psu_flags}
        if not seed.get("atx") and not sys.stdin.isatty():
            seed["atx"] = "unknown"
        return _prompt_psu_object(
            seed=seed,
            yes=not sys.stdin.isatty(),
            required=True,
        )

    if build:
        existing = (data.get("builds") or {}).get(build)
        if not existing:
            print(f"Build not found: {build}", file=sys.stderr)
            return 1
        psu = _merge_psu(existing)
        if psu is None:
            return 2
        b = apply_psu_to_record(existing, psu)
        lab.upsert_build(data, build, b)
        path = lab.save_inventory(data)
        print(f"Build {build!r} PSU {format_psu_summary(psu)} → {path}")
        return 0

    ident = lab.detect_identity()
    host_id = host_id or ident.get("system_id") or ident.get("hostname")
    found = lab.find_host(data, system_id=host_id, hostname=host_id)
    if not found:
        print(f"Host not enrolled: {host_id}. Run lslab enroll first.", file=sys.stderr)
        return 1
    k, h = found
    psu = _merge_psu(h)
    if psu is None:
        return 2
    h = apply_psu_to_record(h, psu)
    lab.upsert_host(data, h)
    path = lab.save_inventory(data)
    print(f"Host {k!r} PSU {format_psu_summary(psu)} → {path}")
    return 0


def cmd_set_build(args: list[str]) -> int:
    if not args:
        print("Usage: lslab set-build <NAME> [--host ID]", file=sys.stderr)
        return 2
    build = args[0]
    host_id = None
    if "--host" in args:
        i = args.index("--host")
        if i + 1 < len(args):
            host_id = args[i + 1]
    data = lab.load_inventory()
    if build not in (data.get("builds") or {}):
        print(f"Build not found: {build}", file=sys.stderr)
        return 1
    ident = lab.detect_identity()
    host_id = host_id or ident.get("system_id") or ident.get("hostname")
    found = lab.find_host(data, system_id=host_id, hostname=host_id)
    if not found:
        print(f"Host not enrolled: {host_id}", file=sys.stderr)
        return 1
    k, h = found
    h["build"] = build
    h.pop("same_as", None)
    lab.upsert_host(data, h)
    path = lab.save_inventory(data)
    print(f"Host {k!r} → build {build!r} → {path}")
    return 0


def cmd_same_as(args: list[str]) -> int:
    if not args:
        print("Usage: lslab same-as <OTHER> [--host ID]", file=sys.stderr)
        return 2
    other = args[0]
    host_id = None
    if "--host" in args:
        i = args.index("--host")
        if i + 1 < len(args):
            host_id = args[i + 1]
    data = lab.load_inventory()
    if not lab.find_host(data, system_id=other, hostname=other):
        print(f"Other host not found: {other}", file=sys.stderr)
        return 1
    ident = lab.detect_identity()
    host_id = host_id or ident.get("system_id") or ident.get("hostname")
    found = lab.find_host(data, system_id=host_id, hostname=host_id)
    if not found:
        print(f"Host not enrolled: {host_id}. Run lslab enroll first.", file=sys.stderr)
        return 1
    k, h = found
    h["same_as"] = other
    h.pop("build", None)
    lab.upsert_host(data, h)
    path = lab.save_inventory(data)
    print(f"Host {k!r} same_as {other!r} → {path}")
    return 0


def cmd_adopt(args: list[str]) -> int:
    yes = "--yes" in args or "-y" in args
    build = None
    if "--build" in args:
        i = args.index("--build")
        if i + 1 < len(args):
            build = args[i + 1]
    ident = lab.detect_identity()
    data = lab.load_inventory()
    sid = ident.get("system_id")
    if not sid:
        print("Error: no system_id; enroll with --system-id first", file=sys.stderr)
        return 2
    matches = lab.builds_matching_board(data, ident.get("board_name"))
    if build is None:
        if not matches:
            print("No builds match this motherboard. Use lslab mkbuild or enroll --mkbuild.", file=sys.stderr)
            return 1
        if len(matches) == 1:
            build = matches[0][0]
        else:
            names = ", ".join(n for n, _ in matches)
            if yes:
                print(f"Error: multiple matches ({names}); pass --build", file=sys.stderr)
                return 2
            build = _prompt(f"Choose build ({names})", matches[0][0])
    if build not in (data.get("builds") or {}):
        print(f"Build not found: {build}", file=sys.stderr)
        return 1
    if not _prompt_yes(f"Adopt build {build!r} for this host?", yes_flag=yes, default=True):
        print("Aborted.")
        return 1
    found = lab.find_host(data, system_id=sid, hostname=ident.get("hostname"))
    host = found[1] if found else {
        "system_id": sid,
        "hostname": ident.get("hostname"),
        "board_name": ident.get("board_name"),
        "board_vendor": ident.get("board_vendor"),
    }
    host["build"] = build
    host.pop("same_as", None)
    lab.upsert_host(data, host)
    path = lab.save_inventory(data)
    print(f"Adopted {build!r} → {path}")
    return 0


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        _usage()
        sys.exit(0 if args else 2)
    cmd = args[0]
    rest = args[1:]
    handlers = {
        "status": cmd_status,
        "path": cmd_path,
        "list": cmd_list,
        "show": cmd_show,
        "enroll": cmd_enroll,
        "mkbuild": cmd_mkbuild,
        "set-psu": cmd_set_psu,
        "set-build": cmd_set_build,
        "same-as": cmd_same_as,
        "adopt": cmd_adopt,
    }
    fn = handlers.get(cmd)
    if not fn:
        _usage()
        sys.exit(2)
    code = fn(rest)
    sys.exit(code)


if __name__ == "__main__":
    main()
