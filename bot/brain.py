"""The only place that calls Claude. Every call's real cost is measured from the API's usage numbers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class Decision:
    status: str                 # ok | refused | truncated | bad_output | api_error
    weights: Optional[dict]
    rationale: str
    cost_usd: float
    detail: str = ""


def _prices() -> tuple[float, float]:
    return config.MODEL_PRICES_PER_MTOK[config.CLAUDE_MODEL]


def cost_of_usage(usage) -> float:
    """Dollar cost of one response, from its usage block (thinking is billed as output)."""
    pin, pout = _prices()

    def n(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
        return value or 0

    return (
        n("input_tokens") * pin
        + n("cache_creation_input_tokens") * pin * 1.25
        + n("cache_read_input_tokens") * pin * 0.1
        + n("output_tokens") * pout
    ) / 1_000_000


def worst_case_cost(system: str, prompt: str) -> float:
    """Upper bound for one call: generous input estimate plus the full output allowance."""
    pin, pout = _prices()
    est_input_tokens = (len(system) + len(prompt)) / 3 + 200
    return (est_input_tokens * pin + config.MAX_OUTPUT_TOKENS * pout) / 1_000_000


def output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "weights": {
                "type": "object",
                "properties": {s: {"type": "number"} for s in config.UNIVERSE},
                "required": list(config.UNIVERSE),
                "additionalProperties": False,
            },
            "rationale": {"type": "string"},
        },
        "required": ["weights", "rationale"],
        "additionalProperties": False,
    }


def decide(system: str, prompt: str) -> Decision:
    import anthropic  # imported here so the offline tests need no install

    client = anthropic.Anthropic(timeout=300.0, max_retries=2)
    try:
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_OUTPUT_TOKENS,
            system=system,
            output_config={
                "effort": config.CLAUDE_EFFORT,
                "format": {"type": "json_schema", "schema": output_schema()},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APITimeoutError:
        # A timed-out call may still have been billed, and it may have been retried:
        # count the worst case for every attempt so the budget is never underestimated.
        return Decision("api_error", None, "", worst_case_cost(system, prompt) * 3, "timeout")
    except anthropic.APIError as e:
        return Decision("api_error", None, "", 0.0, type(e).__name__)

    cost = cost_of_usage(resp.usage)
    if resp.stop_reason == "refusal":
        return Decision("refused", None, "", cost)
    if resp.stop_reason == "max_tokens":
        return Decision("truncated", None, "", cost)
    text = next((b.text for b in resp.content if b.type == "text"), None)
    try:
        data = json.loads(text)
        weights = data["weights"]
        rationale = str(data.get("rationale", ""))[:1000]
    except (TypeError, ValueError, KeyError):
        return Decision("bad_output", None, "", cost)
    if not isinstance(weights, dict):
        return Decision("bad_output", None, "", cost)
    return Decision("ok", weights, rationale, cost)
