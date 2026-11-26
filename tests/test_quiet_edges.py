"""Edge states leave nothing cryptic behind (the owner's review of 2026-11-21, after "Expired — say it again."): a screen
nobody answered is DELETED — words only when Discord won't delete it, and then a full sentence saying what happened; a
timed button set loses its buttons instead of answering "This interaction failed"; a spent budget is said in its own
sentence on every path. No network, no Discord."""
from __future__ import annotations

import asyncio
import pathlib
import re
import types

from oibot_gm import answer_loop, configops, discord_policy, guild_picker, pending_views, wizard
from oibot_gm.llm.provider import BudgetExceeded

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "oibot_gm"


def test_a_restart_deletes_the_screens_that_changed_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_views, "PATH", tmp_path / "pending_views.jsonl")
    monkeypatch.setattr(pending_views, "_live", {})
    monkeypatch.setattr(pending_views, "ENABLED", True)
    pending_views.track("wiz:1", channel_id=7, message_id=8)
    pending_views.track("wiz:2", channel_id=7, message_id=9)
    pending_views.mark_saved("wiz:2")
    pending_views._live.clear()  # a new process
    deleted, edits = [], {}

    class Msg:
        def __init__(self, mid):
            self.mid = mid

        async def delete(self):
            deleted.append(self.mid)

        async def edit(self, **kw):
            edits[self.mid] = kw["content"]

    bot = types.SimpleNamespace(get_channel=lambda cid: types.SimpleNamespace(get_partial_message=Msg))
    assert asyncio.run(pending_views.restore_pending_views(bot)) == 2
    assert deleted == [8] and edits == {9: pending_views.RESTARTED_SAVED}, "what was saved is said; the rest leaves nothing"


def test_a_wizard_nobody_presses_deletes_its_screen():
    calls = []

    class Last:
        async def delete_original_response(self):
            calls.append("delete")

        async def edit_original_response(self, **kw):
            calls.append(kw["content"])

    w = wizard.Wizard.__new__(wizard.Wizard)
    w._closed, w._last, w.title, w.heading = False, Last(), "Absence", True
    asyncio.run(wizard.Wizard.on_timeout(w))
    assert calls == ["delete"]

    class Stuck(Last):
        async def delete_original_response(self):
            raise RuntimeError("gone")

    w._last = Stuck()
    asyncio.run(wizard.Wizard.on_timeout(w))
    assert len(calls) == 2 and calls[1].endswith(wizard.TIMED_OUT), "words only when it can't be deleted"


def test_a_timed_button_set_loses_its_buttons():
    edits = []

    class I:
        async def edit_original_response(self, **kw):
            edits.append(kw)

    view = types.SimpleNamespace()
    assert wizard.fade(view, I()) is view and wizard.fade(None, I()) is None
    asyncio.run(view.on_timeout())
    assert edits == [{"view": None}]


def test_a_spent_budget_is_its_own_sentence_on_every_path():
    e = BudgetExceeded("The bot's monthly budget for Claude is used up; the host raises it.")
    assert configops.user_error(e) == str(e), "a plain-text CHANGE read 'Something went wrong' here until 2026-11-21"
    assert configops.user_error(RuntimeError("boom")) == configops.GENERIC_ERROR


def test_edge_sentences_say_what_happened():
    texts = [discord_policy.TIMED_OUT, discord_policy.READING_TIMED_OUT, discord_policy.READING_DROPPED, discord_policy.EXPIRED_RESTART,
             discord_policy.BUTTON_GONE, pending_views.RESTARTED, pending_views.RESTARTED_SAVED, guild_picker.EXPIRED, wizard.TIMED_OUT,
             answer_loop.ANSWER_NOT_FOUND]
    for t in texts:
        assert t[0].isupper() and t.endswith(".") and not re.search(r"\b(expired|again|it)\b", t, re.I), t


def test_the_swept_strings_stay_gone():
    """The wording the review removed: a subject-less "that", a tail about what the reader need not do, a code word."""
    gone = ["Not configured here", "from another version", "thanks anyway", "This wizard expired",
            ". Nothing to do.", "— look again", "LLM provider is off", "no LLM configured", "Could you say that another way",
            "Something went wrong; ", "Run the command again"]
    for p in SRC.rglob("*.py"):
        text = p.read_text()
        for g in gone:
            assert g not in text, (p.name, g)
