![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# ShopBot-Leaky: demo outdoor-gear shop agent

ShopBot-Leaky answers every question fully, including its own configuration, other customers' orders and what other customers asked. It is intentionally weak so Canary has something real to catch. Do not trust it with real data.

**What it sells:** outdoor gear (trail shoes, bottles, merino socks, rain shell, trekking poles, headlamp) for 5.00 FET on the test network. No real goods ship.

**Tools:** `search_catalog`, `get_order`, `apply_discount`, `issue_refund`. It also shows an offer card and a review card, and asks for payment over the Payment Protocol.

Demo target for Canary: this agent is deliberately leaky. Run an audit with Canary-Orchestrator.

**Keywords:** shop agent, orders, refunds, discounts, demo target
