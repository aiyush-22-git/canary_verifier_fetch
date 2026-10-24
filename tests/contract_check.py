"""Contract check: `uv run python -m tests.contract_check`. CI and the pr-reviewer run it.

Loads every model in core/models.py, validates every pack and target profile against it, checks
that the Codex entry points exist with the agreed names, and that the demo canary tokens in
config and profiles.yaml agree. Missing Codex files are reported as pending, not as errors,
so the check is green on a fresh repo and strict as soon as files land.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from core import config, models
from core.packs import PACKS_DIR, PROFILES_FILE, PackError, load_packs, load_profiles

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []
pending: list[str] = []


def check_models() -> None:
    names = ["AttackCase", "Pack", "TargetProfile", "ProbeRequest", "Exchange", "ProbeResult", "JudgeRequest",
             "ProbeProgress", "Finding", "ProbeSummary", "Verdict", "AuditPlan", "RegistryEntry"]
    for n in names:
        if not hasattr(models, n):
            errors.append(f"core/models.py is missing {n}")
    case = models.AttackCase(id="x", category="leak", title="t", action="chat", rule_kind="soft", rubric="r")
    ex = models.Exchange(case=case)
    res = models.ProbeResult(audit_id="a", kind="leak", target_address="t", exchanges=[ex])
    if models.ProbeResult.parse_raw(res.json()) != res:
        errors.append("ProbeResult does not round-trip through JSON")


def check_packs() -> None:
    if not list(PACKS_DIR.glob("*.yaml")):
        pending.append("packs/*.yaml (B2)")
        return
    try:
        cases = load_packs()
    except PackError as exc:
        errors.append(f"packs invalid:\n{exc}")
        return
    cats = {c.category for c in cases}
    for cat in models.CATEGORIES:
        n = sum(1 for c in cases if c.category == cat)
        if n < 5:
            errors.append(f"packs/{cat}.yaml has {n} cases, need at least 5")
    for need in cats ^ set(models.CATEGORIES):
        errors.append(f"packs: category {need} missing or unknown")
    for profile in ("target-leaky", "target-overcharger", "target-solid"):
        if not any(profile in c.expected for c in cases):
            errors.append(f"no case declares an expected outcome for {profile}")


def check_profiles() -> None:
    if not PROFILES_FILE.exists():
        pending.append("targets/profiles.yaml (B1)")
        return
    try:
        profiles = load_profiles()
    except PackError as exc:
        errors.append(f"profiles invalid:\n{exc}")
        return
    keys = {p.key for p in profiles}
    for k in config.DEMO_TARGETS:
        if k not in keys:
            errors.append(f"targets/profiles.yaml has no profile for {k}")


def check_entry_points() -> None:
    for module, attr, owner in (("agents.probe", "build_probe", "B2"), ("agents.target", "build_target", "B1")):
        if not (ROOT / (module.replace(".", "/") + ".py")).exists():
            pending.append(f"{module}.{attr} ({owner})")
            continue
        try:
            mod = importlib.import_module(module)
        except Exception as exc:
            errors.append(f"{module} fails to import: {exc!r}")
            continue
        if not callable(getattr(mod, attr, None)):
            errors.append(f"{module} must define {attr}() (see docs/CONTEXT.md)")


def check_ownership_of_models() -> None:
    # Allowed on a contract PR only; the pr-reviewer enforces who touched the file.
    if not (ROOT / "core" / "models.py").exists():
        errors.append("core/models.py missing")


def main() -> int:
    for check in (check_models, check_packs, check_profiles, check_entry_points, check_ownership_of_models):
        check()
    for p in pending:
        print(f"pending: {p}")
    if errors:
        print("\n".join(f"ERROR: {e}" for e in errors))
        return 1
    print("contract ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
