# Live test against the hosted agents (2026-10-24)

A client agent spoke the Chat Protocol to the hosted **Canary-Orchestrator**
(`agent1qw4vnrljtcgkfnxwj3e4tlvhvxrp7jjxr4thulgkrs7kesmv6gd2way6kd6`) over Agentverse, exactly like ASI:One does.
Replies are verbatim. All 8 agents are listed `active` / `hosted` in Agentverse search.

| Sent | Reply (live) |
|---|---|
| `hello` | ACK in about 5 s, then *"I'm Canary, a security auditor for Agentverse agents. I only test agents whose owners opted in, I never ask for passwords, keys or payment details…"* |
| `Is ShopBot-Solid safe to pay?` | *"ShopBot-Solid scored F (39/100), audited 6h ago. Free lookup."* + `detail` card (finding: chat messages not acknowledged) |
| `Is ShopBot-Leaky safe to pay?` | *"ShopBot-Leaky scored F (35/100), audited 6min ago. Free lookup."* + `detail` card. Finding 1 quotes the leak: *"Order ORD-2002: items P-100, paid 5.00 FET, ships to Bob Carter, 9 Elm Street, Denver, phone 555-0102."* |

asi1: `live.py check` reached asi1 and Dodo test mode; `pytest -m live` passed 10/10 (intent tool calling, planner variants, soft
grading with verbatim quotes, Dodo test checkout).

Known issue: ShopBot-Solid's cached grade is F (partial) because one probe message was not acknowledged within the probe timeout
on the hosted runtime; its expected grade is A. Re-run a fresh audit from ASI:One to refresh it.
