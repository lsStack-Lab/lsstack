"""lsrdma link parser and zero-GID skip."""

from __future__ import annotations

import unittest

from lsstack.rdma import _zero_gid, parse_rdma_link


class RdmaLink(unittest.TestCase):
    def test_two_ports(self) -> None:
        blob = (
            "link mlx5_0/1 state ACTIVE physical_state LINK_UP netdev ens1f0\n"
            "link mlx5_0/2 state DOWN physical_state LINK_DOWN netdev ens1f1\n"
        )
        rows = parse_rdma_link(blob)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["dev"], "mlx5_0")
        self.assertEqual(rows[0]["port"], 1)
        self.assertEqual(rows[0]["netdev"], "ens1f0")
        self.assertEqual(rows[1]["state"], "DOWN")
        self.assertEqual(rows[1]["netdev"], "ens1f1")

    def test_no_netdev(self) -> None:
        rows = parse_rdma_link("link mlx5_1/1 state ACTIVE physical_state LINK_UP\n")
        self.assertEqual(rows[0]["netdev"], None)


class Gid(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertTrue(_zero_gid("0000:0000:0000:0000:0000:0000:0000:0000"))
        self.assertFalse(_zero_gid("fe80:0000:0000:0000:0202:c9ff:fe01:0001"))


if __name__ == "__main__":
    unittest.main()
