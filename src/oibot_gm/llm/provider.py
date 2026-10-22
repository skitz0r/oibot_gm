"""Provider boundary: `complete(system, user, schema) -> schema instance`.

Everything above this line is provider-neutral. Only the Anthropic adapter
exists today; the routing table maps workloads to models so a cheaper model
can be substituted per workload without touching callers."""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
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
    "help": ("claude-sonnet-5", "low"),             # "how does X work?" from the manual + live state (cached prefix)
}

# $ per million tokens: (input, output, cache read, cache write)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}


# A workload's share of the monthly budget when OIBOT_BUDGET_USD_<WORKLOAD> doesn't set its own cap: questions and
# plain-text parsing are open to many people, so a flood of them can't spend the money loot night needs.
SHARES: dict[str, float] = {"help": 0.5, "config_change": 0.5}

log = logging.getLogger(__name__)

# D45: ONE shared monthly cap, spend shown PER GUILD. The guild a call is for travels in a context variable (asyncio and
# asyncio.to_thread copy it), set by the surfaces that know it (help answers, plain-text parsing, policy compiles); a
# call made outside any guild's context is recorded with guild "" (unattributed).
CURRENT_GUILD: contextvars.ContextVar[str] = contextvars.ContextVar("oibot_llm_guild", default="")


@contextlib.contextmanager
def for_guild(key: str | None):
    """`with for_guild(reg.key): provider.complete(...)` — the usage row is that guild's (nesting restores the outer one)."""
    token = CURRENT_GUILD.set(key or "")
    try:
        yield
    finally:
        CURRENT_GUILD.reset(token)


def price_of(model: str) -> tuple[float, float, float, float]:
    """Exact id, else the model's family (claude-opus-5-5 → claude-opus-5); an unknown family is priced like the
    dearest one and logged, so the cap errs towards stopping early rather than overspending."""
    if model in PRICES:
        return PRICES[model]
    family = next((k for k in sorted(PRICES, key=len, reverse=True) if model.startswith(k.rsplit("-", 1)[0] + "-")), None)
    if family:
        return PRICES[family]
    log.warning("no price for model %s; counting it at the dearest known rate", model)
    return max(PRICES.values())


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
        self.budget_usd = float(os.environ.get("OIBOT_BUDGET_USD", "5"))  # per calendar month (UTC)
        self.ledger = Path(os.environ.get("OIBOT_USAGE_LOG", "out/llm_usage.jsonl"))
        self.log: list[dict] = self._this_month()  # one row per call: workload, model, tokens, cost (survives restarts)

    @staticmethod
    def _month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _this_month(self) -> list[dict]:
        """The ledger's rows for the current month: a restart no longer resets the spend."""
        try:
            rows = [json.loads(ln) for ln in self.ledger.read_text().splitlines() if ln.strip()]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("usage ledger unreadable (%s); counting from zero", e)
            return []
        return [r for r in rows if r.get("month") == self._month()]

    def _record(self, row: dict) -> None:
        self.log = [r for r in self.log if r.get("month") == row["month"]] + [row]  # a new month starts from zero
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger.open("a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            log.warning("usage ledger not written (%s)", e)

    def cap_for(self, workload: str) -> float:
        own = os.environ.get(f"OIBOT_BUDGET_USD_{workload.upper()}")
        return float(own) if own else self.budget_usd * SHARES.get(workload, 1.0)

    def route(self, workload: str) -> tuple[str, str]:
        model, effort = ROUTES[workload]
        return os.environ.get(f"OIBOT_MODEL_{workload.upper()}", model), effort

    @property
    def cost_usd(self) -> float:
        month = self._month()
        return sum(r["cost"] for r in self.log if r.get("month") == month)

    def spent(self, workload: str) -> float:
        month = self._month()
        return sum(r["cost"] for r in self.log if r["workload"] == workload and r.get("month") == month)

    def usage_by_guild(self) -> dict[str, float]:
        """This month's spend per guild key ('' = unattributed: calls made outside a guild's context, and rows from
        before rows carried a guild). The cap stays shared (D45); this is what the Agents page shows the owner."""
        month = self._month()
        out: dict[str, float] = {}
        for r in self.log:
            if r.get("month") == month:
                out[r.get("guild") or ""] = out.get(r.get("guild") or "", 0.0) + float(r.get("cost") or 0.0)
        return out

    def summary(self) -> str:
        by: dict[str, list[dict]] = {}
        for r in self.log:
            by.setdefault(r["workload"], []).append(r)
        parts = [f"{w}: {len(rs)} calls, {sum(r['input']+r['cache_read'] for r in rs)/1000:.0f}K in / {sum(r['output'] for r in rs)/1000:.1f}K out, ${sum(r['cost'] for r in rs):.2f}" for w, rs in by.items()]
        return f"LLM spend this month ${self.cost_usd:.2f} of ${self.budget_usd:.0f} · " + " · ".join(parts) if parts else "no LLM calls this month"

    def complete(self, workload: str, system: str, user: str, schema: type[T]) -> T:
        if self.cost_usd >= self.budget_usd:  # the words a guild's people read: the host raises it, never "set an env var" (§3.5)
            raise BudgetExceeded("The bot's monthly budget for Claude is used up; the host raises it. Slash commands, the sheets and the site keep working.")
        if self.spent(workload) >= self.cap_for(workload):  # the variable is for the host's log, never a guild's reply
            log.warning("LLM workload %s used its share ($%.2f of $%.2f); the host raises OIBOT_BUDGET_USD_%s",
                        workload, self.spent(workload), self.cap_for(workload), workload.upper())
            raise BudgetExceeded("This kind of request has used its share of the bot's monthly Claude budget; the host raises it. "
                                 "Slash commands, the sheets and the site keep working.")
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
            "month": self._month(),
            "guild": CURRENT_GUILD.get(),
            "workload": workload,
            "model": model,
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        pi, po, pr, pw = price_of(model)
        row["cost"] = (row["input"] * pi + row["output"] * po + row["cache_read"] * pr + row["cache_write"] * pw) / 1e6
        self._record(row)
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
