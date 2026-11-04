"""Shared clear-screen watch loop for lsthermal / lspower / lswatch."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime

DEFAULT_WATCH_SEC = 2.0
MIN_WATCH_SEC = 0.5


def parse_watch_args(args: list[str]) -> tuple[list[str], float | None]:
    """Strip -w/--watch from args.

    -w / --watch → DEFAULT_WATCH_SEC
    -w N / --watch N / --watch=N → N seconds
    Returns (remaining_args, interval_or_None).
    """
    out: list[str] = []
    interval: float | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-w", "--watch"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                try:
                    interval = float(args[i + 1])
                    i += 2
                    continue
                except ValueError:
                    interval = DEFAULT_WATCH_SEC
                    i += 1
                    continue
            interval = DEFAULT_WATCH_SEC
            i += 1
            continue
        if a.startswith("--watch="):
            try:
                interval = float(a.split("=", 1)[1])
            except ValueError:
                interval = DEFAULT_WATCH_SEC
            i += 1
            continue
        out.append(a)
        i += 1
    if interval is not None:
        interval = max(MIN_WATCH_SEC, interval)
    return out, interval


def clear_screen() -> None:
    # Home + clear-to-end; works in typical terminals without curses.
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def watch_banner(interval: float, title: str = "Watch") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{title}  (updated {ts}, every {interval:g}s — Ctrl-C to stop)")


def run_watch(render: Callable[[], None], interval: float, title: str = "Watch") -> None:
    """Clear, render, sleep until Ctrl-C."""
    try:
        while True:
            clear_screen()
            watch_banner(interval, title=title)
            render()
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n", file=sys.stderr)
