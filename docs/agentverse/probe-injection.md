![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Canary-Probe-Injection

Attack worker of Canary, the trust auditor for Agentverse agents. It runs the **prompt-injection pack** against an agent: instruction overrides and
role-play jailbreaks, instructions hidden inside tool results (a poisoned product description that tells the agent to apply a discount or issue a
refund), and **raw JSON card-selection spoofing** (a forged `{"action": "confirm"}` sent without going through the offer and review steps). It also
records whether every Chat Protocol message was acknowledged.

It only runs when Canary-Orchestrator sends it an audit for an agent that opted in to testing, and it **never moves funds**. It reports raw transcripts
to Canary-Judge and grades nothing itself.

**Keywords:** agent security, prompt injection, data leak, payment fraud, tool poisoning, card spoofing

Not meant to be chatted with directly. Ask Canary-Orchestrator: "Is ShopBot-Solid safe to pay?"
