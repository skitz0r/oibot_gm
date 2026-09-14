"""Provider boundary: `complete(system, user, schema) -> schema instance`.

Everything above this line is provider-neutral. Only the Anthropic adapter
exists today; the routing table maps workloads to models so a cheaper model
can be substituted per workload without touching callers."""
from __future__ import annotations

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# workload -> (model, effort). Overridable via OIBOT_MODEL_<WORKLOAD>.
ROUTES: dict[str, tuple[str, str]] = {
    "loot_recommend": ("claude-opus-5", "high"),
    "roster_explain": ("claude-sonnet-5", "medium"),
    "roster_change": ("claude-opus-5", "medium"),   # NL request -> structured ops
    "loot_feedback": ("claude-opus-5", "medium"),   # NL feedback on a distribution (compact digest)
    "policy_compile": ("claude-opus-5", "high"),    # prose policy -> rules/constraints (rare, must be right)
    "config_change": ("claude-sonnet-5", "medium"), # plain-text config -> typed ops (cheap, confirmed by a human)
}

# $ per million tokens: (input, output, cache read, cache write)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}


class BudgetExceeded(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    def complete(self, workload: str, system: str, user: str, schema: type[T]) -> T: ...


class AnthropicProvider:
    def __init__(self) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.last_usage: dict[str, int] = {}
        self.name = "claude"
        self.log: list[dict] = []  # one row per call: workload, model, tokens, cost
        self.budget_usd = float(os.environ.get("OIBOT_BUDGET_USD", "5"))

    def route(self, workload: str) -> tuple[str, str]:
        model, effort = ROUTES[workload]
        return os.environ.get(f"OIBOT_MODEL_{workload.upper()}", model), effort

    @property
    def cost_usd(self) -> float:
        return sum(r["cost"] for r in self.log)

    def summary(self) -> str:
        by: dict[str, list[dict]] = {}
        for r in self.log:
            by.setdefault(r["workload"], []).append(r)
        parts = [f"{w}: {len(rs)} calls, {sum(r['input']+r['cache_read'] for r in rs)/1000:.0f}K in / {sum(r['output'] for r in rs)/1000:.1f}K out, ${sum(r['cost'] for r in rs):.2f}" for w, rs in by.items()]
        return f"LLM spend ${self.cost_usd:.2f} of ${self.budget_usd:.0f} budget · " + " · ".join(parts) if parts else "no LLM calls yet"

    def complete(self, workload: str, system: str, user: str, schema: type[T]) -> T:
        if self.cost_usd >= self.budget_usd:
            raise BudgetExceeded(f"LLM budget ${self.budget_usd:.2f} reached (spent ${self.cost_usd:.2f}); set OIBOT_BUDGET_USD to raise")
        model, effort = self.route(workload)
        resp = self.client.messages.parse(
            model=model,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            # the system prompt (policy doc etc.) is the stable prefix
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        u = resp.usage
        row = {
            "workload": workload,
            "model": model,
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        pi, po, pr, pw = PRICES.get(model, PRICES["claude-opus-5"])
        row["cost"] = (row["input"] * pi + row["output"] * po + row["cache_read"] * pr + row["cache_write"] * pw) / 1e6
        self.log.append(row)
        self.last_usage = row
        if resp.parsed_output is None:
            raise RuntimeError(f"no parsed output (stop_reason={resp.stop_reason})")
        return resp.parsed_output


def get_provider(disabled: bool = False) -> Provider | None:
    if disabled:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    return AnthropicProvider()
