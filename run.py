"""One Bureau, eight agents. Only the Orchestrator has a mailbox; every other hop is local.

    uv sync
    LOCAL_ONLY=1 uv run python run.py        # dev seeds, no mailbox (Codex machines, CI)
    uv run python run.py                     # real seeds from .env, Orchestrator on the Agentverse mailbox

The submission runs the same agents as Agentverse hosted agents instead (deploy/hosted/, docs/HOSTED.md).
"""

from __future__ import annotations

import logging

from uagents import Agent, Bureau

from core import config
from core.registry import get_registry

log = logging.getLogger("canary.run")


def build_agents() -> list[Agent]:
    from agents.judge import build_judge
    from agents.orchestrator import build_orchestrator
    from agents.probe import build_probe
    from agents.target import build_target

    get_registry()  # create the DB and pre-allow the demo targets
    agents = [build_orchestrator(), build_judge()]
    agents += [build_probe(kind) for kind in config.PROBE_KINDS]
    agents += [build_target(key) for key in config.DEMO_TARGETS]
    return agents


def print_addresses() -> None:
    """`python run.py --addresses`: name and address of every agent, for the README and Agentverse."""
    for spec in config.AGENTS.values():
        print(f"{spec.name:<26} {config.agent_address(spec.key)}")


def main() -> None:
    import sys

    if "--addresses" in sys.argv:
        print_addresses()
        return
    logging.basicConfig(level=logging.INFO)
    agents = build_agents()
    bureau = Bureau(port=config.AGENTS["orchestrator"].port, endpoint=[f"http://127.0.0.1:{config.AGENTS['orchestrator'].port}/submit"])
    for a in agents:
        bureau.add(a)
    log.info("Bureau up: %s", ", ".join(f"{a.name}={a.address}" for a in agents))
    bureau.run()


if __name__ == "__main__":
    main()
