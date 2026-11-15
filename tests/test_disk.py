"""lsdisk SMART table parse and Attribute/Value/Note rows (no live smartctl)."""

from __future__ import annotations

import unittest

from lsstack.disk import parse_ata_smart_table, smart_detail_rows

_ATA = """
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   096   096   000    Old_age   Always       -       19614
177 Wear_Leveling_Count     0x0013   084   084   000    Pre-fail  Always       -       204
187 Reported_Uncorrect      0x0032   100   100   000    Old_age   Always       -       0
199 UDMA_CRC_Error_Count    0x003e   200   200   000    Old_age   Always       -       1
"""

_NVME = """
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Available Spare:                    100%
Available Spare Threshold:          10%
Percentage Used:                    3%
Power On Hours:                     4210
Media and Data Integrity Errors:    0
"""


class AtaTable(unittest.TestCase):
    def test_value_and_raw(self) -> None:
        rows = parse_ata_smart_table(_ATA)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["Power_On_Hours"]["raw"], 19614)
        self.assertEqual(by_name["Wear_Leveling_Count"]["value"], 84)
        self.assertEqual(by_name["Wear_Leveling_Count"]["raw"], 204)
        self.assertEqual(by_name["UDMA_CRC_Error_Count"]["raw"], 1)


class SmartRowsAta(unittest.TestCase):
    def test_thin_client_shape(self) -> None:
        ata = parse_ata_smart_table(_ATA)
        wear = {
            "protocol": "ata",
            "power_on_hours": 19614,
            "reallocated_sectors": 0,
            "reported_uncorrect": 0,
            "udma_crc_errors": 1,
        }
        rows = smart_detail_rows("PASSED", wear, ata)
        by_attr = {r["attribute"]: r for r in rows}
        self.assertEqual(by_attr["Overall health"]["value"], "PASSED")
        self.assertIsNone(by_attr["Overall health"]["note"])
        self.assertEqual(by_attr["Power_On_Hours"]["value"], "19,614")
        self.assertIn("2.2 years", by_attr["Power_On_Hours"]["note"] or "")
        self.assertEqual(by_attr["Wear_Leveling_Count"]["value"], "84 (raw 204)")
        self.assertIn("~16% write life used", by_attr["Wear_Leveling_Count"]["note"] or "")
        self.assertIsNone(by_attr["Reallocated_Sector_Ct"]["note"])
        self.assertIsNone(by_attr["Reported_Uncorrect"]["note"])
        self.assertEqual(by_attr["UDMA_CRC_Error_Count"]["value"], "1")
        self.assertIn("Sticky", by_attr["UDMA_CRC_Error_Count"]["note"] or "")


class SmartRowsNvme(unittest.TestCase):
    def test_nvme(self) -> None:
        from lsstack.disk import _parse_nvme_smart_attrs

        wear = _parse_nvme_smart_attrs(_NVME)
        rows = smart_detail_rows("PASSED", wear, [])
        by_attr = {r["attribute"]: r for r in rows}
        self.assertEqual(by_attr["Percentage Used"]["value"], "3%")
        self.assertEqual(by_attr["Available Spare"]["value"], "100% (threshold 10%)")
        self.assertIsNone(by_attr["Available Spare"]["note"])
        self.assertEqual(by_attr["Media and Data Integrity Errors"]["value"], "0")
        self.assertIsNone(by_attr["Media and Data Integrity Errors"]["note"])
        self.assertIn("days powered", by_attr["Power_On_Hours"]["note"] or "")


if __name__ == "__main__":
    unittest.main()
