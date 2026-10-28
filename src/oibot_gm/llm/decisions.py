"""The decision provider (design §5.59): one yes/no judgment as a probability, from TypeSafe's decisions API (Jev).

It never writes text and never acts: `noul()` returns a number between 0 and 1 (or None), and code decides what follows.
Raw HTTP through httpx (no SDK). The key is `TYPESAFE_API_KEY` in .env — never printed, never in a log or an ops line.
Usage goes through the SAME ledger as Claude (`llm/provider.py`: out/llm_usage.jsonl, one shared monthly cap, the
workload's share in SHARES, rows per guild via `for_guild`)."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from .provider import CURRENT_GUILD, price_of

log = logging.getLogger(__name__)

KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
WARN_EVERY_S = 600  # a failure's warning is logged at most once per reason per 10 minutes


class DecisionProvider:
    """`await provider.noul(state, instructions, criteria)` → p in [0, 1], or None when the key is missing, the request
    failed (timeout, 4xx / 5xx, bad JSON) or the monthly cap refused. Never raises to the caller.

    `ledger`: the running AnthropicProvider (or anything with `_record`, `cap_for`, `cost_usd`, `_month`, `budget_usd`,
    `spent`) — its rows and its cap are shared. None = no ledger: nothing recorded, no cap (the per-hour cap of the caller
    still applies)."""

    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL, model: str = MODEL, timeout: float = 4.0,
                 ledger: Any = None, transport: httpx.AsyncBaseTransport | None = None):
        self._key = api_key if api_key is not None else os.environ.get(KEY_ENV, "")
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.ledger = ledger
        self._transport = transport  # tests: httpx.MockTransport
        self._warned: dict[str, float] = {}
        self.calls = 0  # requests actually sent (tests, the host's log)

    @property
    def available(self) -> bool:
        return bool(self._key)

    def _warn(self, reason: str, msg: str, *args) -> None:
        now = time.monotonic()
        if now - self._warned.get(reason, -1e9) >= WARN_EVERY_S:
            self._warned[reason] = now
            log.warning(msg, *args)

    def _capped(self, workload: str) -> bool:
        led = self.ledger
        if led is None:
            return False
        try:
            if led.cost_usd >= led.budget_usd:
                return True
            spent = led.spent(workload) if hasattr(led, "spent") else 0.0
            return spent >= led.cap_for(workload)
        except Exception:  # noqa: BLE001 — a ledger that can't answer never stops the decision
            return False

    def _record(self, workload: str, model: str, usage: dict) -> None:
        led = self.ledger
        if led is None:
            return
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        pi, po, _pr, _pw = price_of(model)
        row = {"month": led._month(), "guild": CURRENT_GUILD.get(), "workload": workload, "model": model,
               "input": tin, "output": tout, "cache_read": 0, "cache_write": 0, "cost": (tin * pi + tout * po) / 1e6}
        try:
            led._record(row)
        except Exception as e:  # noqa: BLE001
            self._warn("ledger", "decision usage not recorded (%s)", type(e).__name__)

    async def noul(self, state, instructions: str, criteria: dict | None = None, workload: str = "listen") -> float | None:
        """One request, one yes/no question. `state` is the data (a string or a JSON object: member text stays data);
        `instructions` the whole question; `criteria` {"true": …, "false": …}."""
        if not self._key:
            return None
        if self._capped(workload):
            self._warn("cap", "decision workload %s refused: the monthly budget or its share is used up", workload)
            return None
        q: dict = {"type": "noul", "instructions": instructions}
        if criteria:
            q["criteria"] = {"true": criteria.get("true", ""), "false": criteria.get("false", "")}
        body = {"model": self.model, "state": state, "questions": {"decision": q}}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                self.calls += 1
                resp = await client.post(self.base_url, json=body, headers=headers)
        except httpx.TimeoutException:
            self._warn("timeout", "decision request timed out after %.1fs", self.timeout)
            return None
        except httpx.HTTPError as e:
            self._warn("http", "decision request failed (%s)", type(e).__name__)
            return None
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("detail", ""))[:200]
            except (ValueError, AttributeError):
                pass
            self._warn(f"status{resp.status_code}", "decision request refused: HTTP %s %s", resp.status_code, detail)
            return None
        try:
            data = resp.json()
            p = float(data["answers"]["decision"]["noul"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._warn("json", "decision answer unreadable")
            return None
        self._record(workload, str(data.get("model") or self.model), data.get("usage") or {})
        if not 0.0 <= p <= 1.0:
            self._warn("range", "decision answer out of range: %s", p)
            return None
        return p


DECISIONS: DecisionProvider | None = None  # set at bot start (discord_bot.run / OibotGM.setup); None when no key is set


def get_decisions(ledger: Any = None) -> DecisionProvider | None:
    """The provider when TYPESAFE_API_KEY is set, else None."""
    if not os.environ.get(KEY_ENV):
        return None
    return DecisionProvider(ledger=ledger)


def key_set() -> bool:
    """Whether the host set TYPESAFE_API_KEY (the running provider or the environment) — a yes or no, never the value.
    The Ops page's `listen_ready`, the Host card's TypeSafe line and the plain op's diff read it."""
    return bool((DECISIONS is not None and getattr(DECISIONS, "available", True)) or os.environ.get(KEY_ENV))
