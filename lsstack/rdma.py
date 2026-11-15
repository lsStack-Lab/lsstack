"""lsrdma: InfiniBand / RoCE devices from sysfs, plus rdma link when present.

Maps mlx5_N (or ib0) to PCI BDF and netdev. No config writes.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys

from lsstack.color import attention_note, color_enabled, green, privilege_note, red, yellow
from lsstack.common import read_file, sudo_full_results_note, system_binary
from lsstack.dmi import normalize_bdf

_IB = "/sys/class/infiniband"

_RDMA_LINK_RE = re.compile(
    r"link\s+(\S+)/(\d+)\s+state\s+(\S+)\s+physical_state\s+(\S+)"
    r"(?:\s+netdev\s+(\S+))?",
    re.I,
)


def parse_rdma_link(stdout: str) -> list[dict]:
    """Parse ``rdma link show`` lines into {dev, port, state, phys_state, netdev}."""
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        m = _RDMA_LINK_RE.search(line.strip())
        if not m:
            continue
        rows.append({
            "dev": m.group(1),
            "port": int(m.group(2)),
            "state": m.group(3),
            "phys_state": m.group(4),
            "netdev": m.group(5) or None,
        })
    return rows


def _zero_gid(gid: str) -> bool:
    s = gid.replace(":", "").replace("0x", "").lower()
    return not s or set(s) <= {"0"}


def _rdma_link_rows() -> tuple[list[dict], str | None]:
    exe = system_binary("rdma")
    if not exe:
        return [], "missing_rdma"
    try:
        r = subprocess.run(
            [exe, "link", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return [], "rdma_failed"
    if r.returncode != 0:
        return [], "rdma_failed"
    return parse_rdma_link(r.stdout or ""), None


def _pci_from_ib_dev(path: str) -> str | None:
    dev = os.path.join(path, "device")
    try:
        real = os.path.realpath(dev)
    except OSError:
        return None
    return normalize_bdf(os.path.basename(real))


def _netdevs_from_pci(bdf: str | None) -> list[str]:
    if not bdf:
        return []
    net = f"/sys/bus/pci/devices/{bdf}/net"
    if not os.path.isdir(net):
        return []
    try:
        return sorted(os.listdir(net))
    except OSError:
        return []


def _port_gids(port_dir: str) -> list[str]:
    gdir = os.path.join(port_dir, "gids")
    if not os.path.isdir(gdir):
        return []
    out: list[str] = []
    try:
        names = os.listdir(gdir)
    except OSError:
        return []
    for name in sorted(names, key=lambda x: int(x) if x.isdigit() else x):
        raw = (read_file(os.path.join(gdir, name)) or "").strip()
        if raw and not _zero_gid(raw):
            out.append(raw)
    return out


def _colon_state(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if ":" in s:
        return s.split(":", 1)[1].strip() or s
    return s


def get_rdma_info() -> dict:
    """Verbs devices, ports, netdev map. Empty devices is [na], not an error."""
    link_rows, link_reason = _rdma_link_rows()
    link_by = {(r["dev"], r["port"]): r for r in link_rows}
    devices: list[dict] = []
    if os.path.isdir(_IB):
        try:
            names = sorted(os.listdir(_IB))
        except OSError:
            names = []
        for name in names:
            base = os.path.join(_IB, name)
            if not os.path.isdir(base):
                continue
            pci_bdf = _pci_from_ib_dev(base)
            pci_nets = _netdevs_from_pci(pci_bdf)
            ports_root = os.path.join(base, "ports")
            ports: list[dict] = []
            try:
                pnames = sorted(os.listdir(ports_root), key=lambda x: int(x) if x.isdigit() else x) if os.path.isdir(ports_root) else []
            except OSError:
                pnames = []
            for pn in pnames:
                if not pn.isdigit():
                    continue
                pdir = os.path.join(ports_root, pn)
                pnum = int(pn)
                link = link_by.get((name, pnum)) or {}
                netdev = link.get("netdev")
                if not netdev and len(pci_nets) == 1:
                    netdev = pci_nets[0]
                elif not netdev and pci_nets:
                    # Dual-port: leave None unless rdma link named it.
                    netdev = None
                ports.append({
                    "port": pnum,
                    "state": _colon_state(read_file(os.path.join(pdir, "state"))) or link.get("state"),
                    "phys_state": _colon_state(read_file(os.path.join(pdir, "phys_state"))) or link.get("phys_state"),
                    "rate": (read_file(os.path.join(pdir, "rate")) or "").strip() or None,
                    "link_layer": (read_file(os.path.join(pdir, "link_layer")) or "").strip() or None,
                    "netdev": netdev,
                    "gids": _port_gids(pdir),
                })
            devices.append({
                "name": name,
                "pci_bdf": pci_bdf,
                "node_guid": (read_file(os.path.join(base, "node_guid")) or "").strip() or None,
                "node_type": (read_file(os.path.join(base, "node_type")) or "").strip() or None,
                "netdevs_on_pci": pci_nets,
                "ports": ports,
            })

    reason = None
    if not devices:
        reason = "no_devices"
    return {
        "hostname": socket.gethostname(),
        "reason": reason,
        "rdma_tool": None if link_reason is None else link_reason,
        "rdma_link": link_rows,
        "devices": devices,
    }


def _kv(label: str, value: str | None, width: int = 14) -> None:
    if value is None or value == "":
        return
    print(f"  {label:>{width}}  {value}")


def _section(title: str) -> None:
    print(f"\n  {title}")


def _label_width(*labels: str, minimum: int = 14) -> int:
    return max([minimum, *(len(x) for x in labels if x)])


def _state_color(state: str | None, *, color: bool) -> str:
    s = (state or "—").strip()
    low = s.lower()
    if low in ("active", "link_up"):
        return green(s, enabled=color)
    if low in ("down", "link_down", "disabled"):
        return red(s, enabled=color)
    if low in ("armed", "init", "polling"):
        return yellow(s, enabled=color)
    return s


def _help_text() -> str:
    return """lsrdma — InfiniBand / RoCE devices and netdev map

