---
name: uv-package
description: Authoritative project setup for Fetch.ai uAgent codebases that use `uv` as the Python package manager. Owns dependency installation, lockfile management, virtual environment creation, run commands, and `pyproject.toml` shape. Bootstraps in the current working directory by default (`uv init --bare`), never creates a sub-folder unless explicitly asked, and never generates a `main.py` (the protocol skill owns `agent.py`). Use when the project has a `uv.lock` or `[tool.uv]` block, when the user explicitly chose `uv package` in `fetch-skills`, or when bootstrapping a new uAgent project with no manager. Other skills (chat-protocol, stripe-payment-protocol, fet-payment-protocol, payment-protocol) defer to this skill for setup commands when it is installed.
---

# uv Package Setup (Fetch.ai uAgents)

## Purpose

Single source of truth for environment setup in uAgent projects that use `uv`. Whenever a protocol skill needs to install a dependency, run the agent, generate a lockfile, or create `pyproject.toml`, defer to this skill instead of inventing commands.

## When to Use

- The project root contains `uv.lock` or `[tool.uv]` in `pyproject.toml`.
- The user selected `uv package` in the `fetch-skills` installer.
- Bootstrapping a fresh uAgent project with no manager yet (recommended default).

## When NOT to Use

- The project already uses Poetry (`poetry.lock` or `[tool.poetry]`) — use `poetry-package`.
- The project already uses plain venv + pip (`requirements.txt`, `.venv/`) — use `python-venv-package`.
- The user wants code only without a package setup — use `no-package`.

---

## Required Tooling

`uv` 0.4.x or newer. If missing, install once per machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then verify:

```bash
uv --version
```

If your environment disallows install scripts, use your platform package manager instead.

`uv` manages `.venv/` automatically. Do not run `python -m venv` in a `uv` project.

---

## Bootstrap a New Project

Default behavior: bootstrap **in the current directory**. Do not create a sub-folder unless the user explicitly asked for one. Do not generate a `main.py` — the protocol skill owns `agent.py`.

```bash
# In the user's existing project directory:
uv init --bare                        # creates pyproject.toml only — no main.py, no subfolder
uv add uagents uagents-core python-dotenv openai
```

`uv init --bare` writes `pyproject.toml` to the current working directory and nothing else (no source file, no `.python-version`, no `README.md`). The protocol skill (chat / payment / fet / stripe) owns `agent.py` and the `protocols/` package layout.

If `pyproject.toml` already exists, **skip `uv init` entirely** and go straight to `uv add` — never overwrite an existing project file.

```bash
# pyproject.toml already exists -> just add deps to the existing project
uv add uagents uagents-core python-dotenv openai
```

Only run `uv init <name>` (which creates a new sub-folder) when the user explicitly asked you to scaffold into a fresh directory. In every other case, bootstrap is in-place.

Recommended `pyproject.toml`:

```toml
[project]
name = "agent"
version = "0.1.0"
description = "Fetch.ai uAgent"
requires-python = ">=3.11"
dependencies = [
    "uagents",
    "uagents-core",
    "python-dotenv",
    "openai",
]

[dependency-groups]
dev = [
    "pytest",
    "ruff",
]
```

Note: `readme = "README.md"` is intentionally omitted because `uv init --bare` does not generate one. Add it back if (and only if) the user actually creates a `README.md`.

---

## Add Dependencies

| Need | Command |
| --- | --- |
| Base uAgent deps (chat-capable, ASI:One default) | `uv add uagents uagents-core python-dotenv openai` |
| Stripe payment protocol | `uv add stripe` |
| FET payment protocol | `uv add cosmpy` |
| Combined Stripe + FET payment protocol | `uv add stripe cosmpy` |
| Anthropic client (instead of ASI:One) | `uv add anthropic` |
| HTTP client (preferred) | `uv add httpx` |
| Dev tools | `uv add --dev pytest ruff` |

`openai` is in the base set because the payment-protocol skills ship with ASI:One as the default LLM (ASI:One is OpenAI Chat Completions API-compatible — the official `openai` SDK works unchanged with `base_url="https://api.asi1.ai/v1"`). Drop it only when the user explicitly chooses a different LLM provider or when `run_paid_action` does not need an LLM at all.

`uv add` updates `pyproject.toml` and `uv.lock` in one step. Do not edit `uv.lock` by hand.

## Sync From Lockfile

When pulling a project on a fresh machine or in CI:

```bash
uv sync
```

This installs the exact versions recorded in `uv.lock`.

## Run the Agent

```bash
uv run python agent.py
```

`uv run` activates the project's `.venv` for the duration of the command. No manual `source .venv/bin/activate` needed.

For arbitrary commands:

```bash
uv run pytest
uv run ruff check .
```

## Optional `requirements.txt` Mirror

For deployment targets that only accept `requirements.txt`:

```bash
uv export --format requirements-txt -o requirements.txt
```

Regenerate after every `uv add` / `uv lock`.

---

## Generated Project Files

Always create or update these alongside the agent code.

### `.env.example`

```dotenv
# Agent identity
AGENT_NAME=my-agent
AGENT_SEED=replace-with-a-strong-secret-seed
AGENT_PORT=8000
AGENT_ENDPOINT=
AGENT_MAILBOX=true
```

Append protocol-specific blocks (Stripe, FET) from the relevant protocol skill.

### `.gitignore`

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
.Python

# uv
.venv/
.uv-cache/

# Environment / secrets
.env
.env.local
.env.*.local

# IDE / OS
.idea/
.vscode/
.DS_Store

# Test / coverage
.pytest_cache/
.coverage
htmlcov/
```

### `README.md` Quick Start

`uv init --bare` does not create a `README.md`. Only generate one if the user asked for it (or one already exists). When you do generate it, use:

````markdown
## Quick start

```bash
uv sync
cp .env.example .env
# fill in AGENT_SEED, ASI_ONE_API_KEY, and any protocol secrets
uv run python agent.py
```
````

---

## Hard Rules

- Bootstrap **in the current working directory** by default. Never run `uv init <name>` (which creates a sub-directory) unless the user explicitly asked for a new folder. Default to `uv init --bare` so no sub-folder, `main.py`, or `README.md` is generated.
- If `pyproject.toml` already exists in the current directory, skip `uv init` entirely and go straight to `uv add`. Never overwrite an existing project file.
- Never generate or keep a top-level `main.py`. The protocol skill owns `agent.py` — that is the entrypoint.
- Always use `uv add` to add deps; never edit `uv.lock` by hand.
- Always run Python entrypoints and Python tools via `uv run`; do not call the system `python` directly for project tasks.
- Always pin Python with `requires-python = ">=3.11"` (or higher) in `pyproject.toml`.
- Always commit `pyproject.toml` and `uv.lock`.
- Never run `python -m venv` in a `uv` project — `uv` owns `.venv/`.
- Never `pip install` into a `uv` project's `.venv`.
- Never mix `uv` with `poetry` in the same project. If both lockfiles exist, surface the conflict and stop.

## Composition With Protocol Skills

When a protocol skill needs a new dependency:

1. Use the table above to map protocol → package.
2. Add it with `uv add <pkg>`.
3. Confirm `uv.lock` updated.
4. Continue with the protocol skill's code generation.

This skill does not generate any agent code itself — it only owns environment setup commands.
