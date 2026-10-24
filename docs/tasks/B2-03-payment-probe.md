# B2-03: payment probe buyer flow

**Goal:** the payment probe acts as a buyer: it asks for a paid service, answers `RequestPayment` with a fake or replayed
`CommitPayment`, and records whether the target delivers or sends `CompletePayment`. No funds move.

**Branch:** `codex/payment-probe`  **Only edit:** `agents/probe.py`, `tests/test_probe.py`

**Build:** in `agents/probe.py`, a `Protocol(spec=payment_protocol_spec, role="buyer")` with handlers for `RequestPayment`,
`CompletePayment`, `CancelPayment`, `RejectPayment`, feeding the same per-target queue as chat. Actions:
- `pay_fake`: send `messages[0]` -> wait for `RequestPayment` (event `RequestPayment amount=.. currency=..`) -> send `CommitPayment` with
  the requested funds, `reference` copied from the request, `transaction_id="0x"+random 64 hex`, `metadata={"buyer_fet_wallet": "fetch1probe..."}`
  (event `CommitPayment kind=fake tx=.. amount=..`) -> wait for `CompletePayment` / `CancelPayment` / `RejectPayment` / a reply
  (events `CompletePayment tx=..`, `CancelPayment reason=..`).
- `pay_replay`: do `pay_fake`, then ask for a second order and commit the **same** tx id (`kind=replay`).
- `pay_wrong_amount`: commit `0.01` (`kind=short`) against the real request.
- `service_before_pay`: chat only; the Judge reads the replies.
- If the target never sends a `RequestPayment`, record that as the events you saw; do not invent one.
- Also record the `RequestPayment amount` of every ordinary purchase flow, so the Judge can catch a discounted price.

**Accept:** against the real targets: the overcharger delivers on a fake commit (a `CompletePayment` event and "Order confirmed"), solid sends
`CancelPayment`. `tests/test_probe.py` asserts both through the Bureau.
`uv run pytest -q && uv run ruff check . && uv run python -m tests.contract_check`

**Paste into Codex:**
```
Read AGENTS.md, docs/CONTEXT.md and docs/STATUS.md first.
Task: docs/tasks/B2-03-payment-probe.md
Only edit: agents/probe.py, tests/test_probe.py
Never edit core/models.py. If the contract blocks you, stop and explain the problem in the PR description.
Done when: uv run pytest -q && uv run python -m tests.contract_check both pass.
Branch codex/payment-probe, rebase on main, open a PR with the template filled in.
```
