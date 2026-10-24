"""Loads attack packs and target profiles from YAML and validates them against core/models.py.

`load_packs()` raises on the first invalid row: contract_check uses that. `load_cases()` is the
runtime entry point; only a LOCAL_ONLY dev Bureau falls back to the dev stub pack when packs/ is
empty or invalid. Hosted and mailbox agents never run stub tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from core import config
from core.models import ACTIONS, CATEGORIES, RULE_KINDS, SEVERITIES, AttackCase, Pack, TargetProfile

log = logging.getLogger("canary.packs")

try:  # Agentverse hosted bundle: deploy/hosted/build.py embeds packs/*.yaml and targets/profiles.yaml here
    import bundle_data
except ImportError:
    bundle_data = None

ROOT = Path(globals().get("__file__") or "/hosted/core_packs.py").resolve().parent.parent
PACKS_DIR = ROOT / "packs"
PROFILES_FILE = ROOT / "targets" / "profiles.yaml"


class PackError(ValueError):
    pass


def validate_case(case: AttackCase) -> list[str]:
    errs: list[str] = []
    if case.category not in CATEGORIES:
        errs.append(f"{case.id}: category {case.category!r} not in {CATEGORIES}")
    if case.action not in ACTIONS:
        errs.append(f"{case.id}: action {case.action!r} not in {ACTIONS}")
    if case.rule_kind not in RULE_KINDS:
        errs.append(f"{case.id}: rule_kind {case.rule_kind!r} not in {RULE_KINDS}")
    if case.severity not in SEVERITIES:
        errs.append(f"{case.id}: severity {case.severity!r} not in {SEVERITIES}")
    if case.rule_kind == "soft" and not case.rubric:
        errs.append(f"{case.id}: soft cases need a rubric")
    if case.rule_kind == "soft" and case.hard:
        errs.append(f"{case.id}: soft cases can't be hard (hard fails are exact-match rules only)")
    if case.rule_kind in ("text_match", "event_match", "amount_below") and not case.rule_arg:
        errs.append(f"{case.id}: {case.rule_kind} needs rule_arg")
    if case.action == "raw_json" and not case.raw_json:
        errs.append(f"{case.id}: raw_json action needs raw_json")
    if case.action in ("chat", "raw_json", "tool_injection") and not case.messages:
        errs.append(f"{case.id}: {case.action} needs messages")
    for profile, want in case.expected.items():
        if want not in ("pass", "fail"):
            errs.append(f"{case.id}: expected[{profile}] must be pass|fail")
    return errs


def load_packs(directory: Path | None = None) -> list[AttackCase]:
    """Every case from packs/*.yaml, validated. Raises PackError listing every problem."""
    if directory is None and bundle_data is not None:
        sources = sorted(bundle_data.PACKS.items())
    else:
        sources = [(p.name, p.read_text()) for p in sorted((directory or PACKS_DIR).glob("*.yaml"))]
    cases: list[AttackCase] = []
    errs: list[str] = []
    for name, text in sources:
        try:
            raw = yaml.safe_load(text) or {}
            pack = Pack(**raw)
        except Exception as exc:
            errs.append(f"{name}: {exc}")
            continue
        for case in pack.cases:
            if case.category != pack.category:
                errs.append(f"{name}: {case.id} category {case.category!r} != file category {pack.category!r}")
            errs += [f"{name}: {e}" for e in validate_case(case)]
            cases.append(case)
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            errs.append(f"duplicate case id {c.id}")
        seen.add(c.id)
    if errs:
        raise PackError("\n".join(errs))
    return cases


def load_profiles(path: Path | None = None) -> list[TargetProfile]:
    text = bundle_data.PROFILES if path is None and bundle_data is not None else (path or PROFILES_FILE).read_text()
    raw = yaml.safe_load(text) or {}
    profiles = [TargetProfile(**p) for p in raw.get("profiles", [])]
    errs: list[str] = []
    for p in profiles:
        demo = config.DEMO_TARGETS.get(p.key)
        if demo is None:
            errs.append(f"profile {p.key}: not a key in core.config.DEMO_TARGETS")
            continue
        if p.canary != demo.canary:
            errs.append(f"profile {p.key}: canary {p.canary!r} != config {demo.canary!r}")
        if p.canary not in p.system_prompt:
            errs.append(f"profile {p.key}: system_prompt does not contain its canary token")
    if errs:
        raise PackError("\n".join(errs))
    return profiles


def load_cases() -> list[AttackCase]:
    """Runtime: the real packs if present and valid, otherwise the dev stub pack."""
    # Only a LOCAL_ONLY dev Bureau (unreachable from ASI:One) may fall back to the stub pack. A hosted bundle, or a
    # mailbox Bureau a real user can reach, fails loudly on a broken pack instead of grading anyone on stub tests.
    if config.HOSTED or bundle_data is not None or not config.LOCAL_ONLY:
        return load_packs()
    try:
        cases = load_packs()
    except PackError as exc:
        log.error("invalid packs, using stub pack:\n%s", exc)
        cases = []
    except OSError:
        cases = []
    if cases:
        return cases
    from core.stubs import stub_cases

    log.warning("no packs found in %s: using the dev stub pack", PACKS_DIR)
    return stub_cases()
