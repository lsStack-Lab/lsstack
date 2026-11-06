"""GPU expected width is card max, not empty root lanes."""

from __future__ import annotations

import unittest

from lsstack.topology import _expected_card_width


class ExpectedCardWidth(unittest.TestCase):
    def test_ada_x8_on_x16_root(self) -> None:
        self.assertEqual(_expected_card_width(16, 8, 8), 8)

    def test_min_of_sysfs_and_catalog(self) -> None:
        self.assertEqual(_expected_card_width(16, 8), 8)
        self.assertEqual(_expected_card_width(8), 8)

    def test_ignores_none(self) -> None:
        self.assertEqual(_expected_card_width(None, 8, None), 8)
        self.assertIsNone(_expected_card_width(None, 0, -1))


if __name__ == "__main__":
    unittest.main()
