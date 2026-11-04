"""Shared lab inventory store (builds + hosts). Read by budget; written by lslab only."""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from datetime import datetime, timezone
from typing import Any

from lsstack.common import conf_path, effective_home, read_dmi_id, read_file

_ETC_LAB = "/etc/lsstack/lab_inventory.json"


def _user_lab() -> str:
    return os.path.join(effective_home(), ".lsstack", "lab_inventory.json")

_PLACEHOLDER = re.compile(
    r"^(default string|to be filled|none|null|n/a|not specified|0+|system serial number|chassis serial number)?$",
    re.I,
)


def _conf_value(key: str) -> str | None:
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


def inventory_path(*, for_write: bool = False) -> str:
    """Resolve lab inventory JSON path.

    Order: LSSTACK_LAB, lab_inventory= in ~/.lsstack.conf (SUDO_USER-aware),
    existing ~/.lsstack/… or /etc/lsstack/…, else create user path when writing.
    """
    env = os.environ.get("LSSTACK_LAB")
    if env:
        return os.path.expanduser(env)
    conf = _conf_value("lab_inventory")
    if conf:
        return os.path.expanduser(conf)
    user_lab = _user_lab()
    if os.path.isfile(_ETC_LAB) and (not for_write or os.access(_ETC_LAB, os.W_OK)):
        if os.path.isfile(_ETC_LAB):
            return _ETC_LAB
    if os.path.isfile(user_lab):
        return user_lab
    if for_write:
        return user_lab
    # Prefer etc if present even read-only
    if os.path.isfile(_ETC_LAB):
        return _ETC_LAB
    return user_lab


def empty_inventory() -> dict[str, Any]:
    return {"version": 1, "builds": {}, "hosts": {}}


def load_inventory(path: str | None = None) -> dict[str, Any]:
    path = path or inventory_path(for_write=False)
    if not os.path.isfile(path):
        return empty_inventory()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty_inventory()
    if not isinstance(data, dict):
        return empty_inventory()
    data.setdefault("version", 1)
    data.setdefault("builds", {})
    data.setdefault("hosts", {})
    if not isinstance(data["builds"], dict):
        data["builds"] = {}
    if not isinstance(data["hosts"], dict):
        data["hosts"] = {}
    return data


def save_inventory(data: dict[str, Any], path: str | None = None) -> str:
    path = path or inventory_path(for_write=True)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o755, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".lab_inventory.", dir=parent or None, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.strip()
    if not t or _PLACEHOLDER.match(t):
        return None
    return t


def detect_identity() -> dict[str, str | None]:
    """Board / system identity from DMI sysfs (no root required for most boards)."""
    dmi = read_dmi_id(
        "board_vendor", "board_name", "board_serial", "board_asset_tag",
        "sys_vendor", "product_name", "product_serial", "product_uuid",
        "chassis_serial", "chassis_asset_tag",
    )
    hostname = socket.gethostname()
    # system_id preference: asset tags, then serials, then uuid
    system_id = (
        _clean(dmi.get("board_asset_tag"))
        or _clean(dmi.get("chassis_asset_tag"))
        or _clean(dmi.get("product_serial"))
        or _clean(dmi.get("board_serial"))
        or _clean(dmi.get("chassis_serial"))
        or _clean(dmi.get("product_uuid"))
    )
    board_name = _clean(dmi.get("board_name"))
    board_vendor = _clean(dmi.get("board_vendor")) or _clean(dmi.get("sys_vendor"))
    return {
        "hostname": hostname,
        "system_id": system_id,
        "board_name": board_name,
        "board_vendor": board_vendor,
        "product_name": _clean(dmi.get("product_name")),
        "product_serial": _clean(dmi.get("product_serial")),
        "board_serial": _clean(dmi.get("board_serial")),
        "product_uuid": _clean(dmi.get("product_uuid")),
    }


def _host_key(host: dict) -> str:
    return (host.get("system_id") or host.get("hostname") or "").strip()


def find_host(
    data: dict[str, Any],
    *,
    system_id: str | None = None,
    hostname: str | None = None,
) -> tuple[str, dict] | None:
    hosts = data.get("hosts") or {}
    if system_id and system_id in hosts:
        return system_id, hosts[system_id]
    if system_id:
        for k, h in hosts.items():
            if (h.get("system_id") or k) == system_id:
                return k, h
    if hostname:
        for k, h in hosts.items():
            if h.get("hostname") == hostname:
                return k, h
    return None


