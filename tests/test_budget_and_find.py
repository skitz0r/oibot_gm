"""The LLM budget is monthly and survives restarts, workloads open to everyone get a share, unknown models are priced
by family; a first name two characters share is refused instead of picking one."""
import json
from types import SimpleNamespace

import pytest

from oibot_gm.llm import provider as pv
from oibot_gm.registry import RegistryError


class FakeClient:
    def __init__(self, cost_tokens=1_000_000):
        self.n = cost_tokens
        self.messages = SimpleNamespace(parse=self.parse)

    def parse(self, **kw):
        u = SimpleNamespace(input_tokens=self.n, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(usage=u, parsed_output="ok", stop_reason="end_turn")


def make(tmp_path, monkeypatch, budget="5"):
    monkeypatch.setenv("OIBOT_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OIBOT_BUDGET_USD", budget)
    p = pv.AnthropicProvider.__new__(pv.AnthropicProvider)
    p.client, p.last_usage, p.name = FakeClient(), {}, "claude"
    p.budget_usd = float(budget)
    p.ledger = tmp_path / "usage.jsonl"
    p.log = p._this_month()
    return p


def test_price_by_family_and_unknown_is_dearest():
    assert pv.price_of("claude-opus-5-5") == pv.PRICES["claude-opus-5"]
    assert pv.price_of("claude-sonnet-5") == pv.PRICES["claude-sonnet-5"]
    assert pv.price_of("claude-haiku-4-5-20251001") == pv.PRICES["claude-haiku-4-5"]
    assert pv.price_of("some-new-model") == max(pv.PRICES.values())


def test_spend_survives_a_restart_and_help_has_a_share(tmp_path, monkeypatch):
    p = make(tmp_path, monkeypatch, budget="20")
    p.complete("roster_explain", "s", "u", object)  # 1M sonnet input tokens = $2
    assert p.cost_usd == pytest.approx(2.0)
    again = make(tmp_path, monkeypatch, budget="20")  # a restart reads the ledger back
    assert again.cost_usd == pytest.approx(2.0) and again.cap_for("help") == 10 and again.cap_for("loot_recommend") == 20
    monkeypatch.setenv("OIBOT_BUDGET_USD_HELP", "3")
    again.complete("help", "s", "u", object)  # help: $2 of its own $3 cap
    again.complete("help", "s", "u", object)  # $4 now: the check runs before a call
    with pytest.raises(pv.BudgetExceeded, match="share"):
        again.complete("help", "s", "u", object)
    rows = [json.loads(ln) for ln in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["month"] == pv.AnthropicProvider._month() for r in rows)


def test_last_months_spend_does_not_count(tmp_path, monkeypatch):
    (tmp_path / "usage.jsonl").write_text(json.dumps({"month": "1999-01", "workload": "help", "cost": 99}) + "\n")
    assert make(tmp_path, monkeypatch).cost_usd == 0


def test_find_refuses_a_shared_first_name(reg):
    a, b = reg.test_members()[:2]
    ca, cb = a.main, b.main
    ca.name, ca.surname, cb.name, cb.surname = "Jon", "Smith", "Jon", "Jones"
    assert reg.find("Jon Jones")[1] is cb and reg.find("jon smith")[1] is ca
    with pytest.raises(RegistryError, match="More than one character is called Jon"):
        reg.find("Jon")
