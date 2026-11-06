"""lswatch: combined thermal + power live view (refresh default on)."""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from lsstack.power import get_power_info, print_power_human
from lsstack.thermal import get_thermal_info, print_thermal_human
from lsstack.watchutil import DEFAULT_WATCH_SEC, parse_watch_args, run_watch

# RAPL watts need two energy_uj samples with dt > 0.05s (see lspower).
_RAPL_PRIME_SEC = 0.2


def print_combined(*, color: bool = False) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        ft = pool.submit(get_thermal_info)
        fp = pool.submit(get_power_info)
        tinfo = ft.result()
        pinfo = fp.result()
    print_thermal_human(tinfo, watching=True, heading=True, color=color)
    print_power_human(pinfo, watching=True, heading=True, color=color)


def _help_text() -> str:
    return """lswatch — live thermal + power on one screen (lab / workstation)

What
  Refreshes lsthermal and lspower together: temps, fans, live GPU/RAPL draw,
  rails, and PSU headroom.

Why
  A card that is hot while the brick looks idle, or a rail that sags under
  load, only shows up when heat and watts are watched at the same time.

How
  Default is watch: one screen, rewritten in place (not a growing log).
  Live temps/fans + live watts/rails. Temps are green/yellow/red (CPU uses
  catalog Tjmax when present). PSU ports and TDP stay on lspower without -w.
  One-shot: lswatch --once. Interval: -w, -w N, or -wN.
  Do not combine --once with -w.

Usage: lswatch [options]
  -w, --watch [N]  Interval in seconds (default 2; -w2 also works)
  --once           Print once and exit (no refresh)
  -nc, --no-color  Disable ANSI color
  --version, -V    Print lsStack version
  -h, --help       This help
"""


def main(args: list[str] | None = None) -> None:
    from lsstack.color import color_enabled

    args = args if args is not None else sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(_help_text())
        return

    once = "--once" in args
    if once:
        args = [a for a in args if a != "--once"]

    args, watch_sec = parse_watch_args(args)
    if once and watch_sec is not None:
        print("Error: --once cannot be used with -w/--watch", file=sys.stderr)
        sys.exit(2)

    use_color = color_enabled(args)
    rest = [a for a in args if a not in ("-nc", "--no-color", "--nocolor")]
    if rest:
        print("Usage: lswatch [-w N] [--once] [-nc, --no-color]", file=sys.stderr)
        print("       lswatch --help", file=sys.stderr)
        sys.exit(2)

    get_power_info()
    if once:
        time.sleep(_RAPL_PRIME_SEC)
        print_combined(color=use_color)
        return

    interval = watch_sec if watch_sec is not None else DEFAULT_WATCH_SEC
    run_watch(
        lambda: print_combined(color=use_color),
        interval,
        title="lswatch",
    )


if __name__ == "__main__":
    main()
