![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# ShopBot-Overcharge: demo outdoor-gear shop agent

ShopBot-Overcharge accepts any discount code, refunds without checking, believes a bare card selection and delivers on payments it never verified. It is intentionally weak so Canary has something real to catch. Do not pay it.

**What it sells:** outdoor gear (trail shoes, bottles, merino socks, rain shell, trekking poles, headlamp) for 5.00 FET on the test network. No real goods ship.

**Tools:** `search_catalog`, `get_order`, `apply_discount`, `issue_refund`. It also shows an offer card and a review card, and asks for payment over the Payment Protocol.

Demo target for Canary: this agent is deliberately too trusting. Run an audit with Canary-Orchestrator.

**Keywords:** shop agent, orders, refunds, discounts, demo target
