"""Registry: agents + allowlist, audits, pending audits, payment ledger, and the Judge's audits in flight.

The pending-audit and payment records are the server-side state the Orchestrator checks every card selection against:
a selection alone never starts anything.

Storage rule (hosted agents run handlers overlapping, and their store has no compare-and-swap): anything written by
concurrent handlers lives in its own key and is written once, never read-modified-written. A state change is a new
write-once key (`claim:<audit_id>:<n>`), a list is a log of write-once slots (`SlotLog`), readers assemble from keys.
Only the read-mostly `agents` table stays in SQLite (on hosted agents: a dump in one storage key, written only when it
actually changed).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

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
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,           -- local runs: the same write-once keys a hosted agent keeps in its storage
    value TEXT NOT NULL
);
"""

STORE_KEY = "canary_db"
DUP_TAIL_S = 20.0  # a duplicate of a request that finished this recently is still a duplicate, not a retry


# --- write-once keys over a plain key-value store ------------------------------------------------------------------


class SqliteKV:
    """The storage API (get/set/remove) over one SQLite table, with a real insert-if-absent for `put_once`."""

    def __init__(self, db: sqlite3.Connection, lock: threading.Lock) -> None:
        self._db, self._lock = db, lock

    def get(self, key: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value, sort_keys=True)))
            self._db.commit()

    def remove(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key=?", (key,))
            self._db.commit()

    def swap(self, key: str, expected: Any, value: Any) -> bool:
        """Compare-and-swap: write `value` only if the key still holds `expected` (None: is absent)."""
        new = json.dumps(value, sort_keys=True)
        with self._lock:
            if expected is None:
                cur = self._db.execute("INSERT OR IGNORE INTO kv(key,value) VALUES(?,?)", (key, new))
            else:
                cur = self._db.execute("UPDATE kv SET value=? WHERE key=? AND value=?",
                                       (new, key, json.dumps(expected, sort_keys=True)))
            self._db.commit()
            return cur.rowcount == 1


def put_once(store, key: str, value: Any = None, *, stale: Callable[[dict], bool] | None = None) -> str | None:
    """Write `key` only if nobody holds it (or its holder is `stale`). Returns this writer's nonce if it holds the key.

    SqliteKV does it atomically. A plain store (an Agentverse agent's storage) has no CAS: the write is read back and
    only the writer whose nonce survived holds it. Residual window: if B reads the key before A writes it and B's write
    lands after A's read-back, both believe they hold it. Async callers narrow that by re-checking with `holds` after a
    short sleep."""
    nonce = secrets.token_hex(8)
    rec = {"n": nonce, "at": time.time(), "v": value}
    old = store.get(key)
    if old is not None and not (stale is not None and isinstance(old, dict) and stale(old)):
        return None
    if isinstance(store, SqliteKV):
        return nonce if store.swap(key, old, rec) else None
    store.set(key, rec)
    got = store.get(key)
    return nonce if isinstance(got, dict) and got.get("n") == nonce else None


def holds(store, key: str, nonce: str) -> bool:
    got = store.get(key)
    return isinstance(got, dict) and got.get("n") == nonce


def value_of(store, key: str) -> Any:
    got = store.get(key)
    return got.get("v") if isinstance(got, dict) else None


class SlotLog:
    """An append-only list as write-once slots `<name>:0`, `<name>:1`, ... with a low-water mark `<name>:head`.

    Appenders scan from the head to the first free slot and `put_once` it, so slots stay contiguous and nothing is lost
    to an overlapping append. The head only skips slots the reader is done with; a lost head update just rescans."""

    def __init__(self, store, name: str) -> None:
        self.store, self.name = store, name

    def head(self) -> int:
        h = self.store.get(f"{self.name}:head")
        return h if isinstance(h, int) else 0

    def set_head(self, i: int) -> None:
        if i > self.head():
            self.store.set(f"{self.name}:head", i)

    def append(self, value: Any) -> int:
        i = self.head()
        while put_once(self.store, f"{self.name}:{i}", value) is None:
            i += 1
        return i

    def get(self, i: int, settled_s: float = 0.0) -> Any:
        """Slot i's value; with `settled_s`, None while the slot is younger than that (an overlapping appender may
        still overwrite it: a reader that consumes slots must not take one that is about to change)."""
        rec = self.store.get(f"{self.name}:{i}")
        if not isinstance(rec, dict) or time.time() - rec.get("at", 0) < settled_s:
            return None
        return rec.get("v")

    def occupied(self, i: int) -> bool:
        return self.store.get(f"{self.name}:{i}") is not None

    def items(self, start: int | None = None, settled_s: float = 0.0) -> list[tuple[int, Any]]:
        """Slots from `start` (default: the head) up to the first free one, or the first unsettled one."""
        out, i = [], self.head() if start is None else start
        while (rec := self.store.get(f"{self.name}:{i}")) is not None:
            if time.time() - rec.get("at", 0) < settled_s:
                break
            out.append((i, rec.get("v")))
            i += 1
        return out


# --- the registry ----------------------------------------------------------------------------------------------------


class StoredVerdict(NamedTuple):
    """A stored verdict and when it was stored: what a trust lookup needs to say how old a grade is."""

    verdict: Verdict
    audited_at: float
    verified: bool = False
    last_verified: float = 0.0


def finished(v: Verdict) -> bool:
    """True if the audit graded something. A verdict that graded nothing (target silent, probes down) is not a grade."""
    return (v.passed > 0 or bool(v.findings)) and "nothing could be graded" not in v.partial_reason


class Registry:
    """With `store` (an Agentverse hosted agent's key-value storage) the hot records are keys in that store and the
    `agents` table is an in-memory SQLite snapshotted into STORE_KEY when it changes. Without one, everything is in the
    SQLite file (the hot records in its `kv` table), so local runs and tests take the same write-once code path."""

    def __init__(self, path: str | None = None, store=None) -> None:
        self._store = store
        self._db = sqlite3.connect(":memory:" if store is not None else (path or config.DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._dump = ""  # the snapshot self._db currently holds (hosted)
        with self._lock:
            self._load()
            self._db.executescript(SCHEMA)
            self._db.commit()
            self._save()
        self.kv = store if store is not None else SqliteKV(self._db, self._lock)

    # --- the agents table: SQLite, snapshotted on hosted agents --------------------------------------------------

    def _load(self) -> None:
        """Rebuild the in-memory database from the stored SQL dump (sqlite3 serialize() needs Python 3.11; hosted runs 3.10)."""
        if self._store is None:
            return
        dump = self._store.get(STORE_KEY) or ""
        if not dump or dump == self._dump:
            return
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(dump)
            db.executescript(SCHEMA)
        except sqlite3.Error:  # an unreadable snapshot (older format) must not wedge the agent: start fresh
            db.close()
            return
        self._db, self._dump = db, dump

    def _save(self) -> None:
        """Write the snapshot only if it changed: a cold start (every hosted invocation) must not rewrite it."""
        if self._store is None:
            return
        dump = "\n".join(self._db.iterdump())
        if dump != self._dump:
            self._store.set(STORE_KEY, dump)
            self._dump = dump

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            self._load()
            cur = self._db.execute(sql, args)
            self._db.commit()
            self._save()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._load()
            return self._db.execute(sql, args).fetchall()

    # --- active audits (what the Orchestrator is waiting on) -------------------------------------------------------

    def add_active(self, audit_id: str, requester: str, plan_json: str, paid_tx: str, system: bool) -> None:
        """One key per attempt (a retry writes a new generation); the log lets the sweep find it."""
        self.kv.set(f"active:{audit_id}", {
            "audit_id": audit_id, "requester": requester, "plan_json": plan_json, "paid_tx": paid_tx,
            "system": int(system), "started": time.time(), "gen": secrets.token_hex(6),
        })
        SlotLog(self.kv, "activelog").append(audit_id)

    def get_active(self, audit_id: str) -> dict | None:
        row = self.kv.get(f"active:{audit_id}")
        if not isinstance(row, dict) or self.kv.get(f"active_done:{audit_id}:{row['gen']}") is not None:
            return None
        return row

    def pop_active(self, audit_id: str) -> dict | None:
        """Close an active audit and return it; only one caller gets it (verdict vs watchdog race)."""
        row = self.get_active(audit_id)
        if row is None or put_once(self.kv, f"active_done:{audit_id}:{row['gen']}") is None:
            return None
        return row

    def active_audits(self) -> list[dict]:
        log, rows, seen, head, closed_prefix = SlotLog(self.kv, "activelog"), [], set(), None, True
        for i, audit_id in log.items():
            row = self.get_active(audit_id) if audit_id not in seen else None
            if row is not None:
                rows.append(row)
                seen.add(audit_id)
                closed_prefix = False
            elif closed_prefix:
                head = i + 1
        if head is not None:
            log.set_head(head)
        return rows

    # --- judge: audits waiting for probe results ---------------------------------------------------------------------
    # jreq:<id> the request (one per attempt), jres:<id>:<kind> one key per probe result (first wins),
    # jfinal:<id>:<gen> the grading claim, jprog:<id>:<gen>:<kind> one progress line per probe.

    def _judge_req(self, audit_id: str) -> dict | None:
        rec = value_of(self.kv, f"jreq:{audit_id}")
        return rec if isinstance(rec, dict) else None

    def _judge_final(self, audit_id: str, gen: str) -> dict | None:
        return self.kv.get(f"jfinal:{audit_id}:{gen}")

    def judge_open(self, audit_id: str, req_json: str, deadline: float) -> bool:
        """Record a JudgeRequest. False for a duplicate (the same audit is open or just finished); a later retry of a
        finished audit opens a new attempt."""
        def finished_a_while_ago(old: dict) -> bool:
            gen = (old.get("v") or {}).get("gen", "")
            final = self._judge_final(audit_id, gen)
            return final is not None and time.time() > final.get("at", 0) + DUP_TAIL_S

        rec = {"req": req_json, "deadline": deadline, "gen": secrets.token_hex(6)}
        if put_once(self.kv, f"jreq:{audit_id}", rec, stale=finished_a_while_ago) is None:
            return False
        SlotLog(self.kv, "judgelog").append(audit_id)
        return True

    def judge_add_result(self, audit_id: str, kind: str, result_json: str) -> bool:
        """Store one probe's result, even before its JudgeRequest. False if that probe already reported, or the audit
        was already graded (a late result must not be taken for a retry's)."""
        rec = self._judge_req(audit_id)
        if rec is not None and self._judge_final(audit_id, rec["gen"]) is not None:
            return False
        return put_once(self.kv, f"jres:{audit_id}:{kind}", result_json) is not None

    def judge_get(self, audit_id: str) -> str | None:
        """The open attempt as {"req", "results", "deadline"}, assembled from its keys; None if no request is open."""
        rec = self._judge_req(audit_id)
        if rec is None or self._judge_final(audit_id, rec["gen"]) is not None:
            return None
        return self._judge_doc(audit_id, rec)

    def _judge_doc(self, audit_id: str, rec: dict) -> str:
        kinds = dict.fromkeys([*json.loads(rec["req"]).get("expected_kinds", []), *config.PROBE_KINDS])
        results = {k: r for k in kinds if (r := value_of(self.kv, f"jres:{audit_id}:{k}")) is not None}
        return json.dumps({"req": rec["req"], "results": results, "deadline": rec["deadline"]})

    def judge_claim_progress(self, audit_id: str, kind: str) -> bool:
        """True once per probe per attempt: whoever wins sends that probe's progress line."""
        rec = self._judge_req(audit_id)
        return rec is not None and put_once(self.kv, f"jprog:{audit_id}:{rec['gen']}:{kind}") is not None

    def judge_pop(self, audit_id: str) -> str | None:
        """Claim the open attempt for grading: only one caller gets it, so a Verdict is sent once."""
        rec = self._judge_req(audit_id)
        if rec is None or put_once(self.kv, f"jfinal:{audit_id}:{rec['gen']}") is None:
            return None
        doc = self._judge_doc(audit_id, rec)
        for kind in json.loads(doc)["results"]:
            self.kv.remove(f"jres:{audit_id}:{kind}")  # a retry of this audit starts from no results
        return doc

    def judge_due(self, now: float) -> list[str]:
        log, due, head, closed_prefix = SlotLog(self.kv, "judgelog"), [], None, True
        for i, audit_id in log.items():
            rec = self._judge_req(audit_id)
            if rec is None or self._judge_final(audit_id, rec["gen"]) is not None:
                if closed_prefix:
                    head = i + 1
                continue
            closed_prefix = False
            if rec["deadline"] <= now and audit_id not in due:
                due.append(audit_id)
        if head is not None:
            log.set_head(head)
        return due

    # --- agents & allowlist --------------------------------------------------

    def seed_allowlist(self) -> None:
        """Pre-allow the demo targets. Idempotent, and writes nothing when they are already there."""
        for t in config.DEMO_TARGETS.values():
            address = t.resolve_address()
            row = self.find_agent(address)
            if row and row["allowed"] and row["name"] == t.name and row["canary"] == t.canary:
                continue
            self.allow_agent(address, t.name, t.canary, demo=True)

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

    def all_agents(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM agents ORDER BY name")

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

    # --- audits: verdict:<audit_id> (written once per audit), vlast:<address> (newest audit),
    # vdone:<address> (newest audit that graded something) ------------------------------------------------------

    def save_verdict(self, verdict: Verdict, requester: str, paid_tx: str = "") -> None:
        now = time.time()
        self.kv.set(f"verdict:{verdict.audit_id}", {
            "verdict_json": verdict.json(), "requester": requester, "paid_tx": paid_tx, "created_at": now,
        })
        self.kv.set(f"vlast:{verdict.target_address}", {"audit_id": verdict.audit_id, "created_at": now})
        if finished(verdict):
            self.kv.set(f"vdone:{verdict.target_address}", {"audit_id": verdict.audit_id, "created_at": now})

    def _verdict_row(self, audit_id: str) -> dict | None:
        row = self.kv.get(f"verdict:{audit_id}")
        return row if isinstance(row, dict) else None

    def get_verdict(self, audit_id: str) -> Verdict | None:
        row = self._verdict_row(audit_id)
        return Verdict.parse_raw(row["verdict_json"]) if row else None

    def latest(self, address: str, max_age_s: float | None = None) -> RegistryEntry | None:
        """Most recent audit for an address; with max_age_s only if it is that fresh (24 h cache)."""
        last = self.kv.get(f"vlast:{address}")
        row = self._verdict_row(last["audit_id"]) if isinstance(last, dict) else None
        if row is None:
            return None
        if max_age_s is not None and time.time() - row["created_at"] > max_age_s:
            return None
        v = Verdict.parse_raw(row["verdict_json"])
        agent = self._all("SELECT verified, last_verified FROM agents WHERE address=?", (address,))
        top = sorted(v.findings, key=lambda f: (not f.hard, ("critical", "high", "medium", "low").index(f.severity)))
        return RegistryEntry(
            target_name=v.target_name,
            target_address=v.target_address,
            grade=v.grade,
            score=v.score,
            partial=v.partial,
            top_findings=top[:3],
            audited_at=row["created_at"],
            verified=bool(agent and agent[0]["verified"]),
            last_verified=float(agent[0]["last_verified"] or 0) if agent else 0.0,
        )

    def latest_verdict(self, address: str) -> StoredVerdict | None:
        """The newest finished audit for an address, with when it was stored. Rows that graded nothing (saved before
        the Orchestrator refused to cache them) are skipped: an audit that didn't finish never shows a grade."""
        done = self.kv.get(f"vdone:{address}")
        row = self._verdict_row(done["audit_id"]) if isinstance(done, dict) else None
        if row is None:
            return None
        v = Verdict.parse_raw(row["verdict_json"])
        if not finished(v):
            return None
        agent = self._all("SELECT verified, last_verified FROM agents WHERE address=?", (address,))
        verified = bool(agent and agent[0]["verified"])
        return StoredVerdict(v, float(row["created_at"]), verified, float(agent[0]["last_verified"] or 0) if agent else 0.0)

    def findings_for(self, audit_id: str) -> list[Finding]:
        v = self.get_verdict(audit_id)
        return list(v.findings) if v else []

    # --- pending audits (server-side state for card selections) ----------------------
    # pending:<id> is written once; its state is the chain claim:<id>:0, :1, ... of write-once transitions.

    def create_pending(self, audit_id: str, requester: str, plan_json: str) -> None:
        self.kv.set(f"pending:{audit_id}", {
            "audit_id": audit_id, "requester": requester, "plan_json": plan_json, "created_at": time.time(),
        })
        self.kv.set(f"plast:{requester}", audit_id)  # the requester's newest plan (last writer wins: it is the newest)

    def _pending_row(self, audit_id: str) -> dict | None:
        row = self.kv.get(f"pending:{audit_id}")
        if not isinstance(row, dict):
            return None
        state, ref, n = "awaiting_payment", "", 0
        while (step := value_of(self.kv, f"claim:{audit_id}:{n}")) is not None:
            state, ref, n = step["state"], step["ref"] or ref, n + 1
        return {**row, "state": state, "payment_ref": ref, "epoch": n}

    def get_pending(self, audit_id: str, requester: str) -> dict | None:
        """Only the requester that created it sees it, and only while it is fresh."""
        row = self._pending_row(audit_id)
        if not row or row["requester"] != requester or time.time() - row["created_at"] > config.PENDING_AUDIT_TTL_S:
            return None
        return row

    def latest_pending(self, requester: str, state: str = "awaiting_payment") -> dict | None:
        audit_id = self.kv.get(f"plast:{requester}")
        row = self.get_pending(audit_id, requester) if isinstance(audit_id, str) else None
        return row if row and row["state"] == state else None

    def set_pending_state(self, audit_id: str, state: str, payment_ref: str = "") -> None:
        n = (self._pending_row(audit_id) or {}).get("epoch", 0)
        while put_once(self.kv, f"claim:{audit_id}:{n}", {"state": state, "ref": payment_ref}) is None:
            n += 1

    def claim_pending(self, audit_id: str, requester: str, from_state: str, to_state: str, payment_ref: str = "") -> bool:
        """Atomic state transition: only one caller wins (write-once claim:<id>:<n>), so a payment or a retry starts
        an audit once."""
        row = self._pending_row(audit_id)
        if not row or row["requester"] != requester or row["state"] != from_state:
            return False
        return put_once(self.kv, f"claim:{audit_id}:{row['epoch']}", {"state": to_state, "ref": payment_ref}) is not None

    # --- payments (idempotent on transaction id) -----------------------------------------

    def record_payment(self, tx_id: str, requester: str, audit_id: str, method: str) -> bool:
        """True the first time a transaction id is seen. False on a replay: reject it."""
        rec = {"requester": requester, "audit_id": audit_id, "method": method}
        return put_once(self.kv, f"pay:{tx_id}", rec) is not None

    def save_dodo_session(self, audit_id: str, requester: str, session_id: str, url: str) -> None:
        self.kv.set(f"dodo:{audit_id}", {
            "audit_id": audit_id, "session_id": session_id, "requester": requester, "url": url, "created_at": time.time(),
        })

    def dodo_session(self, audit_id: str, requester: str) -> dict | None:
        row = self.kv.get(f"dodo:{audit_id}")
        return row if isinstance(row, dict) and row["requester"] == requester else None

    def paid_checkout_pending(self, audit_id: str, requester: str) -> dict | None:
        """The pending row of an audit that has a Dodo checkout, ignoring the plan's TTL: a checkout link outlives the
        plan, and a buyer who paid it after the TTL must still get the audit. Only for a payment check, never a start."""
        if not self.dodo_session(audit_id, requester):
            return None
        row = self._pending_row(audit_id)
        return row if row and row["requester"] == requester else None

    def payment_seen(self, tx_id: str) -> bool:
        return self.kv.get(f"pay:{tx_id}") is not None

    def close(self) -> None:
        self._db.close()


_registry: Registry | None = None
_store = None


def bind_store(store) -> None:
    """Hosted agents: keep the registry in the agent's own storage (called once by the bundle's agent.py)."""
    global _store, _registry
    _store, _registry = store, None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry(store=_store)
        _registry.seed_allowlist()
    return _registry