def builds_matching_board(data: dict[str, Any], board_name: str | None) -> list[tuple[str, dict]]:
    if not board_name:
        return []
    want = board_name.strip().lower()
    out = []
    for name, b in (data.get("builds") or {}).items():
        mb = (b.get("motherboard") or b.get("board_name") or "").strip().lower()
        if mb and mb == want:
            out.append((name, b))
    return out


def resolve_build_name(data: dict[str, Any], host: dict, *, _seen: set | None = None) -> str | None:
    """Follow build= or same_as= to a build id."""
    _seen = _seen or set()
    sid = host.get("system_id") or host.get("hostname")
    if sid in _seen:
        return None
    if sid:
        _seen.add(str(sid))
    if host.get("build"):
        return host["build"]
    same = host.get("same_as")
    if not same:
        return None
    found = find_host(data, system_id=same, hostname=same)
    if not found:
        return None
    return resolve_build_name(data, found[1], _seen=_seen)


def resolve_lab_power(
    *,
    system_id: str | None = None,
    hostname: str | None = None,
    board_name: str | None = None,
) -> dict[str, Any]:
    """Resolve PSU object + platform_watts for budget.

    Precedence: ~/.lsstack.conf psu_watts (watts override) → host.psu / psu_watts
    → build.psu / psu_watts → none.
    Does not auto-apply board match (that is lslab adopt/enroll).
    """
    from lsstack.psu_roster import normalize_psu_object, psu_from_record

    conf_psu = _conf_value("psu_watts")
    conf_plat = _conf_value("platform_watts")
    conf_psu_i = int(conf_psu) if conf_psu and conf_psu.isdigit() else None
    conf_plat_i = int(conf_plat) if conf_plat and conf_plat.isdigit() else None

    data = load_inventory()
    path = inventory_path(for_write=False)
    ident = detect_identity()
    system_id = system_id or ident.get("system_id")
    hostname = hostname or ident.get("hostname")
    board_name = board_name or ident.get("board_name")

    found = find_host(data, system_id=system_id, hostname=hostname)
    host = found[1] if found else None
    host_key = found[0] if found else None

    build_name = resolve_build_name(data, host) if host else None
    build = (data.get("builds") or {}).get(build_name) if build_name else None

    psu_obj = None
    psu_source = None
    if conf_psu_i is not None:
        # Conf overrides nameplate watts only; keep plugs/ATX from lab when present.
        base = psu_from_record(host) or psu_from_record(build)
        if base:
            psu_obj = dict(base)
            psu_obj["watts"] = conf_psu_i
        else:
            psu_obj = normalize_psu_object(None, watts_only=conf_psu_i)
        psu_source = "conf"
    else:
        host_psu = psu_from_record(host)
        if host_psu:
            psu_obj = host_psu
            psu_source = "host"
        else:
            build_psu = psu_from_record(build)
            if build_psu:
                psu_obj = build_psu
                psu_source = f"build:{build_name}"

    psu = psu_obj["watts"] if psu_obj else None

    platform = conf_plat_i
    platform_source = "conf" if platform is not None else None
    if platform is None and host and host.get("platform_watts") is not None:
        platform = int(host["platform_watts"])
        platform_source = "host"
    if platform is None and build and build.get("platform_watts") is not None:
        platform = int(build["platform_watts"])
        platform_source = f"build:{build_name}"

    matches = builds_matching_board(data, board_name)

    return {
        "inventory_path": path if os.path.isfile(path) else path,
        "inventory_exists": os.path.isfile(path),
        "system_id": system_id,
        "hostname": hostname,
        "board_name": board_name,
        "host_key": host_key,
        "enrolled": host is not None,
        "build": build_name,
        "psu": psu_obj,
        "psu_watts": psu,
        "psu_source": psu_source,
        "platform_watts": platform,
        "platform_source": platform_source,
        "board_build_matches": [n for n, _ in matches],
        "host": host,
        "build_record": build,
    }


def upsert_host(data: dict[str, Any], host: dict) -> str:
    """Insert/update host keyed by system_id (required)."""
    sid = (host.get("system_id") or "").strip()
    if not sid:
        raise ValueError("system_id is required")
    host = dict(host)
    host["system_id"] = sid
    host["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "enrolled_at" not in host:
        host["enrolled_at"] = host["updated_at"]
    data.setdefault("hosts", {})[sid] = host
    return sid


def upsert_build(data: dict[str, Any], name: str, build: dict) -> str:
    name = name.strip()
    if not name:
        raise ValueError("build name required")
    rec = dict(build)
    rec["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data.setdefault("builds", {})[name] = rec
    return name
