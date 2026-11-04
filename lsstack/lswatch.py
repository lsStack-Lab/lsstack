"""lswatch: combined thermal + power live view (refresh default on)."""

from __future__ import annotations

import sys

from lsstack.power import print_power_human
from lsstack.thermal import print_thermal_human
from lsstack.watchutil import DEFAULT_WATCH_SEC, parse_watch_args, run_watch


def print_combined(*, watching: bool = True, color: bool = False) -> None:
    print_thermal_human(watching=True, heading=True, color=color)
    print_power_human(watching=True, heading=True, color=color)
    if not watching:
        # one-shot tips
        print("  Notes")
        print(
            f"  {'Note':>14}  watch default: lswatch "
            f"(clears screen, every {DEFAULT_WATCH_SEC:g}s; -w N to set interval)"
        )
        print(f"  {'Note':>14}  one-shot: lswatch --once")
        print()


def main(args: list[str] | None = None) -> None:
    from lsstack.color import color_enabled

    args = args if args is not None else sys.argv[1:]
    once = "--once" in args
    if once:
        args = [a for a in args if a != "--once"]

    args, watch_sec = parse_watch_args(args)
    use_color = color_enabled(args)
    # leftover flags besides color knobs
    rest = [a for a in args if a not in ("-nc", "--no-color", "--nocolor")]
    if rest:
        print(f"Usage: lswatch [-w N] [--once] [-nc, --no-color]", file=sys.stderr)
        sys.exit(2)

    if once:
        print_combined(watching=False, color=use_color)
        return

    interval = watch_sec if watch_sec is not None else DEFAULT_WATCH_SEC
    run_watch(
        lambda: print_combined(watching=True, color=use_color),
        interval,
        title="lswatch",
    )


if __name__ == "__main__":
    main()
