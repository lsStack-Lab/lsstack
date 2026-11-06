"""Optional ANSI color for human ls* output (TTY only; educational attention cues)."""

from __future__ import annotations

import os
import sys

# Verdict / status → style
_OK = {"ok", "pass"}
_WARN = {
    "gpu_headroom",
    "trained_low",
    "width_limited",
    "root_gen_low",
    "warn",
}
_BAD = {
    "gen_mismatch",
    "oversub",
    "fail",
    "critical",
}
_NA = {"na"}


def color_enabled(args: list[str] | None = None) -> bool:
    """True when we may emit ANSI. Honors NO_COLOR, LSSTACK_COLOR, -nc, --no-color, TTY."""
    args = args or []
    if "-nc" in args or "--no-color" in args or "--nocolor" in args:
        return False
    env = (os.environ.get("LSSTACK_COLOR") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("NO_COLOR") is not None and os.environ.get("NO_COLOR") != "":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _wrap(code: str, text: str, *, enabled: bool) -> str:
    if not enabled or not text:
        return text
    return f"\033[{code}m{text}\033[0m"


def muted(text: str, *, enabled: bool) -> str:
    return _wrap("2", text, enabled=enabled)


def green(text: str, *, enabled: bool) -> str:
    return _wrap("32", text, enabled=enabled)


def yellow(text: str, *, enabled: bool) -> str:
    return _wrap("33", text, enabled=enabled)


def red(text: str, *, enabled: bool) -> str:
    return _wrap("31", text, enabled=enabled)


def verdict_bracket(verdict: str | None, *, enabled: bool) -> str:
    """Color only the [verdict] token. [na] is skip, not a warning."""
    v = (verdict or "?").strip()
    token = f"[{v}]"
    key = v.lower()
    if key in _OK:
        return green(token, enabled=enabled)
    if key in _BAD:
        return red(token, enabled=enabled)
    if key in _NA:
        return muted(token, enabled=enabled)
    if key in _WARN:
        return yellow(token, enabled=enabled)
    return yellow(token, enabled=enabled)


def headroom_status(ok: bool, text: str, *, enabled: bool) -> str:
    """Color a short headroom status phrase (OK vs undersized)."""
    if ok:
        return green(text, enabled=enabled)
    return red(text, enabled=enabled)


def attention_note(text: str, *, enabled: bool) -> str:
    """Mild emphasis for 'N need attention' style summary lines."""
    return yellow(text, enabled=enabled)


def privilege_note(text: str, *, enabled: bool) -> str:
    """Amber cue for partial output: detail exists but needs sudo/root.

    Not red (that means broken/fail) and not purple (poor CLI contrast / nonstandard).
    Yellow/amber = incomplete privilege, re-run elevated for that field only.
    """
    return yellow(text, enabled=enabled)


def looks_like_privilege_note(text: str | None) -> bool:
    """Heuristic for notes that should use privilege_note coloring."""
    if not text:
        return False
    t = text.lower()
    return any(
        s in t
        for s in (
            "needs sudo",
            "need sudo",
            "needs root",
            "need root",
            "without sudo",
            "without root",
            "run with sudo",
            "run as root",
            "must be run as root",
        )
    )
