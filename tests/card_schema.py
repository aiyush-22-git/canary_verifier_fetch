"""Validator for the documented ASI:One card schemas (predefined kinds + custom element tree).

Stands in for the Card Playground in CI; a card that passes here must still be pasted into
the Playground once by hand before the demo (docs/STATUS.md tracks that).
"""

from __future__ import annotations

from typing import Any

BADGE_VARIANTS = {"info", "success", "warning"}
INPUT_KINDS = {"text", "number", "email", "select", "checkbox"}
TEXT_STYLES = {"body", "muted", "emphasis"}


def _cta(c: Any, where: str, errs: list[str]) -> None:
    if not isinstance(c, dict) or not isinstance(c.get("label"), str) or not isinstance(c.get("selection"), dict):
        errs.append(f"{where}: CTA needs label and selection")


def _node(n: Any, depth: int, where: str, errs: list[str]) -> None:
    if depth > 8:
        errs.append(f"{where}: nesting deeper than 8")
        return
    if not isinstance(n, dict) or "type" not in n:
        errs.append(f"{where}: node without type")
        return
    t = n["type"]
    if t == "section":
        for i, k in enumerate(n.get("children", [])):
            _node(k, depth + 1, f"{where}/{i}", errs)
    elif t == "group":
        if n.get("direction") not in ("row", "column"):
            errs.append(f"{where}: group.direction")
        for i, k in enumerate(n.get("children", [])):
            _node(k, depth + 1, f"{where}/{i}", errs)
    elif t == "divider":
        pass
    elif t == "text":
        if not isinstance(n.get("value"), str) or n.get("style", "body") not in TEXT_STYLES:
            errs.append(f"{where}: text")
    elif t == "heading":
        if not isinstance(n.get("value"), str) or n.get("level", 2) not in (1, 2, 3):
            errs.append(f"{where}: heading")
    elif t == "badge":
        if not isinstance(n.get("label"), str) or n.get("variant", "info") not in BADGE_VARIANTS:
            errs.append(f"{where}: badge")
    elif t == "button":
        if not isinstance(n.get("label"), str) or not isinstance(n.get("action", {}).get("selection"), dict):
            errs.append(f"{where}: button")
    elif t == "input":
        if n.get("kind") not in INPUT_KINDS or not n.get("name") or not n.get("label"):
            errs.append(f"{where}: input")
        if n.get("kind") == "select" and not n.get("options"):
            errs.append(f"{where}: select needs options")
    elif t == "list":
        for i, item in enumerate(n.get("items", [])):
            for j, k in enumerate(item.get("children", [])):
                _node(k, depth + 2, f"{where}/{i}/{j}", errs)
            if "action" in item and not isinstance(item["action"].get("selection"), dict):
                errs.append(f"{where}/{i}: list action")
    elif t in ("image", "choice_grid"):
        pass
    else:
        errs.append(f"{where}: unknown type {t}")


def validate(kind: str, p: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if kind == "custom":
        if "root" not in p:
            return ["custom payload needs root"]
        _node(p["root"], 1, "root", errs)
    elif kind == "carousel":
        if not p.get("title") or not isinstance(p.get("items"), list):
            errs.append("carousel needs title and items")
        for i, it in enumerate(p.get("items", [])):
            if not it.get("id") or not it.get("title"):
                errs.append(f"items/{i}: id and title")
            _cta(it.get("primary_cta"), f"items/{i}", errs)
    elif kind == "detail":
        if not p.get("title"):
            errs.append("detail needs title")
        for i, r in enumerate(p.get("summary_rows", [])):
            if "label" not in r or "value" not in r:
                errs.append(f"summary_rows/{i}")
        if not p.get("ctas"):
            errs.append("detail needs ctas")
        for i, c in enumerate(p.get("ctas", [])):
            _cta(c, f"ctas/{i}", errs)
    elif kind == "form":
        if not p.get("fields"):
            errs.append("form needs fields")
        for i, f in enumerate(p.get("fields", [])):
            if f.get("kind") not in INPUT_KINDS or not f.get("name"):
                errs.append(f"fields/{i}")
            if f.get("kind") == "select" and not f.get("options"):
                errs.append(f"fields/{i}: select needs options")
        _cta(p.get("submit_cta"), "submit_cta", errs)
    elif kind == "review":
        if not p.get("title") or not p.get("summary_rows"):
            errs.append("review needs title and summary_rows")
        _cta(p.get("approve_cta"), "approve_cta", errs)
        _cta(p.get("reject_cta"), "reject_cta", errs)
    else:
        errs.append(f"unknown kind {kind}")
    return errs
