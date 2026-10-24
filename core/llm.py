"""The only asi1 client. OpenAI SDK pointed at ASI:One, 20 s timeout, one retry.

Nothing else imports `openai`. Callers get plain dicts / strings, or `None` on failure so
every feature can degrade (rules-only verdicts, keyword intent, cached plans).
"""

from __future__ import annotations

import json
import logging
import re
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


async def _create(**kwargs: Any) -> Any:
    """One call with one retry. Raises the last error; wrappers below turn that into None."""
    if not available():
        raise RuntimeError("ASI1_API_KEY is not set")
    last: Exception | None = None
    for attempt in range(2):
        try:
            return await _get_client().chat.completions.create(**kwargs)
        except Exception as exc:  # network, 429, 5xx, timeout
            last = exc
            log.warning("asi1 call failed (attempt %d): %s", attempt + 1, type(exc).__name__)
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
    except Exception:
        return None
    calls = resp.choices[0].message.tool_calls or []
    if not calls:
        return None
    fn = calls[0].function
    try:
        args = json.loads(fn.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    return fn.name, args if isinstance(args, dict) else {}
