"""The shared confirm screen (phase 5, §7b on Discord): Wizard.decide needs a verb, Wizard.decide_box prints the
server's box as given, re-checks it at the press, and no wizard button ends in an ellipsis."""
import asyncio
import inspect

import discord

from oibot_gm import consequences as cq
from oibot_gm.constants import CONFIRM_WORDS
from oibot_gm.wizard import Field, Wizard, box_text, button_label, lint, minute_opts, when_fields
from wizard_harness import Harness


def run(c):
    return asyncio.run(c)


def test_decide_needs_a_verb_and_the_note_button_is_verb_with_a_note(reg):
    assert inspect.signature(Wizard.decide).parameters["do_label"].default is inspect.Parameter.empty
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")

    async def do(i, note):
        pass
    run(wiz.decide(h.command(), "It moves.", do=do, do_label="Move", note={"label": "Note"}))
    assert h.buttons() == ["Move", "Move with a note", "Back"]


def test_the_box_prints_title_diff_sentences_and_its_verb(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="Edit event")
    b = cq.box("Change Barrow Deeps on Tue 9 Dec?", ["It locks a day before.", "Nobody is told."], "Save",
               diff=["Locks: 2 hours before → 24 hours before"])

    async def do(i, v):
        pass
    run(wiz.decide_box(h.command(), b, do=do))
    t = h.text
    assert t.index("**Change Barrow Deeps") < t.index("Locks: 2 hours") < t.index("It locks a day before.")
    assert "**Edit event**" not in t, "one heading: the box's title"
    assert h.buttons() == ["Save", "Back"]
    assert h.screen["view"].children[0].style is discord.ButtonStyle.success


def test_box_text_adds_and_rewords_nothing():
    b = cq.box("Move X?", ["It moves.", "Nobody is told."], "Move", diff=["Starts: 7:00 PM → 8:00 PM"])
    assert box_text(b) == "**Move X?**\nStarts: 7:00 PM → 8:00 PM\nIt moves.\nNobody is told."
    assert box_text(b, changed=True) == CONFIRM_WORDS["changed"] + "\n" + box_text(b)


def test_a_danger_box_is_red_and_a_refusal_has_back_alone(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")

    async def do(i, v):
        pass
    run(wiz.decide_box(h.command(), cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True), do=do))
    assert h.screen["view"].children[0].style is discord.ButtonStyle.danger
    run(wiz.decide_box(h.command(), cq.refusal_box("That time is taken", "Barrow Deeps already has an event then."), do=do))
    assert h.buttons() == ["Back"] and "already has an event then." in h.text


