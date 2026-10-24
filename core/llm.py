"""The only asi1 client. OpenAI SDK pointed at ASI:One, 20 s timeout, one retry, a persisted token cap.

Nothing else imports `openai`. Callers get plain dicts / strings, or `None` on failure so
every feature can degrade (rules-only verdicts, keyword intent, cached plans).
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from core import config

log = logging.getLogger("canary.llm")

TIMEOUT_S = 20
_client: AsyncOpenAI | None = None


def available() -> bool:
    return bool(config.ASI1_API_KEY)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.ASI1_API_KEY, base_url=config.ASI1_BASE_URL, timeout=TIMEOUT_S, max_retries=0
        )
    return _client


def reset_client() -> None:
    """Tests and key rotation: drop the cached client."""
    global _client
    _client = None


LOGGER: Any = None  # hosted bundles set this to the agent's own logger: it is the only log output the platform shows


def visible(message: str) -> None:
    """A diagnostic line that reaches the hosted agent's logs (never include keys or prompts)."""
    (LOGGER or log).warning(message)


# --- token budget: every asi1 call counts; persisted so restarts can't reset it ------------


def tokens_used() -> int:
    try:
        return int(json.loads(Path(config.LLM_USAGE_FILE).read_text(encoding="utf-8"))["total_tokens"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _record_usage(resp: Any) -> None:
    spent = getattr(getattr(resp, "usage", None), "total_tokens", None)
    if not isinstance(spent, int) or spent <= 0:
        return
    path = Path(config.LLM_USAGE_FILE)
    try:  # bookkeeping must never fail a call that already succeeded (read-only home, full disk)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"total_tokens": tokens_used() + spent}), encoding="utf-8")
    except OSError as exc:
        log.warning("could not record asi1 usage: %s", type(exc).__name__)


async def _create(**kwargs: Any) -> Any:
    """One call with one retry. Raises the last error; wrappers below turn that into None."""
    if not available():
        raise RuntimeError("ASI1_API_KEY is not set")
    if tokens_used() >= config.LLM_TOKEN_CAP:
        raise RuntimeError(f"asi1 token cap reached ({config.LLM_TOKEN_CAP} tokens)")
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = await _get_client().chat.completions.create(**kwargs)
            _record_usage(resp)
            return resp
        except Exception as exc:  # network, 429, 5xx, timeout
            last = exc
            log.warning("asi1 call failed (attempt %d): %s", attempt + 1, type(exc).__name__)
            visible(f"[llm] asi1 call failed (attempt {attempt + 1}): {type(exc).__name__}: {str(exc)[:160]}")
    assert last is not None
    raise last


async def complete(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 800,
) -> str | None:
    """Plain text completion. `model=config.ASI1_FALLBACK_MODEL` is what targets fall back to."""
    try:
        resp = await _create(
            model=model or config.ASI1_MODEL, messages=messages, temperature=temperature, max_tokens=max_tokens
        )
    except Exception:
        return None
    return (resp.choices[0].message.content or "").strip()


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (handles ``` fences and chatter)."""
    cleaned = _FENCE.sub("", raw.strip())
    try:
        out = json.loads(cleaned)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        out = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


async def complete_json(system: str, user: str, *, max_tokens: int = 900) -> dict[str, Any] | None:
    """Ask for a JSON object. Returns None if asi1 is down or the reply is not JSON."""
    raw = await complete(
        [
            {"role": "system", "content": system + "\nReply with one JSON object and nothing else."},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return parse_json_object(raw) if raw else None


async def tool_call(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int = 400
) -> tuple[str, dict[str, Any]] | None:
    """Let asi1 pick one tool. Returns (tool_name, arguments) or None when it answered in prose
    or the call failed."""
    try:
        resp = await _create(
            model=config.ASI1_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        visible(f"[llm] tool_call unavailable: {type(exc).__name__}: {str(exc)[:160]}")
        return None
    calls = resp.choices[0].message.tool_calls or []
    if not calls:
        visible(f"[llm] tool_call: model answered in prose, no tool: {(resp.choices[0].message.content or '')[:100]!r}")
        return None
    fn = calls[0].function
    try:
        args = json.loads(fn.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    return fn.name, args if isinstance(args, dict) else {}


ToolHandler = Callable[[str, dict[str, Any]], Any | Awaitable[Any]]


async def run_tools(
    system: str,
    history: list[dict[str, Any]],
    user: str,
    tools: list[dict[str, Any]],
    handler: ToolHandler,
    *,
    model: str | None = None,
    max_rounds: int = 5,
    max_tokens: int = 600,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Target brain: the full tool-call loop (asi1 asks for tools -> `handler` runs them -> results go
    back -> repeat, at most `max_rounds`). asi1 keeps no server-side thread, so the caller owns
    `history` and gets the extended one back. Returns (text, history) or None when asi1 failed."""
    convo = [*history, {"role": "user", "content": user}]
    try:
        for _ in range(max_rounds + 1):
            resp = await _create(
                model=model or config.ASI1_TARGET_MODEL,
                messages=[{"role": "system", "content": system}, *convo],
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=max_tokens,
            )
            msg = resp.choices[0].message
            calls = list(msg.tool_calls or [])
            if not calls:
                text = (msg.content or "").strip()
                convo.append({"role": "assistant", "content": text})
                return text, convo
            convo.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = {}
                result = handler(c.function.name, args if isinstance(args, dict) else {})
                if inspect.isawaitable(result):
                    result = await result
                convo.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result, default=str)})
    except Exception as exc:  # asi1 down, cap reached, or a tool blew up: the target degrades
        log.warning("asi1 tool loop failed: %s", type(exc).__name__)
        return None
    log.warning("asi1 tool loop exceeded %d rounds", max_rounds)
    return None
