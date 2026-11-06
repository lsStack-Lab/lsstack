"""Plain-text save must drop ANSI."""

from __future__ import annotations

import unittest

from lsstack.color import green, strip_ansi


class StripAnsi(unittest.TestCase):
    def test_strips_csi(self) -> None:
        raw = green("[pass]", enabled=True)
        self.assertIn("\x1b[", raw)
        self.assertEqual(strip_ansi(raw), "[pass]")

    def test_plain_unchanged(self) -> None:
        self.assertEqual(strip_ansi("Balance"), "Balance")
        self.assertEqual(strip_ansi(""), "")


if __name__ == "__main__":
    unittest.main()
