"""System design power budget: spec watts where known, class estimates otherwise.

Used by lspower and lsstack (report) so headroom math stays consistent.
"""

from __future__ import annotations

import os
import re

from lsstack.common import read_file

_CONF = os.path.expanduser("~/.lsstack.conf")

# --- Class estimates (W). Not nameplate; shown as kind=est on the ledger. ---
DIMM_WATTS = {
    "DDR3": 4,
    "DDR4": 4,
    "DDR5": 5,
    "LPDDR4": 3,
    "LPDDR5": 4,
    "UNKNOWN": 4,
}

DISK_WATTS = {
    "NVMe": 8,
    "SSD": 3,
    "HDD": 8,
    "Unknown": 5,
}

# NIC / DPU class estimates (per PCI network function)
NIC_WATTS = {
    "ethernet_1g": 4,
    "ethernet_2_5g": 5,
    "ethernet_5g": 6,
    "ethernet_10g": 12,
    "ethernet_25g": 18,
    "ethernet_40g": 25,
    "ethernet_50g": 30,
    "ethernet_100g": 40,
    "ethernet_200g": 55,
    "infiniband": 18,
    "dpu": 75,  # BlueField-class mid; BF3 peak can be higher
    "other": 8,
}

FAN_WATTS = 3  # per running fan (hwmon rpm > 0)
DEFAULT_PLATFORM_WATTS = 40  # board/chipset/USB/misc; override via platform_watts= in conf

