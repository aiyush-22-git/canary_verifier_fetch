"""One Bureau, eight agents. Only the Orchestrator has a mailbox; every other hop is local.

    uv sync
    LOCAL_ONLY=1 uv run python run.py        # dev seeds, no mailbox (Codex machines, CI)
    uv run python run.py                     # real seeds from .env, Orchestrator on the Agentverse mailbox

Probes and targets come from agents/probe.py and agents/target.py once those PRs land
(`build_probe(kind)`, `build_target(key)`); until then the dev stubs from core/stubs.py are used.
"""

from __future__ import annotations

import importlib
import logging

from uagents import Agent, Bureau

from core import config
from core.registry import get_registry

log = logging.getLogger("canary.run")


def _factory(module: str, attr: str, fallback):
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError):
        log.warning("%s.%s not found: using the dev stub", module, attr)
        return fallback


def build_agents() -> list[Agent]:
    from agents.judge import build_judge
    from agents.orchestrator import build_orchestrator
    from core.stubs import build_stub_probe, build_stub_target

    get_registry()  # create the DB and pre-allow the demo targets
    build_probe = _factory("agents.probe", "build_probe", build_stub_probe)
    build_target = _factory("agents.target", "build_target", build_stub_target)
    agents = [build_orchestrator(), build_judge()]
    agents += [build_probe(kind) for kind in config.PROBE_KINDS]
    agents += [build_target(key) for key in config.DEMO_TARGETS]
    return agents


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    agents = build_agents()
    bureau = Bureau(port=config.AGENTS["orchestrator"].port, endpoint=[f"http://127.0.0.1:{config.AGENTS['orchestrator'].port}/submit"])
    for a in agents:
        bureau.add(a)
    log.info("Bureau up: %s", ", ".join(f"{a.name}={a.address}" for a in agents))
    bureau.run()


if __name__ == "__main__":
    main()
