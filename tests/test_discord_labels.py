"""Every button the bot can render (§7b Discord: "No Discord button label ends in an ellipsis"). The site keeps its
ellipses (D1): words shared with it go through wizard.button_label where Discord renders them."""
import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import discord

import oibot_gm
from oibot_gm import appeals, discord_news, discord_policy, discord_pool, discord_registry, flows_test
from oibot_gm.raid_buttons import (RUN_WORDS, FillButton, LootButton, LootStartButton, PlaceButton, RunButton,
                                   SignupButton)

PY = Path(__file__).resolve().parents[1] / "src" / "oibot_gm"
ELLIPSIS = ("…", "...")


def _tails(node) -> list[str]:
    """Every literal end a label argument can take: a str / f-string's, both sides of a condition, each value of an
    `or` / `and`, the right side of a `+`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[-1], ast.Constant):
        return [node.values[-1].value]
    if isinstance(node, ast.IfExp):
        return _tails(node.body) + _tails(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [t for v in node.values for t in _tails(v)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _tails(node.right)
    return []


def _bad(source: str, where: str = "") -> list[str]:
    bad = []
    for n in ast.walk(ast.parse(source)):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "attr", getattr(n.func, "id", ""))
        args = ([n.args[0]] if name == "button" and n.args else []) + [k.value for k in n.keywords if k.arg == "label" and name != "Field"]
        bad += [f"{where}:{n.lineno} {t!r}" for a in args for t in _tails(a) if t.rstrip().endswith(ELLIPSIS)]
    return bad


def test_no_button_literal_ends_in_an_ellipsis():
    bad = []
    for f in PY.rglob("*.py"):
        if "web" in f.parts:
            continue
        bad += _bad(f.read_text(), f.name)
    assert not bad, bad


def test_the_scan_reads_through_a_condition_an_or_and_a_join():
    assert _bad('Button(label="Judging…" if busy else None)')
    assert _bad('Button(label=None if busy else "Judging…")')
    assert _bad('Button(label=name or "Pick one…")')
    assert _bad('Button(label="Clear " + "the rest…")')
    assert _bad('wiz.button(f"{verb}" + "…", go)')
    assert not _bad('Button(label="Judging" if busy else "Wait … ok")')


def test_every_persistent_button_renders_without_one():
    """Every DynamicItem[discord.ui.Button] in the package is rendered here: a new one fails until it is listed."""
    seen = set()
    for m in pkgutil.iter_modules(oibot_gm.__path__):
        mod = importlib.import_module(f"oibot_gm.{m.name}")
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if discord.ui.DynamicItem[discord.ui.Button] in getattr(cls, "__orig_bases__", ()) and cls.__module__.startswith("oibot_gm"):
                seen.add(cls)
    items = [RunButton("demo", "bd-abcdef", a) for a in RunButton.LABELS] + [LootButton("demo", "s", "-", k) for k in LootButton.LABELS]
    items += [LootStartButton("demo", "open"), flows_test.TestAnswerButton("demo", "bd-abcdef")]
    items += [appeals.AppealButton("demo", k, "0123456789ab") for k in appeals.AppealButton.LABELS]
    items += [discord_registry.ApplicationButton(a, 1, "demo") for a in ("accept", "decline")]  # (action, discord_id, gk)
    items += [discord_registry.RegisterButton(a, "demo") for a in discord_registry.RegisterButton.LABELS]
    items += [SignupButton("demo", "bd-abcdef", s) for s in SignupButton.STYLES]
    items += [FillButton("demo", "bd-abcdef", 1, a) for a in ("yes", "no")] + [PlaceButton("demo", "r", 1, a) for a in ("yes", "no")]
    items += [discord_policy.CfgApplyButton("demo", "tok12345", a) for a in discord_policy.CfgApplyButton.LABELS]
    items += [discord_news.ProposalButton("demo", "261125-abcd", a) for a in discord_news.ProposalButton.LABELS]
    items += [discord_pool.AbsenceButton(a, "demo") for a in (*discord_pool.AbsenceButton.LABELS, "clear")]
    items += [discord_pool.LootWishButton("demo", "an-item")]
    assert seen <= {type(i) for i in items}, f"a persistent button this test doesn't render: {seen - {type(i) for i in items}}"
    assert not [i.item.label for i in items if (i.item.label or "").endswith(ELLIPSIS)]


def test_the_sites_shared_words_keep_theirs():
    assert RUN_WORDS["fill"] == "Fill seats…", "the site's menu word (D1)"
    assert RunButton("demo", "bd-abcdef", "fill").item.label == "Fill seats"
