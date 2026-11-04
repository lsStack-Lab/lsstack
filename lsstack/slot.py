"""Compatibility shim: prefer `lsslots` / `lsstack.slots`."""

from __future__ import annotations

import sys
import warnings

from lsstack.slots import get_populated_endpoints, get_slot_info
from lsstack.slots import main as _slots_main

warnings.warn(
    "lsstack.slot is deprecated; use lsstack.slots",
    DeprecationWarning,
    stacklevel=2,
)


def main(args: list[str] | None = None) -> None:
    print("lsslot is deprecated; use lsslots", file=sys.stderr)
    _slots_main(args)


__all__ = ["get_populated_endpoints", "get_slot_info", "main"]
