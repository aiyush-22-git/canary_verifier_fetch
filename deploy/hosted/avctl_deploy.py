"""Create and update the 8 hosted agents on Agentverse with avctl (instead of pasting each file by hand).

    avctl auth login                       # once (browser)
    uv run python deploy/hosted/avctl_deploy.py            # all 8: the 7 first, then the Orchestrator with their addresses
    uv run python deploy/hosted/avctl_deploy.py --only judge

Each agent gets its own project folder outside the repo (~/canary_work/canary-hosted/<key>, default) because the generated agent.py
holds your API keys. `avctl hosting deploy` creates the agent if it does not exist, otherwise uploads and restarts it, so
re-running this is the redeploy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build  # noqa: E402

from core import config  # noqa: E402

AVCTL = os.environ.get("AVCTL", str(Path.home() / ".local" / "bin" / "avctl"))
WORK = Path(os.environ.get("CANARY_HOSTED_DIR", Path.home() / "canary_work" / "canary-hosted"))
ADDRESSES = ROOT / "deploy" / "hosted" / "addresses.json"
PYPROJECT = '[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = "{desc}"\nreadme = "README.md"\nrequires-python = ">=3.10,<3.13"\ndependencies = [\n    "uagents>=0.25.5",\n    "uagents-core",\n    "openai",\n]\n'


def av(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([AVCTL, *args], cwd=cwd, capture_output=True, text=True, timeout=180)


def known_addresses() -> dict[str, str]:
    """{agent display name: address} for everything on the account."""
    out = av("hosting", "get", "agents").stdout
    found = {}
    for line in out.splitlines()[1:]:
        m = re.search(r"(agent1q[0-9a-z]+)", line)
        if m:
            found[line[: m.start()].split("  ")[0].strip()] = m.group(1)
    return found


def deploy_one(key: str) -> bool:
    spec = type("Spec", (), {"name": build.agent_name(key)})
    src = build.DIST / key
    folder = WORK / key
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / ".avctl").exists():
        av("hosting", "init", cwd=folder)
    shutil.copyfile(src / "agent.py", folder / "agent.py")
    readme = src / "README.md"
    (folder / "README.md").write_text(readme.read_text() if readme.is_file() else f"# {spec.name}\n\nPart of Canary, a trust auditor for Agentverse agents.\n")
    (folder / "pyproject.toml").write_text(PYPROJECT.format(name=key, desc=f"Canary: {spec.name}"))
    res = av("hosting", "deploy", "--name", spec.name, "--no-dependency-check", "-f", "agent.py", cwd=folder)
    ok = res.returncode == 0
    tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
    print(f"[{'ok' if ok else 'FAIL'}] {spec.name:<26} {' | '.join(tail)[:200]}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="deploy just this agent key")
    args = ap.parse_args()
    if "logged-in" not in av("auth", "status").stdout:
        print("Not signed in: run `avctl auth login` first.")
        return 1
    others = [k for k in build.ENTRY if k not in ("orchestrator", "tester")]
    keys = [args.only] if args.only else others + ["orchestrator"]
    env = build.read_env()
    if "orchestrator" in keys:  # the rail comes from .env; dodo without its keys stops here, before any deploy
        print(f"Orchestrator payment rail: {build.payment_rail(env)}")
    env["TESTER_VERSION"] = str(int(time.time()))  # a new tester build runs its script again
    env["TESTER_SCRIPT"] = os.environ.get("TESTER_SCRIPT", "")  # "quick": hello, lookup and one audit only
    build.DIST.mkdir(parents=True, exist_ok=True)
    failed = 0
    for key in keys:
        if key == "tester":  # the load tester only needs the Orchestrator's address
            orch = known_addresses().get("Canary-Orchestrator")
            if not orch:
                print("Tester not deployed: Canary-Orchestrator is not on the account yet")
                return 1
            addresses = {"orchestrator": orch}
        elif key == "orchestrator":
            names = known_addresses()
            addresses = {k: names[config.AGENTS[k].name] for k in others if config.AGENTS[k].name in names}
            absent = [k for k in others if k not in addresses]
            if absent:
                print(f"Orchestrator not deployed: no address yet for {', '.join(absent)}")
                failed += 1
                continue
            ADDRESSES.write_text(json.dumps(addresses, indent=1))
        else:
            addresses = json.loads(ADDRESSES.read_text()) if ADDRESSES.is_file() else {}
        code, _ = build.build_agent(key, addresses, env)
        (build.DIST / key).mkdir(exist_ok=True)
        (build.DIST / key / "agent.py").write_text(code, encoding="utf-8")
        failed += not deploy_one(key)
    print("\n" + ("All deployed." if not failed else f"{failed} step(s) failed: see above."))
    for name, addr in known_addresses().items():
        print(f"  {name:<26} {addr}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