def test_back_finishes_or_goes_to_the_screen_given(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    b = cq.box("Move X?", ["It moves."], "Move")

    async def do(i, v):
        pass
    run(wiz.decide_box(h.command(), b, do=do))
    run(h.press("Back"))
    assert "Nothing was changed." in h.text and h.screen["view"] is None
    went = []

    async def back(i):
        went.append(True)
        await wiz.show(i, "The picker.")
    wiz2 = Wizard(reg, h.user.id, title="T")
    run(wiz2.decide_box(h.command(), b, do=do, back=back))
    run(h.press("Back"))
    assert went == [True] and "The picker." in h.text


def test_a_box_that_changed_is_shown_again_and_writes_nothing(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    calls, now = [], {"b": cq.box("Move X?", ["It moves to 8:00 PM."], "Move")}

    async def do(i, v):
        calls.append(v)
        await wiz.finish(i, "done")
    shown = now["b"]
    run(wiz.decide_box(h.command(), shown, do=do, refresh=lambda: now["b"]))
    now["b"] = cq.box("Move X?", ["It moves to 9:00 PM."], "Move")
    run(h.press("Move"))
    assert calls == [] and h.text.startswith(CONFIRM_WORDS["changed"]) and "9:00 PM" in h.text
    run(h.press("Move"))
    assert calls == [{}]


def test_an_async_refresh_and_a_fresh_refusal(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    calls = []

    async def do(i, v):
        calls.append(v)

    async def refresh():
        return cq.refusal_box("This event is over", "This event has finished.")
    run(wiz.decide_box(h.command(), cq.box("Move X?", ["It moves."], "Move"), do=do, refresh=refresh))
    run(h.press("Move"))
    assert calls == [] and h.buttons() == ["Back"] and "This event has finished." in h.text


def test_the_note_form_is_the_write_and_rechecks_the_box(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    calls = []

    async def do(i, v):
        calls.append(v)
        await wiz.finish(i, "done")
    b = cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True)
    run(wiz.decide_box(h.command(), b, do=do, refresh=lambda: b,
                       note=[Field("reason", "Why? (optional)", required=False, paragraph=True)]))
    run(h.press("Cancel event with a note"))
    assert h.modal.title == "Cancel event with a note"
    run(h.submit(reason="Half the raid is sick."))
    assert calls == [{"reason": "Half the raid is sick."}]


def test_the_note_submit_shows_a_changed_box_instead_of_writing(reg):
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    calls, now = [], {"b": cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True)}

    async def do(i, v):
        calls.append(v)
    run(wiz.decide_box(h.command(), now["b"], do=do, refresh=lambda: now["b"], note=[Field("reason", "Why?", required=False)]))
    run(h.press("Cancel event with a note"))
    now["b"] = cq.box("Cancel X?", ["It goes, and two raids with it."], "Cancel event", danger=True)
    run(h.submit(reason="x"))
    assert calls == [] and h.text.startswith(CONFIRM_WORDS["changed"])


def test_button_label_and_lint():
    assert button_label("Fill seats…") == "Fill seats" and button_label("Move it...") == "Move it" and button_label("Lock") == "Lock"
    assert button_label("Wait … ok") == "Wait … ok"
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Remove…"))
    assert any("ellipsis" in e for e in lint(v))


def test_wizard_button_drops_the_ellipsis(reg):
    wiz = Wizard(reg, 1, title="T")

    async def p(i):
        pass
    assert wiz.button("Pick the recipe…", p).label == "Pick the recipe"


def test_when_fields_takes_a_minute_step(reg):
    from datetime import date
    fields = when_fields(reg, day0=date(2030, 1, 7), step=15)
    assert [o.value for o in fields[2].options] == [o.value for o in minute_opts(15)] == ["0", "15", "30", "45"]


def test_a_refresh_that_refuses_shows_a_refusal_box_and_writes_nothing(reg):
    from oibot_gm.registry import RegistryError
    h = Harness(reg)
    wiz = Wizard(reg, h.user.id, title="T")
    calls = []

    async def do(i, v):
        calls.append(v)

    def refresh():
        raise RegistryError("That event is gone.")
    run(wiz.decide_box(h.command(), cq.box("Move X?", ["It moves."], "Move"), do=do, refresh=refresh))
    run(h.press("Move"))
    assert calls == [] and h.buttons() == ["Back"] and "That event is gone." in h.text
    assert not h.text.startswith(CONFIRM_WORDS["changed"])


def test_the_note_submit_refused_by_the_guard_finishes_and_writes_nothing(reg):
    h = Harness(reg)
    calls, why = [], {"v": None}

    async def guard(i):
        return why["v"]
    wiz = Wizard(reg, h.user.id, title="T", guard=guard)

    async def do(i, v):
        calls.append(v)
    b = cq.box("Cancel X?", ["It goes."], "Cancel event", danger=True)
    run(wiz.decide_box(h.command(), b, do=do, refresh=lambda: b, note=[Field("reason", "Why?", required=False)]))
    run(h.press("Cancel event with a note"))
    why["v"] = "Only an officer can cancel an event."
    run(h.submit(reason="x"))
    assert calls == [] and "Only an officer can cancel an event." in h.text and h.screen["view"] is None


def test_a_verb_with_an_ellipsis_is_taken_once(reg):
    h = Harness(reg)

    async def do(i, v):
        pass
    wiz = Wizard(reg, h.user.id, title="T")
    run(wiz.decide(h.command(), "It moves.", do=do, do_label="Move…", note={"label": "Note"}))
    assert h.buttons() == ["Move", "Move with a note", "Back"]
    run(h.press("Move with a note"))
    assert h.modal.title == "Move with a note"
    wiz2 = Wizard(reg, h.user.id, title="T")
    run(wiz2.decide_box(h.command(), cq.box("Move X?", ["It moves."], "Move…"), do=do, note=[Field("n", "Note", required=False)]))
    assert h.buttons() == ["Move", "Move with a note", "Back"]
    run(h.press("Move with a note"))
    assert h.modal.title == "Move with a note"


def test_lint_reads_a_persistent_buttons_own_label():
    from oibot_gm.raid_buttons import LootButton
    v = discord.ui.View(timeout=None)
    b = LootButton("demo", "s", "-", next(iter(LootButton.LABELS)))
    b.item.label = "Override…"
    v.add_item(b)
    assert any("ellipsis" in e for e in lint(v))
