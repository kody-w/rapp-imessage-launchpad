"""A deliberately blocked template. This is not an installed evidence producer."""


def build(context: dict) -> dict:
    return {
        "scenario": "future",
        "status": "blocked",
        "title": "Synthetic contract example — not a real finding",
        "change": "This template demonstrates the proposal boundary only.",
        "impact": "No real source evidence was inspected.",
        "action": "Implement and configure a real source before enabling this producer.",
        "decision": "Remain blocked; never turn a synthetic fixture into a real notification.",
        "evidence": [],
        "artifacts": [],
        "fingerprint": "synthetic-contract-example-v1",
        "urgency": "routine",
        "reason": "The example is deliberately unsendable.",
    }
