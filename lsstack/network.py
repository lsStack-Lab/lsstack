"""lsnetwork: network interfaces from sysfs (/sys/class/net).

Also exposes get_nic_devices() for PCI NICs (add-on vs onboard when Type 41 lists them).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys

from lsstack.color import color_enabled, green, privilege_note, red, yellow
from lsstack.common import load_json, lookup_nic_specs, read_file, sudo_full_results_note, system_binary

_NET = "/sys/class/net"
_PCI = "/sys/bus/pci/devices"
_ONBOARD_UNSET = object()


def _iface_addrs(name: str) -> list[str]:
    """Optional addresses via distro `ip` (iproute2); empty if unavailable."""
    exe = system_binary("ip")
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "-br", "addr", "show", "dev", name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        parts = r.stdout.strip().split()
        return [p for p in parts[2:] if "/" in p or ":" in p or "." in p]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _permanent_mac(name: str) -> str | None:
    """Permanent (burned-in) MAC via ethtool -P when available."""
    exe = system_binary("ethtool")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-P", name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        text = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"Permanent address:\s*([0-9a-fA-F:]{11,})", text)
        if m:
            mac = m.group(1).lower()
            if mac != "00:00:00:00:00:00":
                return mac
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _lspci_name(bdf: str) -> str | None:
    exe = system_binary("lspci")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-s", bdf, "-nn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # e.g. "07:00.0 Ethernet controller [0200]: Intel Corporation I211 ... [8086:1539]"
        line = r.stdout.strip().splitlines()[0]
        if ": " in line:
            return line.split(": ", 1)[1].strip()
        return line
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _dmi_onboard_ethernet_bdfs() -> set[str] | None:
    """Bus addresses of onboard Ethernet from SMBIOS Type 41, or None if unavailable."""
    from lsstack.dmi import onboard_ethernet_bdfs

    return onboard_ethernet_bdfs()


def get_network_info() -> list[dict]:
    """List interfaces with operstate, speed, duplex, MAC, MTU, optional addrs."""
    out: list[dict] = []
    if not os.path.isdir(_NET):
        return out
    for name in sorted(os.listdir(_NET)):
        base = os.path.join(_NET, name)
        if not os.path.isdir(base):
            continue
        oper = (read_file(os.path.join(base, "operstate")) or "unknown").strip()
        speed = read_file(os.path.join(base, "speed"))
        duplex = read_file(os.path.join(base, "duplex"))
        mac = read_file(os.path.join(base, "address"))
        mtu = read_file(os.path.join(base, "mtu"))
        carrier = read_file(os.path.join(base, "carrier"))
        device = None
        pci_bdf = None
        driver = None
        try:
            device_path = os.path.realpath(os.path.join(base, "device"))
            if "virtual" in device_path:
                device = "virtual"
            else:
                from lsstack.dmi import normalize_bdf

                device = os.path.basename(device_path)
                pci_bdf = normalize_bdf(device)
                drv_link = os.path.join(device_path, "driver")
                if os.path.islink(drv_link) or os.path.exists(drv_link):
                    try:
                        driver = os.path.basename(os.path.realpath(drv_link))
                    except OSError:
                        driver = None
        except OSError:
            device = None
        speed_mbps = None
        if speed and speed.strip().lstrip("-").isdigit():
            v = int(speed.strip())
            if v > 0:
                speed_mbps = v
        is_lo = name == "lo"
        addrs = _iface_addrs(name) if not is_lo else []
        perm = _permanent_mac(name) if not is_lo else None
        out.append({
            "name": name,
            "operstate": oper,
            "speed_mbps": speed_mbps,
            "duplex": (duplex or "").strip() or None,
            "mac": (mac or "").strip() or None,
            "permanent_mac": perm,
            "mtu": int(mtu) if mtu and mtu.isdigit() else None,
            "carrier": (carrier or "").strip() or None,
            "device": device,
            "pci_bdf": pci_bdf,
            "driver": driver,
            "addrs": addrs,
            "is_loopback": is_lo,
        })
    return out


def get_nic_devices(onboard_bdfs: set[str] | None | object = _ONBOARD_UNSET) -> list[dict]:
    """PCI network controllers (class 0x02*) — add-on vs onboard when Type 41 lists them.

    One entry per PCI function. Correlates netdevs, permanent MAC, optional add_on flag.
    add_on is None when the DMI table is unreadable or Type 41 lists no Ethernet.
    """
    if onboard_bdfs is _ONBOARD_UNSET:
        onboard_bdfs = _dmi_onboard_ethernet_bdfs()
    nic_db = load_json("nic_pci_id.json") or {}
    out: list[dict] = []
    if not os.path.isdir(_PCI):
        return out
    try:
        bdfs = os.listdir(_PCI)
    except OSError:
        return out

    for bdf in sorted(bdfs):
        path = os.path.join(_PCI, bdf)
        cls = read_file(os.path.join(path, "class"))
        if not cls:
            continue
        try:
            class_int = int(cls.strip(), 16)
        except ValueError:
            continue
        # Base class 0x02 = Network controller
        if (class_int >> 16) != 0x02:
            continue

        vendor = (read_file(os.path.join(path, "vendor")) or "").strip() or None
        device = (read_file(os.path.join(path, "device")) or "").strip() or None
        sub_vendor = (read_file(os.path.join(path, "subsystem_vendor")) or "").strip() or None
        sub_device = (read_file(os.path.join(path, "subsystem_device")) or "").strip() or None
        driver = None
        drv_link = os.path.join(path, "driver")
        if os.path.exists(drv_link):
            try:
                driver = os.path.basename(os.path.realpath(drv_link))
            except OSError:
                driver = None
        link_speed = read_file(os.path.join(path, "current_link_speed"))
        link_width = read_file(os.path.join(path, "current_link_width"))
        max_speed = read_file(os.path.join(path, "max_link_speed"))
        max_width = read_file(os.path.join(path, "max_link_width"))

        ifaces: list[str] = []
        net_dir = os.path.join(path, "net")
        if os.path.isdir(net_dir):
            try:
                ifaces = sorted(os.listdir(net_dir))
            except OSError:
                ifaces = []

        mac = None
        permanent_mac = None
        speed_mbps = None
        for iname in ifaces:
            ibase = os.path.join(_NET, iname)
            if mac is None:
                mac = (read_file(os.path.join(ibase, "address")) or "").strip() or None
            if permanent_mac is None:
                permanent_mac = _permanent_mac(iname)
            if speed_mbps is None:
                sp = read_file(os.path.join(ibase, "speed"))
                if sp and sp.strip().lstrip("-").isdigit():
                    v = int(sp.strip())
                    if v > 0:
                        speed_mbps = v

        # Empty Type 41 is not "all add-on"; firmware often omits onboard Ethernet.
        add_on: bool | None
        if onboard_bdfs:
            add_on = bdf.lower() not in onboard_bdfs
        else:
            add_on = None

        # Sub-class hint: 0x00 Ethernet, 0x07 InfiniBand, …
        subclass = (class_int >> 8) & 0xFF
        subclass_name = {
            0x00: "ethernet",
            0x01: "token_ring",
            0x02: "fddi",
            0x03: "atm",
            0x04: "isdn",
            0x05: "worldfip",
            0x06: "picmg",
            0x07: "infiniband",
            0x08: "fabric",
        }.get(subclass, f"network_{subclass:#04x}")

        lspci_model = _lspci_name(bdf)
        specs = lookup_nic_specs(nic_db, vendor, device)
        out.append({
            "bdf": bdf,
            "vendor": vendor,
            "device": device,
            "subsystem_vendor": sub_vendor,
            "subsystem_device": sub_device,
            "driver": driver,
            "model": (specs or {}).get("model") or lspci_model,
            "lspci_model": lspci_model,
            "subclass": subclass_name,
            "ifaces": ifaces,
            "mac": permanent_mac or mac,
            "permanent_mac": permanent_mac,
            "current_mac": mac,
            "speed_mbps": speed_mbps,
            "pcie_link_speed": (link_speed or "").strip() or None,
            "pcie_link_width": (link_width or "").strip() or None,
            "pcie_max_speed": (max_speed or "").strip() or None,
            "pcie_max_width": (max_width or "").strip() or None,
            "add_on": add_on,
            "specs": specs,
        })
    return out


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _add_on_label(add_on: bool | None) -> str:
    if add_on is True:
        return "add-on"
    if add_on is False:
        return "onboard"
    return "add-on/onboard unknown"


def _state_display(oper: str | None, carrier: str | None, *, color: bool) -> str:
    state = (oper or "unknown").strip() or "unknown"
    extra = ""
    if carrier == "0" and state != "down":
        extra = " (no carrier)"
    text = state + extra
    low = state.lower()
    if low == "up" and carrier != "0":
        return green(text, enabled=color)
    if low == "down" or carrier == "0":
        return red(text, enabled=color)
    if low in ("dormant", "lowerlayerdown"):
        return yellow(text, enabled=color)
    return text


def _width_int(raw) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().lstrip("x")
    if s.isdigit():
        return int(s)
    return None


def _pcie_link_line(nic: dict) -> str | None:
    speed = nic.get("pcie_link_speed")
    width = nic.get("pcie_link_width")
    max_speed = nic.get("pcie_max_speed")
    max_width = nic.get("pcie_max_width")
    if not speed and not width and not max_speed and not max_width:
        return None
    w = f"x{width}" if width and not str(width).startswith("x") else (width or "—")
    bits = [f"{w}, {speed or '—'}"]
    max_bits = []
    if max_width:
        mw = f"x{max_width}" if not str(max_width).startswith("x") else str(max_width)
        max_bits.append(mw)
    if max_speed:
        max_bits.append(max_speed.replace(" PCIe", ""))
    if max_bits:
        bits.append(f"(max {' '.join(max_bits)})")
    return " ".join(bits)


def _link_below_max(nic: dict) -> bool:
    cur_w = _width_int(nic.get("pcie_link_width"))
    max_w = _width_int(nic.get("pcie_max_width"))
    if cur_w is not None and max_w is not None and cur_w < max_w:
        return True
    cur_s = nic.get("pcie_link_speed") or ""
    max_s = nic.get("pcie_max_speed") or ""
    return bool(cur_s and max_s and cur_s != max_s)


def _help_text() -> str:
    return """lsnetwork — network interfaces and PCI NICs for lab / workstation builds

