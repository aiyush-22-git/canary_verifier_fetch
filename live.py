"""Live-run helper. Everything between a fresh clone and "ASI:One can reach the Orchestrator".

    uv run python live.py init-env        # write .env from .env.example with fresh random seeds (never overwrites)
    uv run python live.py check           # preflight: keys, seeds, asi1 ping, Agentverse reachability, payment rail
    uv run python live.py register        # register all 8 agents (profile, readme, protocols) with your Agentverse API key
    uv run python live.py register --dry-run
    uv run python live.py addresses       # name, address and Agentverse profile URL for the README

Then `uv run python run.py` (real seeds, Orchestrator on the mailbox) and `uv run pytest -m live`.
Full runbook: docs/LIVE.md.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import stat
import sys
from pathlib import Path

import httpx

from core import config

ROOT = Path(__file__).resolve().parent
PROFILE_URL = "https://agentverse.ai/agents/details/{address}/profile"
OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def env_path() -> Path:
    """The repo-root .env. Inside a worktree that is the main checkout (the first ancestor whose `.git` is a
    directory), so there is one .env no matter where you run from; config.py finds it by searching upward."""
    for folder in (ROOT, *ROOT.parents):
        if (folder / ".git").is_dir():
            return folder / ".env"
    return ROOT / ".env"


def init_env() -> int:
    target, example = env_path(), ROOT / ".env.example"
    if target.exists():
        print(f"{target} already exists: leaving it alone (delete it first if you really want fresh seeds).")
        return 1
    text = example.read_text()
    text = re.sub(r"^LOCAL_ONLY=.*$", "LOCAL_ONLY=0", text, flags=re.M)
    text = re.sub(r"^(SEED_[A-Z_]+)=$", lambda m: f"{m.group(1)}=canary-{secrets.token_hex(16)}", text, flags=re.M)
    target.write_text(text)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Wrote {target} with fresh seeds for all 8 agents (git-ignored, mode 600).")
    print("Now add ASI1_API_KEY and AGENTVERSE_API_KEY (and BACKBOARD_API_KEY once targets land), then: uv run python live.py check")
    print("Back the seeds up: losing one means losing that agent's Agentverse address.")
    return 0


def _row(level: str, what: str, detail: str = "") -> None:
    print(f"[{level}] {what}" + (f": {detail}" if detail else ""))


async def _ping_asi1() -> str | None:
    from core import llm

    reply = await llm.complete([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=8)
    return reply


def _reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=8, follow_redirects=True).status_code < 500
    except Exception:
        return False


def check() -> int:
    blocking = 0

    def need(cond: bool, what: str, ok_detail: str = "", fail_detail: str = "") -> bool:
        nonlocal blocking
        if cond:
            _row(OK, what, ok_detail)
        else:
            blocking += 1
            _row(FAIL, what, fail_detail)
        return cond

    if config.LOCAL_ONLY:
        _row(WARN, "LOCAL_ONLY=1", "this is the dev mode: no mailbox, dev seeds. Set LOCAL_ONLY=0 in .env for the live run.")

    seeds = {k: config._env("SEED_" + k.upper().replace("-", "_")) for k in config.AGENTS}
    missing = [k for k, v in seeds.items() if not v]
    need(not missing or config.LOCAL_ONLY, "agent seeds", "all 8 set" if not missing else "", f"missing: {', '.join(missing)} (run init-env)")
    need(len({v for v in seeds.values() if v}) == len([v for v in seeds.values() if v]), "seeds are distinct",
         "", "two agents share a seed, so they would share an address")

    if need(bool(config.ASI1_API_KEY), "ASI1_API_KEY", "set", "missing: asi1 runs intent, the planner and the Judge's soft grading"):
        reply = asyncio.run(_ping_asi1())
        need(bool(reply), "asi1 reachable", f"replied {reply!r}" if reply else "", "the key is set but https://api.asi1.ai/v1 did not answer (bad key, quota, or network)")

    need(bool(config.AGENTVERSE_API_KEY), "AGENTVERSE_API_KEY", "set", "missing: needed by `live.py register`")
    need(_reachable("https://agentverse.ai"), "agentverse.ai reachable")
    if not config.BACKBOARD_API_KEY:
        _row(WARN, "BACKBOARD_API_KEY", "not set: only the Codex target agents need it; stubs run without it")

    rail = config.PAYMENT_RAIL
    if rail == "dodo":
        have = bool(config.DODO_API_KEY and config.DODO_PRODUCT_ID)
        if need(have, "Dodo Payments", f"{config.DODO_ENV}, product {config.DODO_PRODUCT_ID}",
                "set DODO_PAYMENTS_API_KEY and DODO_PRODUCT_ID (create the product in the Dodo dashboard first)"):
            from core import payments

            try:
                product = payments._dodo_http("GET", f"/products/{config.DODO_PRODUCT_ID}")
                price = product.get("price_detail", {}).get("price") or product.get("price")
                _row(OK, "Dodo product reachable", f"{product.get('name', '?')}, price {price} (minor units); must be >= {config.AUDIT_PRICE_USD} USD")
            except Exception as exc:
                need(False, "Dodo product reachable", "", f"{type(exc).__name__}: wrong key, wrong DODO_ENV, or unknown product id")
    elif rail == "stripe":
        need(bool(config.STRIPE_SECRET_KEY and config.STRIPE_PUBLISHABLE_KEY), "Stripe test keys", "set", "STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY missing")
    elif rail == "fet":
        _row(OK, "payment rail", f"FET {'stable-testnet' if config.FET_USE_TESTNET else 'mainnet'}, price {config.AUDIT_PRICE_FET} FET")
        if not missing or config.LOCAL_ONLY:
            from core.agentkit import make_agent

            _row(OK, "seller wallet", f"{make_agent('orchestrator').wallet.address()} (receives audit payments; testnet funds come from the faucet)")
    else:
        _row(WARN, "payment rail", f"PAYMENT_RAIL={rail!r}: paid audits will show 'payments aren't available' (never faked)")

    print()
    if blocking:
        print(f"{blocking} blocking problem(s). Fix them, then re-run: uv run python live.py check")
        return 1
    print("Preflight passed. Next: uv run python live.py register   then   uv run python run.py")
    return 0


def _readme_for(key: str, name: str) -> str:
    candidates = {
        "orchestrator": "orchestrator.md", "judge": "judge.md",
    }
    path = ROOT / "docs" / "agentverse" / candidates.get(key, f"{key}.md")
    if path.exists():
        return path.read_text()
    return f"# {name}\n\nPart of Canary, a paid trust audit for Agentverse agents. Ask Canary-Orchestrator: \"Is ShopBot-Solid safe to pay?\""


def register(dry_run: bool = False) -> int:
    from uagents_core.config import AgentverseConfig
    from uagents_core.contrib.protocols.chat import chat_protocol_spec
    from uagents_core.contrib.protocols.payment import payment_protocol_spec
    from uagents_core.protocol import ProtocolSpecification
    from uagents_core.utils.registration import (
        AgentverseRegistrationRequest,
        AgentverseRequestError,
        RegistrationRequestCredentials,
        register_agent,
    )

    if not dry_run and not config.AGENTVERSE_API_KEY:
        print("AGENTVERSE_API_KEY is not set.")
        return 1
    chat = ProtocolSpecification.compute_digest(chat_protocol_spec.manifest())
    pay = ProtocolSpecification.compute_digest(payment_protocol_spec.manifest())
    protocols = {"orchestrator": [chat, pay], "judge": [], "probe": [chat], "target": [chat, pay]}
    cfg = AgentverseConfig()
    failures = 0
    for key, spec in config.AGENTS.items():
        req = AgentverseRegistrationRequest(
            name=spec.name,
            endpoint=cfg.mailbox_endpoint,
            protocols=protocols[spec.role],
            type="mailbox",
            description=f"Canary: {spec.role} agent. Paid trust audits for Agentverse agents.",
            readme=_readme_for(key, spec.name),
            active=True,
            metadata={"categories": ["security", "trust"], "is_public": "True"},
        )
        address = config.agent_address(key)
        if dry_run:
            print(f"[dry-run] would register {spec.name} {address} protocols={len(req.protocols)} readme={len(req.readme or '')} chars")
            continue
        creds = RegistrationRequestCredentials(agentverse_api_key=config.AGENTVERSE_API_KEY, agent_seed_phrase=config.agent_seed(key))
        try:
            register_agent(req, cfg, creds)
            print(f"[ok]   {spec.name:<26} {PROFILE_URL.format(address=address)}")
        except AgentverseRequestError as exc:
            failures += 1
            print(f"[FAIL] {spec.name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {spec.name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


def addresses() -> int:
    for spec in config.AGENTS.values():
        address = config.agent_address(spec.key)
        print(f"{spec.name:<26} {address}  {PROFILE_URL.format(address=address)}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "init-env":
        return init_env()
    if cmd == "check":
        return check()
    if cmd == "register":
        return register(dry_run="--dry-run" in sys.argv)
    if cmd == "addresses":
        return addresses()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
