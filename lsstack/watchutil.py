"""Shared clear-screen watch loop for lsthermal / lspower / lswatch."""

from __future__ import annotations

import io
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime

DEFAULT_WATCH_SEC = 2.0
MIN_WATCH_SEC = 0.5


def _parse_interval(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        print(
            f"Error: invalid watch interval {raw!r} (use -w N, -wN, or --watch=N seconds)",
            file=sys.stderr,
        )
        sys.exit(2)


def parse_watch_args(args: list[str]) -> tuple[list[str], float | None]:
    """Strip -w/--watch from args.

    -w / --watch → DEFAULT_WATCH_SEC
    -w N / -wN / --watch N / --watch=N → N seconds
    Invalid N exits 2 (does not silently become the default).
    Returns (remaining_args, interval_or_None).
    """
    out: list[str] = []
    interval: float | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-w", "--watch"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                interval = _parse_interval(args[i + 1])
                i += 2
                continue
            interval = DEFAULT_WATCH_SEC
            i += 1
            continue
        if a.startswith("--watch="):
            interval = _parse_interval(a.split("=", 1)[1])
            i += 1
            continue
        if a.startswith("-w") and not a.startswith("--"):
            raw = a[2:]
            if raw:
                interval = _parse_interval(raw)
                i += 1
                continue
        out.append(a)
        i += 1
    if interval is not None:
        interval = max(MIN_WATCH_SEC, interval)
    return out, interval


# Alternate buffer + erase whole screen. CSI H + J only clears from the
# cursor and leaves scrollback, so watch looked like it was appending frames.
_ALT_ENTER = "\033[?1049h"
_ALT_LEAVE = "\033[?1049l"
_CLEAR = "\033[2J\033[H"


def clear_screen() -> None:
    sys.stdout.write(_CLEAR)
    sys.stdout.flush()


def watch_banner(interval: float, title: str = "Watch") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{title}  (updated {ts}, every {interval:g}s — Ctrl-C to stop)")


def run_watch(render: Callable[[], None], interval: float, title: str = "Watch") -> None:
    """Sample off-screen, then paint one complete frame. Alternate buffer on a TTY."""
    use_alt = False
    try:
        use_alt = sys.stdout.isatty()
    except Exception:
        use_alt = False
    try:
        if use_alt:
            sys.stdout.write(_ALT_ENTER)
            clear_screen()
            print(f"{title}  (sampling…)")
            sys.stdout.flush()
        while True:
            t0 = time.monotonic()
            buf = io.StringIO()
            with redirect_stdout(buf):
                render()
            clear_screen()
            watch_banner(interval, title=title)
            sys.stdout.write(buf.getvalue())
            sys.stdout.flush()
            delay = interval - (time.monotonic() - t0)
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        if use_alt:
            sys.stdout.write(_ALT_LEAVE)
            sys.stdout.flush()
