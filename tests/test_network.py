"""lsnetwork addr/route parsers (no live ip)."""

from __future__ import annotations

import unittest

from lsstack.network import parse_default_routes, parse_ip_o_addr


class IpOAddr(unittest.TestCase):
    def test_inet_and_inet6(self) -> None:
        blob = (
            "2: enp8s0    inet 192.168.1.10/24 brd 192.168.1.255 scope global enp8s0\n"
            "2: enp8s0    inet6 fe80::1/64 scope link \n"
        )
        addrs = parse_ip_o_addr(blob)
        self.assertEqual(addrs[0], "192.168.1.10/24")
        self.assertTrue(any(a.startswith("fe80:") for a in addrs))


class DefaultRoutes(unittest.TestCase):
    def test_v4_dev_via(self) -> None:
        rows = parse_default_routes(
            "default via 192.168.1.1 dev enp8s0 proto dhcp src 192.168.1.10\n",
            "",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dev"], "enp8s0")
        self.assertEqual(rows[0]["via"], "192.168.1.1")
        self.assertEqual(rows[0]["family"], "4")

    def test_v6(self) -> None:
        rows = parse_default_routes(
            "",
            "default via fe80::1 dev enp8s0 proto ra metric 1024\n",
        )
        self.assertEqual(rows[0]["family"], "6")
        self.assertEqual(rows[0]["dev"], "enp8s0")
        self.assertEqual(rows[0]["via"], "fe80::1")


if __name__ == "__main__":
    unittest.main()
