"""SQLite registry: agents + allowlist, audits, findings, pending audits and payment ledger.

One process, one file. The pending-audit and payment tables are the server-side state the
Orchestrator checks every card selection against: a selection alone never starts anything.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time

from core import config
from core.models import Finding, RegistryEntry, Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    address TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canary TEXT NOT NULL DEFAULT '',
    allowed INTEGER NOT NULL DEFAULT 0,
    demo INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    last_verified REAL NOT NULL DEFAULT 0,
    owner TEXT NOT NULL DEFAULT '',
    owner_code TEXT NOT NULL DEFAULT '',
    owner_code_expires REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audits (
    audit_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    target_name TEXT NOT NULL,
    requester TEXT NOT NULL,
    grade TEXT NOT NULL,
    score INTEGER NOT NULL,
    partial INTEGER NOT NULL,
    verdict_json TEXT NOT NULL,
    paid_tx TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audits_by_address ON audits(address, created_at DESC);
CREATE TABLE IF NOT EXISTS findings (
    audit_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    hard INTEGER NOT NULL,
    finding_json TEXT NOT NULL,
    PRIMARY KEY (audit_id, case_id)
);
CREATE TABLE IF NOT EXISTS pending (
    audit_id TEXT PRIMARY KEY,
    requester TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    state TEXT NOT NULL,            -- awaiting_payment | paid | running | done | cancelled
    created_at REAL NOT NULL,
    payment_ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
    tx_id TEXT PRIMARY KEY,         -- unique: a replayed transaction id is rejected
    requester TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    method TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Registry:
    def __init__(self, path: str | None = None) -> None:
        self._db = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- agents & allowlist --------------------------------------------------

    def seed_allowlist(self) -> None:
        """Pre-allow the demo targets. Idempotent."""
        for t in config.DEMO_TARGETS.values():
            self.allow_agent(t.resolve_address(), t.name, t.canary, demo=True)

    def allow_agent(self, address: str, name: str, canary: str = "", *, demo: bool = False, owner: str = "") -> None:
        self._run(
            "INSERT INTO agents(address,name,canary,allowed,demo,owner) VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(address) DO UPDATE SET name=excluded.name, allowed=1, "
            "canary=CASE WHEN excluded.canary!='' THEN excluded.canary ELSE canary END",
            (address, name, canary, int(demo), owner),
        )

    def find_agent(self, ref: str) -> sqlite3.Row | None:
        """Look up by address or case-insensitive name."""
        rows = self._all("SELECT * FROM agents WHERE address=? OR lower(name)=lower(?)", (ref.strip(), ref.strip()))
        return rows[0] if rows else None

    def is_allowed(self, address: str) -> bool:
        rows = self._all("SELECT allowed FROM agents WHERE address=?", (address,))
        return bool(rows and rows[0]["allowed"])

    def canary_for(self, address: str) -> str:
        rows = self._all("SELECT canary FROM agents WHERE address=?", (address,))
        return rows[0]["canary"] if rows else ""

    # --- owner verification ------------------------------------------------------

    def issue_owner_code(self, address: str, requester: str, name: str = "", ttl_s: int = 1800) -> str:
        code = "canary-verify-" + secrets.token_hex(4)
        self._run(
            "INSERT INTO agents(address,name,allowed,owner,owner_code,owner_code_expires) VALUES(?,?,0,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET owner=excluded.owner, owner_code=excluded.owner_code, "
            "owner_code_expires=excluded.owner_code_expires",
            (address, name or address[:14], requester, code, time.time() + ttl_s),
        )
        return code

    def confirm_owner_code(self, address: str, requester: str, profile_text: str) -> bool:
        """The owner added the code to the agent's Agentverse profile: allow it and mark verified."""
        rows = self._all("SELECT * FROM agents WHERE address=?", (address,))
        if not rows:
            return False
        row = rows[0]
        if row["owner"] != requester or not row["owner_code"] or time.time() > row["owner_code_expires"]:
            return False
        if row["owner_code"] not in profile_text:
            return False
        self._run(
            "UPDATE agents SET allowed=1, verified=1, last_verified=?, owner_code='' WHERE address=?",
            (time.time(), address),
        )
        return True

    def verified_agents(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM agents WHERE verified=1 AND allowed=1")

    def touch_verified(self, address: str) -> None:
        self._run("UPDATE agents SET last_verified=? WHERE address=?", (time.time(), address))

    # --- audits ----------------------------------------------------------------------

    def save_verdict(self, verdict: Verdict, requester: str, paid_tx: str = "") -> None:
        now = time.time()
        self._run(
            "INSERT OR REPLACE INTO audits(audit_id,address,target_name,requester,grade,score,partial,"
            "verdict_json,paid_tx,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                verdict.audit_id, verdict.target_address, verdict.target_name, requester, verdict.grade,
                verdict.score, int(verdict.partial), verdict.json(), paid_tx, now,
            ),
        )
        for f in verdict.findings:
            self._run(
                "INSERT OR REPLACE INTO findings(audit_id,case_id,severity,hard,finding_json) VALUES(?,?,?,?,?)",
                (verdict.audit_id, f.case_id, f.severity, int(f.hard), f.json()),
            )

    def get_verdict(self, audit_id: str) -> Verdict | None:
        rows = self._all("SELECT verdict_json FROM audits WHERE audit_id=?", (audit_id,))
        return Verdict.parse_raw(rows[0]["verdict_json"]) if rows else None

    def latest(self, address: str, max_age_s: float | None = None) -> RegistryEntry | None:
        """Most recent audit for an address; with max_age_s only if it is that fresh (24 h cache)."""
        rows = self._all(
            "SELECT a.verdict_json, a.created_at, g.verified, g.last_verified FROM audits a "
            "LEFT JOIN agents g ON g.address=a.address WHERE a.address=? ORDER BY a.created_at DESC LIMIT 1",
            (address,),
        )
        if not rows:
            return None
        row = rows[0]
        if max_age_s is not None and time.time() - row["created_at"] > max_age_s:
            return None
        v = Verdict.parse_raw(row["verdict_json"])
        top = sorted(v.findings, key=lambda f: (not f.hard, ("critical", "high", "medium", "low").index(f.severity)))
        return RegistryEntry(
            target_name=v.target_name,
            target_address=v.target_address,
            grade=v.grade,
            score=v.score,
            partial=v.partial,
            top_findings=top[:3],
            audited_at=row["created_at"],
            verified=bool(row["verified"] or 0),
            last_verified=float(row["last_verified"] or 0),
        )

    def findings_for(self, audit_id: str) -> list[Finding]:
        return [Finding.parse_raw(r["finding_json"]) for r in self._all(
            "SELECT finding_json FROM findings WHERE audit_id=?", (audit_id,))]

    # --- pending audits (server-side state for card selections) ----------------------

    def create_pending(self, audit_id: str, requester: str, plan_json: str) -> None:
        self._run(
            "INSERT OR REPLACE INTO pending(audit_id,requester,plan_json,state,created_at) VALUES(?,?,?,?,?)",
            (audit_id, requester, plan_json, "awaiting_payment", time.time()),
        )

    def get_pending(self, audit_id: str, requester: str) -> sqlite3.Row | None:
        """Only the requester that created it sees it, and only while it is fresh."""
        rows = self._all("SELECT * FROM pending WHERE audit_id=? AND requester=?", (audit_id, requester))
        if not rows or time.time() - rows[0]["created_at"] > config.PENDING_AUDIT_TTL_S:
            return None
        return rows[0]

    def latest_pending(self, requester: str, state: str = "awaiting_payment") -> sqlite3.Row | None:
        rows = self._all(
            "SELECT * FROM pending WHERE requester=? AND state=? ORDER BY created_at DESC LIMIT 1", (requester, state)
        )
        if not rows or time.time() - rows[0]["created_at"] > config.PENDING_AUDIT_TTL_S:
            return None
        return rows[0]

    def set_pending_state(self, audit_id: str, state: str, payment_ref: str = "") -> None:
        self._run("UPDATE pending SET state=?, payment_ref=CASE WHEN ?!='' THEN ? ELSE payment_ref END WHERE audit_id=?",
                  (state, payment_ref, payment_ref, audit_id))

    # --- payments (idempotent on transaction id) -----------------------------------------

    def record_payment(self, tx_id: str, requester: str, audit_id: str, method: str) -> bool:
        """True the first time a transaction id is seen. False on a replay: reject it."""
        try:
            self._run(
                "INSERT INTO payments(tx_id,requester,audit_id,method,created_at) VALUES(?,?,?,?,?)",
                (tx_id, requester, audit_id, method, time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def payment_seen(self, tx_id: str) -> bool:
        return bool(self._all("SELECT 1 FROM payments WHERE tx_id=?", (tx_id,)))

    def close(self) -> None:
        self._db.close()


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
        _registry.seed_allowlist()
    return _registry

