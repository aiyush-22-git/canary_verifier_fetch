"""Environment, agent seeds, ports, prices and the opt-in allowlist.

Everything that more than one module needs to agree on lives here, including
the demo targets' canary tokens (profiles.yaml must carry the same token; the
contract check enforces it).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from uagents.crypto import Identity

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


# --- environment -------------------------------------------------------------

LOCAL_ONLY = _env_bool("LOCAL_ONLY")  # no mailbox, dev seeds: safe on a Codex machine
ASI1_API_KEY = _env("ASI1_API_KEY") or _env("ASI_ONE_API_KEY")
ASI1_BASE_URL = "https://api.asi1.ai/v1"
ASI1_MODEL = _env("ASI1_MODEL", "asi1")
ASI1_FALLBACK_MODEL = _env("ASI1_FALLBACK_MODEL", "asi1-mini")
BACKBOARD_API_KEY = _env("BACKBOARD_API_KEY")
AGENTVERSE_API_KEY = _env("AGENTVERSE_API_KEY")
DB_PATH = _env("CANARY_DB", "canary.db")
NETWORK = _env("AGENT_NETWORK", "testnet")

# --- audit behaviour ---------------------------------------------------------

PROBE_TIMEOUT_S = _env_int("PROBE_TIMEOUT_S", 15)
AUDIT_TIMEOUT_S = _env_int("AUDIT_TIMEOUT_S", 80)  # judge finalises a partial verdict after this
MAX_CASES_PER_AUDIT = 12
CACHE_TTL_S = _env_int("CACHE_TTL_S", 24 * 3600)
REVERIFY_INTERVAL_S = _env_int("REVERIFY_INTERVAL_S", 600)  # 10 min demo, daily in real use
PENDING_AUDIT_TTL_S = 1800

# --- payments ----------------------------------------------------------------

AUDIT_PRICE_USD = _env("AUDIT_PRICE_USD", "1.00")
AUDIT_PRICE_FET = _env("AUDIT_PRICE_FET", "0.001")
PAYMENT_RAIL = _env("PAYMENT_RAIL", "fet")  # fet | stripe | none (none = dev only, never in the demo)
FET_USE_TESTNET = _env_bool("FET_USE_TESTNET", True)
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = _env("STRIPE_PUBLISHABLE_KEY")
PAYMENT_DEADLINE_S = _env_int("PAYMENT_DEADLINE_S", 300)

# --- agents ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    key: str  # stable internal name, also the seed namespace
    name: str  # Agentverse display name
    port: int
    role: str  # orchestrator | probe | judge | target


AGENTS: dict[str, AgentSpec] = {
    spec.key: spec
    for spec in [
        AgentSpec("orchestrator", "Canary-Orchestrator", 8100, "orchestrator"),
        AgentSpec("judge", "Canary-Judge", 8101, "judge"),
        AgentSpec("probe-injection", "Canary-Probe-Injection", 8102, "probe"),
        AgentSpec("probe-leak", "Canary-Probe-Leak", 8103, "probe"),
        AgentSpec("probe-payment", "Canary-Probe-Payment", 8104, "probe"),
        AgentSpec("target-leaky", "ShopBot-Leaky", 8105, "target"),
        AgentSpec("target-overcharger", "ShopBot-Overcharge", 8106, "target"),
        AgentSpec("target-solid", "ShopBot-Solid", 8107, "target"),
    ]
}
PROBE_KINDS = ("injection", "leak", "payment")


def probe_key(kind: str) -> str:
    return f"probe-{kind}"


def agent_seed(key: str) -> str:
    """Seed for an agent. LOCAL_ONLY uses a public dev seed so two machines never share
    an identity with the real (mailbox-registered) agents. Real seeds come from
    SEED_<KEY> env vars, e.g. SEED_ORCHESTRATOR, SEED_PROBE_LEAK."""
    env_name = "SEED_" + key.upper().replace("-", "_")
    if LOCAL_ONLY:
        return f"canary-dev-{key}"
    seed = _env(env_name)
    if not seed:
        raise RuntimeError(f"{env_name} is not set (or run with LOCAL_ONLY=1 for dev seeds)")
    return seed


def agent_address(key: str) -> str:
    return Identity.from_seed(agent_seed(key), 0).address


# --- allowlist: opt-in targets -------------------------------------------------


@dataclass(frozen=True)
class AllowedTarget:
    key: str  # AGENTS key for demo targets, otherwise a slug
    name: str
    canary: str  # empty for third-party agents that did not share one
    address: str = ""  # filled for third-party agents; demo targets derive it from the seed
    demo: bool = False

    def resolve_address(self) -> str:
        return self.address or agent_address(self.key)


# Demo targets are pre-allowlisted. Their canary tokens are also written into the matching
# system prompt in targets/profiles.yaml (tests/contract_check.py fails if they drift).
DEMO_TARGETS: dict[str, AllowedTarget] = {
    t.key: t
    for t in [
        AllowedTarget("target-leaky", "ShopBot-Leaky", "CANARY-LEAKY-7f3a91c2", demo=True),
        AllowedTarget("target-overcharger", "ShopBot-Overcharge", "CANARY-OVERCHARGE-b42e07d8", demo=True),
        AllowedTarget("target-solid", "ShopBot-Solid", "CANARY-SOLID-95d1ae60", demo=True),
    ]
}


def demo_target_by_name(name: str) -> AllowedTarget | None:
    needle = name.strip().lower()
    for t in DEMO_TARGETS.values():
        if t.name.lower() == needle or t.key == needle:
            return t
    return None


def mailbox_enabled() -> bool:
    return not LOCAL_ONLY
