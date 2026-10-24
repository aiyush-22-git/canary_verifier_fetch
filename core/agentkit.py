"""Builds uAgents the same way everywhere, so agent files only contain handlers."""

from __future__ import annotations

from pathlib import Path

from uagents import Agent

from core import config

AGENTVERSE_URL = "https://agentverse.ai"
ROOT = Path(__file__).resolve().parent.parent


def make_agent(key: str, *, mailbox: bool = False, description: str = "", readme_path: str | None = None) -> Agent:
    """Agent for a `config.AGENTS` key. Only the Orchestrator asks for a mailbox; everything
    else is reached by local Bureau delivery. LOCAL_ONLY never opens a mailbox."""
    spec = config.AGENTS[key]
    if readme_path:
        readme_path = str(ROOT / readme_path)
    kwargs: dict = {
        "name": spec.name,
        "port": spec.port,
        "seed": config.agent_seed(key),
        "network": config.NETWORK,
        "description": description or spec.name,
        "readme_path": readme_path,
        "enable_agent_inspector": False,
        # Without this an agent handles one message at a time, so a probe waiting for a target's
        # reply inside its ProbeRequest handler would block the very reply it is waiting for.
        "handle_messages_concurrently": True,
    }
    if config.mailbox_enabled():
        kwargs["agentverse"] = AGENTVERSE_URL
        if mailbox:
            kwargs["mailbox"] = True
    else:
        kwargs["endpoint"] = [f"http://127.0.0.1:{spec.port}/submit"]
    return Agent(**kwargs)
