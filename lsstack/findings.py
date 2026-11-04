"""Findings catalog: load what/why/prompt templates; detection stays in callers."""

from __future__ import annotations

from string import Formatter

from lsstack.common import load_json

_VERDICT_TO_ID = {
    "gen_mismatch": "pcie.gen_mismatch",
    "trained_low": "pcie.trained_low",
    "width_limited": "pcie.width_limited",
    "gpu_headroom": "pcie.gpu_headroom",
    "oversub": "pcie.oversub",
    "root_gen_low": "pcie.root_gen_low",
    "ok": "pcie.link_ok",
}

_ROOT_VERDICT_TO_ID = {
    "oversub": "pcie.oversub",
    "gpu_headroom": "pcie.gpu_headroom",
    "root_gen_low": "pcie.root_gen_low",
    "ok": "pcie.root_ok",
}

_catalog: dict | None = None


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format(template: str, ctx: dict) -> str:
    if not template:
        return ""
    try:
        return Formatter().vformat(template, (), _SafeFormat(ctx))
    except Exception:
        return template


def load_catalog(force: bool = False) -> dict:
    global _catalog
    if _catalog is not None and not force:
        return _catalog
    raw = load_json("findings.json") or {}
    findings = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(findings, dict):
        findings = {}
    _catalog = findings
    return _catalog


def finding_id_for_verdict(verdict: str | None, *, root: bool = False) -> str | None:
    if not verdict:
        return None
    table = _ROOT_VERDICT_TO_ID if root else _VERDICT_TO_ID
    return table.get(verdict)


def get_finding(fid: str | None) -> dict | None:
    if not fid:
        return None
    return load_catalog().get(fid)


def render(fid: str | None, **ctx) -> dict | None:
    """Return filled finding card, or None if id missing from catalog."""
    base = get_finding(fid)
    if not base:
        return None
    return {
        "id": base.get("id") or fid,
        "subsystem": base.get("subsystem"),
        "severity": base.get("default_severity") or "info",
        "title": _format(base.get("title") or "", ctx),
        "what": _format(base.get("what") or "", ctx),
        "why": _format(base.get("why") or "", ctx),
        "prompt": _format(base.get("prompt") or "", ctx),
        "related_commands": list(base.get("related_commands") or []),
        "sources": list(base.get("sources") or []),
    }


def render_verdict(verdict: str | None, *, root: bool = False, **ctx) -> dict | None:
    return render(finding_id_for_verdict(verdict, root=root), **ctx)