What
  Reports verbs devices under /sys/class/infiniband (mlx5_N, …), PCI BDF,
  port state/rate/link-layer, and which netdev is bound (from rdma link
  when iproute2 provides it).

Why
  Dual-port ConnectX and similar cards are easy to mix up: mlx5_0 vs
  mlx5_1 vs enpXs0f0. This map is the seated-fabric check before cabling
  or addresses. Ethernet IPs and PCI NICs → lsnetwork.

How
  No devices is [na] (typical workstation with only a GbE NIC), not a
  failure. Missing ``rdma`` (iproute2) still shows sysfs; netdev per port
  may be blank on multi-port cards until that tool is present.
  Lean data → --json.

Usage: lsrdma [options]
  --json, -j       JSON (devices + rdma_link)
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def print_rdma_human(info: dict | None = None, *, color: bool = False) -> None:
    info = info if info is not None else get_rdma_info()
    devices = info.get("devices") or []
    labels = [
        "Host", "Note", "Device", "PCI", "Node GUID", "Type", "Port",
        "State", "Phys", "Rate", "Link", "Netdev", "GID", "PCI netdevs",
    ]
    labels.extend(d.get("name") or "" for d in devices)
    w = _label_width(*labels)

    print("\nRDMA")
    _section("Summary")
    _kv("Host", info.get("hostname"), w)
    nports = sum(len(d.get("ports") or []) for d in devices)
    if info.get("reason") == "no_devices":
        _kv("Note", "[na] no verbs devices under /sys/class/infiniband", w)
    else:
        _kv("Note", f"{len(devices)} device(s); {nports} port(s)", w)
    if devices and info.get("rdma_tool") in ("missing_rdma", "rdma_failed"):
        missing = info.get("rdma_tool") == "missing_rdma"
        _kv(
            "Note",
            attention_note(
                "rdma (iproute2) missing; port↔netdev from PCI only"
                if missing
                else "rdma link failed; port↔netdev from PCI only",
                enabled=color,
            ),
            w,
        )

    if devices:
        _section("Devices")
    for d in devices:
        _kv("Device", d.get("name"), w)
        _kv("PCI", d.get("pci_bdf"), w)
        if d.get("node_guid"):
            _kv("Node GUID", d["node_guid"], w)
        nets = d.get("netdevs_on_pci") or []
        if nets:
            _kv("PCI netdevs", ", ".join(nets), w)
        for p in d.get("ports") or []:
            bits = [f"{p.get('port')}"]
            if p.get("link_layer"):
                bits.append(p["link_layer"])
            _kv("Port", " ".join(str(x) for x in bits), w)
            _kv("State", _state_color(p.get("state"), color=color), w)
            if p.get("phys_state"):
                _kv("Phys", _state_color(p.get("phys_state"), color=color), w)
            if p.get("rate"):
                _kv("Rate", p["rate"], w)
            if p.get("netdev"):
                _kv("Netdev", p["netdev"], w)
            elif nets and len(nets) > 1:
                _kv("Netdev", "— (need rdma link for multi-port)", w)
            gids = p.get("gids") or []
            if gids:
                _kv("GID", gids[0], w)
                for extra in gids[1:3]:
                    _kv("GID", extra, w)

    _section("Notes")
    sudo_note = sudo_full_results_note("lsrdma")
    if sudo_note:
        _kv("Note", privilege_note(sudo_note, enabled=color), w)
    _kv("Note", "IPs and PCI NICs → lsnetwork", w)
    _kv("Note", "does not set addresses, mlxconfig, or netplan", w)
    print()


def main(args: list[str] | None = None) -> None:
    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return
    info = get_rdma_info()
    if "--json" in args or "-j" in args:
        print(json.dumps(info, indent=2, default=str))
        return
    print_rdma_human(info, color=color_enabled(args))


if __name__ == "__main__":
    main()