_DPU_HINTS = re.compile(
    r"bluefield|connectx.?dpu|nvidia.?dpu|\bdpu\b|pensando|fungible|ionic|"
    r"amd.?pensando|ipuc|smart.?nic|bf3|bf2",
    re.I,
)
def _conf_int(key: str) -> int | None:
    if not os.path.isfile(_CONF):
        return None
    try:
        with open(_CONF, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return int(v.strip())
    except (OSError, ValueError):
        return None
    return None


def _lab_power() -> dict:
    """PSU/platform from lab inventory (after local conf inside resolve)."""
    try:
        from lsstack.lab_inventory import resolve_lab_power
        return resolve_lab_power()
    except Exception:
        return {}


def _normalize_dram_class(type_str: str | None) -> str:
    t = (type_str or "").strip().upper().replace(" ", "")
    if not t or t in ("-", "UNKNOWN", "DRAM", "OTHER"):
        return "UNKNOWN"
    for key in ("DDR5", "DDR4", "DDR3", "LPDDR5", "LPDDR4", "LPDDR3"):
        if key in t:
            return key
    return "UNKNOWN"


def _dimm_row(cls: str, source: str, part_number: str | None) -> dict:
    return {
        "class": cls,
        "watts": DIMM_WATTS.get(cls, DIMM_WATTS["UNKNOWN"]),
        "source": source,
        "part_number": part_number,
    }


def _populated_dimms() -> list[dict]:
    """Populated modules: SPD when it covers the set, else DMI Type 17.

    Incomplete SPD (one EEPROM dead) must not hide extra Type 17 sticks.
    """
    from lsstack import memory as memory_mod

    spd: list[dict] = []
    spd_fn = getattr(memory_mod, "_spd_per_dimm", None)
    if callable(spd_fn):
        for s in spd_fn() or []:
            spd.append(_dimm_row(
                _normalize_dram_class(s.get("type")),
                "spd",
                s.get("part_number"),
            ))

    dmi: list[dict] = []
    infer = getattr(memory_mod, "_infer_dram_gen_from_part_number", None)
    for d in memory_mod.get_dimm_devices() or []:
        size = str(d.get("size") or d.get("size_str") or "0")
        if size in ("0", "0 GiB", "[empty]") or d.get("description") == "[empty]":
            continue
        cls = _normalize_dram_class(d.get("type") or d.get("memory_type"))
        if cls == "UNKNOWN" and d.get("part_number") and callable(infer):
            inferred = infer(d.get("part_number") or "")
            if inferred:
                cls = inferred
        dmi.append(_dimm_row(cls, "dmi", d.get("part_number")))

    if spd and (not dmi or len(spd) >= len(dmi)):
        return spd
    return dmi


def _running_fans() -> list[dict]:
    """Fans with rpm > 0 (empty headers stay 0 and are excluded)."""
    from lsstack.thermal import get_hwmon_fans

    fans = get_hwmon_fans() or []
    # Prefer asus_wmi labels; get_hwmon_fans already non-zero only
    seen: set[str] = set()
    out: list[dict] = []
    for f in fans:
        lab = (f.get("label") or "").strip()
        key = lab.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": lab, "rpm": f.get("rpm"), "watts": FAN_WATTS})
    return out


def _disk_lines() -> list[dict]:
    from lsstack.disk import get_disk_info

    out: list[dict] = []
    for d in get_disk_info() or []:
        typ = (d.get("type") or "Unknown").strip()
        w = DISK_WATTS.get(typ, DISK_WATTS["Unknown"])
        out.append({
            "device": d.get("device"),
            "class": typ,
            "watts": w,
        })
    return out


def _classify_nic(nic: dict) -> str:
    model = (nic.get("model") or "") + " " + (nic.get("driver") or "")
    # DPU vs NIC: nameplate/model string (ConnectX alone is NIC; BlueField is DPU)
    if _DPU_HINTS.search(model):
        return "dpu"

    subclass = (nic.get("subclass") or "").lower()
    if subclass == "infiniband":
        return "infiniband"

    speed = nic.get("speed_mbps")
    # Link down → infer from model strings
    model_l = (nic.get("model") or "").lower()
    if speed is None or speed <= 0:
        if re.search(r"100\s*gbe|100gbe|100gb", model_l):
            speed = 100_000
        elif re.search(r"50\s*gbe|50gbe", model_l):
            speed = 50_000
        elif re.search(r"40\s*gbe|40gbe", model_l):
            speed = 40_000
        elif re.search(r"25\s*gbe|25gbe", model_l):
            speed = 25_000
        elif re.search(r"10\s*gbe|10gbe|10-gigabit|x540|x550|x710|xxv710", model_l):
            speed = 10_000
        elif re.search(r"2\.5|i225|i226", model_l):
            speed = 2500
        elif re.search(r"gigabit|i211|e1000|igb|igc", model_l):
            speed = 1000

    if isinstance(speed, int):
        if speed >= 200_000:
            return "ethernet_200g"
        if speed >= 100_000:
            return "ethernet_100g"
        if speed >= 50_000:
            return "ethernet_50g"
        if speed >= 40_000:
            return "ethernet_40g"
        if speed >= 25_000:
            return "ethernet_25g"
        if speed >= 10_000:
            return "ethernet_10g"
        if speed >= 5000:
            return "ethernet_5g"
        if speed >= 2500:
            return "ethernet_2_5g"
        if speed >= 1000:
            return "ethernet_1g"

    if subclass == "ethernet":
        return "ethernet_1g"
    return "other"


def _nic_lines() -> list[dict]:
    from lsstack.network import get_nic_devices

    out: list[dict] = []
    for n in get_nic_devices() or []:
        cls = _classify_nic(n)
        out.append({
            "bdf": n.get("bdf"),
            "class": cls,
            "watts": NIC_WATTS.get(cls, NIC_WATTS["other"]),
            "model": n.get("model"),
            "speed_mbps": n.get("speed_mbps"),
        })
    return out


def _line(
    key: str,
    label: str,
    watts: float | int | None,
    kind: str,
    detail: str | None = None,
    count: int | None = None,
    include_in_total: bool = True,
) -> dict:
    return {
        "key": key,
        "label": label,
        "watts": None if watts is None else int(round(watts)),
        "kind": kind,  # "spec" | "est"
        "detail": detail,
        "count": count,
        "include_in_total": include_in_total,
    }


def estimate_system_budget(
    *,
    cpu_info: dict | None = None,
    gpu_list: list | None = None,
) -> dict:
    """Build design power ledger.

    Returns dict with lines[], total_w, spec_w, est_w, psu_watts, headroom, …
    """
    if cpu_info is None:
        from lsstack.proc import get_cpu_info
        cpu_info = get_cpu_info()
    if gpu_list is None:
        from lsstack.gpu import get_gpu_info
        gpu_list, _ = get_gpu_info()

    lines: list[dict] = []

    # CPU — spec (0 W + reason when the catalog has no TDP)
    cpu_tdp = None
    specs = (cpu_info or {}).get("specs") or {}
    cpu_name = (cpu_info or {}).get("model_name")
    if specs.get("tdp_watts") is not None:
        cpu_tdp = int(specs["tdp_watts"])
        lines.append(_line("cpu", "CPU", cpu_tdp, "spec", detail=cpu_name))
    else:
        detail = "TDP unknown (not in catalog)"
        if cpu_name:
            detail = f"{detail}  {cpu_name}"
        lines.append(_line("cpu", "CPU", 0, "spec", detail=detail))

    # GPU(s) — spec (same 0 W row when a live GPU has no catalog TDP)
    gpu_tdp_total = 0
    gpu_n = 0
    gpus = gpu_list or []
    multi = len(gpus) > 1
    for i, g in enumerate(gpus):
        sp = g.get("specs") or {}
        name = sp.get("model") or (g.get("nvidia_smi") or {}).get("name") or g.get("bus_id") or f"GPU {i}"
        key = "gpu" if not multi else f"gpu_{i}"
        label = "GPU" if not multi else f"GPU {i}"
        if sp.get("tdp_watts") is None:
            lines.append(_line(
                key, label, 0, "spec",
                detail=f"TDP unknown (not in catalog)  {name}",
            ))
            continue
        w = int(sp["tdp_watts"])
        gpu_tdp_total += w
        gpu_n += 1
        lines.append(_line(key, label, w, "spec", detail=str(name)))

    # DRAM — class × populated count
    dimms = _populated_dimms()
    if dimms:
        by_cls: dict[str, list] = {}
        for d in dimms:
            by_cls.setdefault(d["class"], []).append(d)
        for cls, group in sorted(by_cls.items()):
            w_each = group[0]["watts"]
            n = len(group)
            lines.append(_line(
                f"dram_{cls.lower()}",
                f"DRAM ({cls})",
                w_each * n,
                "est",
                detail=f"{w_each} W × {n} DIMM(s)",
                count=n,
            ))
    else:
        from lsstack.dmi import probe_table

        probe = probe_table()
        if probe.get("reason") == "needs_sudo":
            dram_detail = "needs sudo for DMI Type 17 / SPD"
        elif not probe.get("ok"):
            dram_detail = "count unknown (no DMI table)"
        else:
            dram_detail = "count unknown (no populated Type 17 rows)"
        lines.append(_line(
            "dram",
            "DRAM",
            None,
            "est",
            detail=dram_detail,
            count=None,
            include_in_total=False,
        ))

    # Storage — class × disk
    disks = _disk_lines()
    if disks:
        by_cls: dict[str, list] = {}
        for d in disks:
            by_cls.setdefault(d["class"], []).append(d)
        for cls, group in sorted(by_cls.items()):
            w_each = group[0]["watts"]
            n = len(group)
            lines.append(_line(
                f"disk_{cls.lower()}",
                f"Storage ({cls})",
                w_each * n,
                "est",
                detail=f"{w_each} W × {n}",
                count=n,
            ))

    # NICs / DPUs — class per PCI function
    nics = _nic_lines()
    if nics:
        by_cls: dict[str, list] = {}
        for n in nics:
            by_cls.setdefault(n["class"], []).append(n)
        for cls, group in sorted(by_cls.items()):
            w_each = group[0]["watts"]
            n = len(group)
            label = "DPU" if cls == "dpu" else f"NIC ({cls})"
            lines.append(_line(
                f"nic_{cls}",
                label,
                w_each * n,
                "est",
                detail=f"{w_each} W × {n}",
                count=n,
            ))

    # Fans — running only (accurate connected/spinning count)
    fans = _running_fans()
    if fans:
        n = len(fans)
        lines.append(_line(
            "fans",
            "Fans",
            FAN_WATTS * n,
            "est",
            detail=f"{FAN_WATTS} W × {n} running",
            count=n,
        ))

    # Platform + PSU: conf override (inside lab resolve) → host/build → default platform
    lab = _lab_power()
    platform = lab.get("platform_watts")
    platform_src = lab.get("platform_source") or "default"
    if platform is None:
        platform = _conf_int("platform_watts")
        if platform is not None:
            platform_src = "conf"
    if platform is None:
        platform = DEFAULT_PLATFORM_WATTS
        platform_src = "default"
    plat_hint = {
        "conf": "from ~/.lsstack.conf",
        "host": "from lslab host",
        "default": "default; lslab mkbuild / platform_watts=",
    }.get(platform_src, platform_src)
    if platform_src and str(platform_src).startswith("build:"):
        plat_hint = f"from lslab {platform_src}"
    lines.append(_line(
        "platform",
        "Platform",
        platform,
        "est",
        detail=f"board/chipset/USB ({plat_hint})",
    ))

    def _sum_kind(kind: str) -> int:
        return sum(
            int(L["watts"])
            for L in lines
            if L.get("kind") == kind
            and L.get("include_in_total", True)
            and L.get("watts") is not None
        )

    spec_w = _sum_kind("spec")
    est_w = _sum_kind("est")
    total_w = spec_w + est_w

    psu_obj = lab.get("psu")
    psu = lab.get("psu_watts")
    if psu is None:
        psu = _conf_int("psu_watts")
        if psu is not None and psu_obj is None:
            from lsstack.psu_roster import normalize_psu_object
            psu_obj = normalize_psu_object(None, watts_only=psu)
    psu_source = lab.get("psu_source") if lab.get("psu_watts") is not None else (
        "conf" if psu is not None else None
    )
    headroom = None
    if psu is not None and psu > 0:
        pct = round(100.0 * total_w / psu, 1)
        headroom = {
            "psu_watts": psu,
            "used_w": total_w,
            "pct_of_psu": pct,
            "ok": pct <= 80.0,
            "psu_source": psu_source,
        }

    return {
        "lines": lines,
        "total_w": total_w,
        "spec_w": spec_w,
        "est_w": est_w,
        "cpu_tdp_w": cpu_tdp,
        "gpu_tdp_w": gpu_tdp_total if gpu_n else None,
        "gpu_count": gpu_n,
        "dimm_count": len(dimms),
        "fan_count": len(fans),
        "nic_count": len(nics),
        "psu": psu_obj,
        "psu_watts": psu,
        "psu_source": psu_source,
        "platform_watts": platform,
        "platform_source": platform_src,
        "lab": {
            "enrolled": lab.get("enrolled"),
            "build": lab.get("build"),
            "system_id": lab.get("system_id"),
            "inventory_path": lab.get("inventory_path"),
            "board_build_matches": lab.get("board_build_matches"),
        },
        "headroom": headroom,
        "dimms": dimms,
        "fans": fans,
        "nics": nics,
        "disks": disks,
    }
