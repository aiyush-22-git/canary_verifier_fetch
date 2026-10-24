![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# ShopBot-Solid: demo outdoor-gear shop agent

ShopBot-Solid checks order ownership, allows only listed discount codes, caps refunds, ignores instructions hidden in tool results and confirms an order only after a verified payment on an offer, review, confirm flow. It is the control Canary should grade well, but no audit proves an agent safe.

**What it sells:** outdoor gear (trail shoes, bottles, merino socks, rain shell, trekking poles, headlamp) for 5.00 FET on the test network. No real goods ship.

**Tools:** `search_catalog`, `get_order`, `apply_discount`, `issue_refund`. It also shows an offer card and a review card, and asks for payment over the Payment Protocol.

Demo target for Canary: this agent is deliberately hardened. Run an audit with Canary-Orchestrator.

**Keywords:** shop agent, orders, refunds, discounts, demo target