What
  Reports Linux netdevs (state, speed, MAC, addresses) and PCI network
  controllers (model, driver, PCIe link, onboard vs add-on when DMI allows).

Why
  Builders need to know which physical NIC is which, whether the cable link is
  up at the expected speed, and how the card sits on PCIe — before chasing
  switch or firewall problems.

How
  Run on the host; read Interfaces first, then PCI NICs for hardware identity.
  Kernel module is on the Driver line. PCIe gen/width and idle Gen1 → lstopology.
  Lean machine output → --json. Loopback → --all.

Usage: lsnetwork [options]
  --json, -j       JSON (interfaces + nics)
  --all            Include loopback (lo)
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def get_network_bundle() -> dict:
    """Interfaces + PCI NICs for human/--json."""
    onboard = _dmi_onboard_ethernet_bdfs()
    return {
        "hostname": socket.gethostname(),
        "interfaces": get_network_info(),
        "nics": get_nic_devices(onboard_bdfs=onboard),
        "dmi_onboard_ethernet": None if onboard is None else sorted(onboard),
    }


def print_network_human(
    info: dict | None = None,
    *,
    color: bool = False,
    show_loopback: bool = False,
) -> None:
    info = info if info is not None else get_network_bundle()
    ifaces_all = info.get("interfaces") or []
    nics = info.get("nics") or []
    lo = [i for i in ifaces_all if i.get("is_loopback")]
    ifaces = ifaces_all if show_loopback else [i for i in ifaces_all if not i.get("is_loopback")]

    labels = [
        "Host", "Note", "Interface", "State", "Speed", "Duplex", "MAC",
        "Permanent MAC", "MTU", "PCI", "Driver", "Addresses", "Carrier",
        "BDF", "Model", "Subclass", "Interfaces", "PCIe link",
        "Rated", "Ports", "RDMA", "Catalog", "Device",
    ]
    labels.extend(i.get("name") or "" for i in ifaces)
    labels.extend(n.get("bdf") or "" for n in nics)
    w = _label_width(*labels)

    print("\nNetwork")

    _section("Summary")
    _kv("Host", info.get("hostname"), w)
    up = sum(1 for i in ifaces if (i.get("operstate") or "").lower() == "up")
    downish = [
        i for i in ifaces
        if (i.get("operstate") or "").lower() == "down" or i.get("carrier") == "0"
    ]
    _kv(
        "Note",
        f"{len(ifaces)} interface(s)"
        + (f"; {up} up" if ifaces else "")
        + (f"; {len(downish)} down/no-carrier" if downish else "")
        + (f"; {len(nics)} PCI NIC(s)" if nics else ""),
        w,
    )
    if not show_loopback and lo:
        _kv("Note", f"loopback hidden ({len(lo)}); show with --all", w)

    _section("Interfaces")
    if not ifaces:
        _kv("Note", "none under /sys/class/net" + ("" if show_loopback else " (besides lo)"), w)
    for i in ifaces:
        _kv("Interface", i.get("name"), w)
        _kv("State", _state_display(i.get("operstate"), i.get("carrier"), color=color), w)
        if i.get("speed_mbps"):
            _kv("Speed", f"{i['speed_mbps']} Mbps", w)
        elif (i.get("operstate") or "").lower() == "up":
            _kv("Speed", "— (sysfs speed unread)", w)
        if i.get("duplex"):
            _kv("Duplex", i["duplex"], w)
        if i.get("mac") and not i.get("is_loopback"):
            _kv("MAC", i["mac"], w)
        if i.get("permanent_mac") and i.get("permanent_mac") != i.get("mac"):
            _kv("Permanent MAC", i["permanent_mac"], w)
        if i.get("mtu") is not None:
            _kv("MTU", str(i["mtu"]), w)
        if i.get("pci_bdf"):
            _kv("PCI", i["pci_bdf"], w)
        elif i.get("device"):
            _kv("Device", i["device"], w)
        if i.get("driver"):
            _kv("Driver", i["driver"], w)
        if i.get("addrs"):
            _kv("Addresses", ", ".join(i["addrs"]), w)
        elif not i.get("is_loopback"):
            _kv("Addresses", "— (none or iproute2 unavailable)", w)

    _section("PCI NICs")
    if not nics:
        _kv("Note", "no PCI class 0x02 network controllers", w)
    elif nics and all(n.get("add_on") is None for n in nics):
        if "dmi_onboard_ethernet" not in info:
            _kv("Note", "onboard vs add-on unknown", w)
        elif info.get("dmi_onboard_ethernet") is None:
            _kv(
                "Note",
                privilege_note("— (needs sudo for DMI Type 41)", enabled=color),
                w,
            )
        else:
            _kv("Note", "no SMBIOS Type 41 Ethernet; onboard vs add-on unknown", w)
    for n in nics:
        add_on = n.get("add_on")
        bdf = n.get("bdf") or ""
        if add_on is None:
            _kv("BDF", bdf, w)
        else:
            _kv("BDF", f"{bdf} ({_add_on_label(add_on)})", w)
        if n.get("model"):
            _kv("Model", n["model"], w)
        specs = n.get("specs")
        if specs:
            if specs.get("port_speed_gbps") is not None:
                _kv("Rated", f"{specs['port_speed_gbps']} Gbps", w)
            if specs.get("ports") is not None:
                _kv("Ports", str(specs["ports"]), w)
            if specs.get("rdma") and specs.get("rdma") != "none":
                _kv("RDMA", str(specs["rdma"]), w)
            live = n.get("speed_mbps")
            rated = specs.get("port_speed_gbps")
            if live and rated:
                try:
                    live_g = float(live) / 1000.0
                    if live_g + 0.05 < float(rated) * 0.5:
                        _kv(
                            "Note",
                            f"live link {live} Mbps << curated {rated} Gbps "
                            "(cable/slot/driver?)",
                            w,
                        )
                except (TypeError, ValueError):
                    pass
        else:
            vend = (n.get("vendor") or "").replace("0x", "")
            dev = (n.get("device") or "").replace("0x", "")
            _kv(
                "Catalog",
                f"No curated specs for {vend}:{dev} (not in lab catalog). "
                "Add with: lscatalog add nic --from-live",
                w,
            )
        if n.get("subclass"):
            _kv("Subclass", n["subclass"], w)
        if n.get("driver"):
            _kv("Driver", n["driver"], w)
        if n.get("ifaces"):
            _kv("Interfaces", ", ".join(n["ifaces"]), w)
        else:
            _kv("Interfaces", "— (no netdev bound)", w)
        if n.get("mac"):
            _kv("MAC", n["mac"], w)
        if n.get("speed_mbps"):
            _kv("Speed", f"{n['speed_mbps']} Mbps", w)
        link = _pcie_link_line(n)
        if link:
            _kv("PCIe link", link, w)
        if _link_below_max(n):
            _kv("Note", f"{n.get('bdf') or 'NIC'} below device max — see lstopology", w)

    _section("Notes")
    sudo_note = sudo_full_results_note("lsnetwork")
    if sudo_note:
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    _kv("Note", "PCIe path / idle Gen1 → lstopology", w)
    _kv("Note", "lab roster → lscatalog / nic_pci_id", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    # -v/--verbose accepted as no-ops; human output is always full. Lean data → --json.

    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    info = get_network_bundle()

    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return

    print_network_human(
        info,
        color=color_enabled(args),
        show_loopback=("--all" in args),
    )


if __name__ == "__main__":
    main()
