"""The decision provider (design §5.59): one yes/no judgment as a probability from TypeSafe's decisions API, through a fake
httpx transport — never the network. Failures answer None (never raise); usage rows land in Claude's ledger at the jev
price; the shared monthly cap and the listen share refuse before a request is sent."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from oibot_gm.llm import decisions, provider
from oibot_gm.llm.decisions import DecisionProvider


class Ledger:
    """What DecisionProvider needs of AnthropicProvider (its _record / cap_for / cost_usd / spent / _month / budget_usd)."""

    budget_usd = 5.0

    def __init__(self):
        self.log: list[dict] = []

    @staticmethod
    def _month():
        return "2026-10"

    def _record(self, row):
        self.log.append(row)

    def cap_for(self, workload):
        return self.budget_usd * provider.SHARES.get(workload, 1.0)

    @property
    def cost_usd(self):
        return sum(r["cost"] for r in self.log)

    def spent(self, workload):
        return sum(r["cost"] for r in self.log if r["workload"] == workload)


def answer(p=0.87, tokens=312, model="jev-1.13.0"):
    return {"model": model, "answers": {"decision": {"type": "noul", "noul": p}}, "usage": {"input_tokens": tokens, "output_tokens": 0}}


def make(handler, ledger=None, key="ts-test-key"):
    seen: list[httpx.Request] = []

    def h(request):
        seen.append(request)
        return handler(request)

    return DecisionProvider(api_key=key, ledger=ledger, transport=httpx.MockTransport(h)), seen


def ask(dp, state="hello"):
    return asyncio.run(dp.noul(state, "Is this for the bot?", {"true": "yes", "false": "no"}))


def test_a_noul_answer_is_parsed_and_the_request_is_the_documented_shape():
    dp, seen = make(lambda r: httpx.Response(200, json=answer(0.87)))
    assert ask(dp, {"message": {"text": "when is the raid?"}}) == pytest.approx(0.87)
    body = json.loads(seen[0].content)
    assert seen[0].url == httpx.URL(decisions.BASE_URL) and seen[0].headers["authorization"] == "Bearer ts-test-key"
    assert body["model"] == "jev-latest" and body["state"] == {"message": {"text": "when is the raid?"}}
    q = body["questions"]["decision"]
    assert q == {"type": "noul", "instructions": "Is this for the bot?", "criteria": {"true": "yes", "false": "no"}}


@pytest.mark.parametrize("status", [400, 422, 500])
def test_a_refused_request_is_none(status):
    dp, _ = make(lambda r: httpx.Response(status, json={"detail": "bad state"}))
    assert ask(dp) is None


def test_a_timeout_is_none():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    dp, _ = make(boom)
    assert ask(dp) is None


def test_bad_json_and_out_of_range_are_none():
    dp, _ = make(lambda r: httpx.Response(200, content=b"not json"))
    assert ask(dp) is None
    dp, _ = make(lambda r: httpx.Response(200, json={"answers": {}}))
    assert ask(dp) is None
    dp, _ = make(lambda r: httpx.Response(200, json=answer(1.7)))
    assert ask(dp) is None


def test_no_key_is_none_without_a_request(monkeypatch):
    monkeypatch.delenv(decisions.KEY_ENV, raising=False)
    dp, seen = make(lambda r: httpx.Response(200, json=answer()), key="")
    assert not dp.available and ask(dp) is None and seen == [] and dp.calls == 0
    assert decisions.get_decisions() is None
    monkeypatch.setenv(decisions.KEY_ENV, "x")
    assert decisions.get_decisions().available


def test_usage_is_recorded_at_the_jev_price_per_guild():
    led = Ledger()
    dp, _ = make(lambda r: httpx.Response(200, json=answer(0.2, tokens=1_000_000)), ledger=led)
    with provider.for_guild("oi"):
        assert ask(dp) == pytest.approx(0.2)
    row = led.log[0]
    assert row["workload"] == "listen" and row["model"] == "jev-1.13.0" and row["guild"] == "oi" and row["month"] == "2026-10"
    assert row["input"] == 1_000_000 and row["output"] == 0 and row["cost"] == pytest.approx(0.042)
    assert provider.price_of("jev-1.13.0") == provider.PRICES["jev-latest"] and provider.SHARES["listen"] == 0.2


def test_the_cap_refuses_before_a_request():
    led = Ledger()
    led.log.append({"workload": "listen", "cost": 1.0, "month": "2026-10"})  # the share: 0.2 × $5 = $1, used up
    dp, seen = make(lambda r: httpx.Response(200, json=answer()), ledger=led)
    assert ask(dp) is None and seen == []
    led.log[:] = [{"workload": "help", "cost": 5.0, "month": "2026-10"}]  # the whole month's budget
    assert ask(dp) is None and seen == []
    led.log.clear()
    assert ask(dp) == pytest.approx(0.87) and len(seen) == 1
