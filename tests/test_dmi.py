"""SMBIOS enum and Type 0 ROM regressions (no live DMI table)."""

from __future__ import annotations

import unittest

from lsstack.dmi import _FORM_FACTORS, _MEM_TYPES, _type0_rom_size


class Type17Enums(unittest.TestCase):
    """DSP0134 Type 17 enums are 1-based; [0] is unused."""

    def test_form_factor_dimm_is_0x09(self) -> None:
        self.assertEqual(_FORM_FACTORS[0], "")
        self.assertEqual(_FORM_FACTORS[0x09], "DIMM")

    def test_memory_type_ddr4_is_0x18(self) -> None:
        self.assertEqual(_MEM_TYPES[0], "")
        self.assertEqual(_MEM_TYPES[0x18], "DDR4")
        self.assertEqual(_MEM_TYPES[0x1E], "HBM")
        self.assertEqual(_MEM_TYPES[0x20], "DDR5")


class Type0RomSize(unittest.TestCase):
    def _buf(self, *, rom: int, ext: int | None = None, length: int = 26) -> bytes:
        formatted = bytearray(max(length, 26))
        formatted[9] = rom
        if ext is not None:
            formatted[24] = ext & 0xFF
            formatted[25] = (ext >> 8) & 0xFF
        return bytes(formatted)

    def test_legacy_8_mb(self) -> None:
        # 64 KiB * (127 + 1) = 8 MiB
        self.assertEqual(_type0_rom_size(self._buf(rom=127), 18), "8 MB")

    def test_extended_16_mb(self) -> None:
        # Byte 0xFF → Extended WORD: unit 0 = MB, size 16
        self.assertEqual(
            _type0_rom_size(self._buf(rom=0xFF, ext=16), 26),
            "16 MB",
        )

    def test_extended_missing_word_is_none(self) -> None:
        self.assertIsNone(_type0_rom_size(self._buf(rom=0xFF), 24))


if __name__ == "__main__":
    unittest.main()
