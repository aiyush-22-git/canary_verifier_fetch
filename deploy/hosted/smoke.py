"""Run each generated bundle the way Agentverse would, without the repo: `python -I deploy/hosted/smoke.py <agent.py>`.

Checks the file is self-contained (imports only its embedded modules), executes against a pre-made `agent` like the
hosted runtime provides, and reports which handlers got registered. Run from any directory; the repo is NOT on sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(path: str) -> int:
    from uagents import Agent

    agent = Agent(name="smoke", seed="smoke-seed-" + Path(path).parent.name, port=18999, enable_agent_inspector=False)
    ns: dict = {"__name__": "__hosted__", "agent": agent}
    code = Path(path).read_text(encoding="utf-8")
    exec(compile(code, path, "exec"), ns)
    own = agent._protocol  # @agent.on_message handlers live on the agent's default protocol
    handlers = len(own._unsigned_message_handlers) + len(own._signed_message_handlers)
    for proto in agent.protocols.values():
        handlers += len(proto._unsigned_message_handlers) + len(proto._signed_message_handlers)
    intervals = len(agent._interval_handlers) + len(own._interval_handlers)
    print(f"ok  {Path(path).parent.name:<20} message handlers={handlers} intervals={intervals} protocols={len(agent.protocols)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
