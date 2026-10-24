"""A key-value store that makes two handler invocations overlap the way hosted Agentverse agents can.

`Lockstep.overlap(a, b)` runs two invocations in threads that take strict turns, one storage operation each, while both
are alive: A reads, B reads, A writes, B writes. That is the schedule that loses a read-modify-write update (last
writer wins) and that lets two check-then-set callers both see a key empty. Outside `overlap` it is a plain store.
Values are copied through JSON on the way in and out, like a real store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from typing import Any


class _View:
    def __init__(self, owner: Lockstep, who: int | None) -> None:
        self._owner, self._fixed = owner, who

    @property
    def _who(self) -> int:
        """`shared` views take the turn of whichever overlapping invocation (thread) uses them."""
        return self._fixed if self._fixed is not None else self._owner.who_by_thread.get(threading.get_ident(), -1)

    def get(self, key: str) -> Any:
        raw = self._owner.op(self._who, lambda: self._owner.data.get(key))
        return None if raw is None else json.loads(raw)

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def set(self, key: str, value: Any) -> None:
        raw = json.dumps(value)
        self._owner.op(self._who, lambda: self._owner.data.__setitem__(key, raw))
        self._owner.writes += 1

    def remove(self, key: str) -> None:
        self._owner.op(self._who, lambda: self._owner.data.pop(key, None))


class Lockstep:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.writes = 0
        self._cond = threading.Condition()
        self._turn = 0
        self._alive: set[int] = set()
        self.who_by_thread: dict[int, int] = {}
        self.plain = _View(self, -1)  # never waits: set-up and checks outside `overlap`
        self.shared = _View(self, None)  # for objects built before `overlap` and used inside it (a warm registry)

    def op(self, who: int, fn: Callable[[], Any]) -> Any:
        with self._cond:
            if who in self._alive:
                self._cond.wait_for(lambda: self._turn == who or len(self._alive) < 2, timeout=5)
            out = fn()
            if who in self._alive:
                self._turn = 1 - who
                self._cond.notify_all()
            return out

    def overlap(self, a: Callable[[_View], Any], b: Callable[[_View], Any]) -> list[Any]:
        """Run a(store) and b(store) as two overlapping invocations; returns their results."""
        results: list[Any] = [None, None]
        errors: list[BaseException] = []
        self._alive, self._turn = {0, 1}, 0

        def go(who: int, fn: Callable[[_View], Any]) -> None:
            self.who_by_thread[threading.get_ident()] = who
            try:
                results[who] = fn(_View(self, who))
            except BaseException as e:  # surfaced in the test thread below
                errors.append(e)
            finally:
                with self._cond:
                    self._alive.discard(who)
                    self._cond.notify_all()

        threads = [threading.Thread(target=go, args=(i, f)) for i, f in enumerate((a, b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        if errors:
            raise errors[0]
        return results

    def overlap_async(self, a, b) -> list[Any]:
        """Same, for two coroutine functions taking the store view."""
        return self.overlap(lambda s: asyncio.run(a(s)), lambda s: asyncio.run(b(s)))


class FakeCtx:
    """What a handler touches on its Context: storage, a logger, send (recorded)."""

    def __init__(self, storage, sent: list[tuple[str, Any]]) -> None:
        self.storage, self.sent = storage, sent
        self.logger = logging.getLogger("fake-ctx")

    async def send(self, to: str, msg: Any) -> None:
        self.sent.append((to, msg))


class FakeAgent:
    """Collects the handlers a build_* function registers, so tests can call them directly."""

    def __init__(self) -> None:
        self.handlers: dict[Any, Callable] = {}

    def on_message(self, model):
        def deco(fn):
            self.handlers[model] = fn
            return fn
        return deco

    def on_interval(self, period: float):
        def deco(fn):
            self.handlers["interval"] = fn
            return fn
        return deco
