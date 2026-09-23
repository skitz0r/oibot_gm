"""The registration wizards driven end to end: class → spec → offspec → names (register / card / /me char add),
plan only, apply, and the character flows (name · spec · main · retire). Classes come from the profile."""
import asyncio
from types import SimpleNamespace

import discord

from oibot_gm import flows_register  # noqa: F401 — registers the flows
from oibot_gm.wizard import FLOWS
from wizard_harness import Harness

NEW_ID = 7_777_000_001


class FakeOps:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    async def emit(self, cfg, level, text):
        self.lines.append((level, text))


def harness(reg, *, uid=NEW_ID, name="Newbie"):
    h = Harness(reg)
    h.user = SimpleNamespace(id=uid, display_name=name, mention=f"@{name}")
    h.posted = []

    async def post_application(r, a):
        h.posted.append(a)
        return "Posted to the applications channel."
    h.client = SimpleNamespace(ico=lambda kind, key: "", ops=FakeOps(), post_application=post_application,
                               raids=SimpleNamespace(store=lambda _r: None))
    return h


def cls_specs(reg, n=3):
    """A class from the profile with at least `n` specs, and its specs (never a hardcoded list)."""
    cls = next(c for c, s in reg.profile.classes.items() if len(s) >= n)
    return cls, list(reg.profile.classes[cls])


async def pick_class_specs(h, cls, spec, off=None):
    await h.pick("Class", cls)
    offered = [o.value for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Main spec" for o in s.options]
    await h.pick("Main spec", spec)
    if off:
        await h.pick("Offspec (optional)", off)
    return offered


def run(coro):
    asyncio.run(coro)


# ---- register

def test_register_a_main_end_to_end(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.card_press(), reg, slot="main")
        assert h.selects() == ["Class", "Main spec", "Offspec (optional)"] and h.buttons() == ["Continue", "Cancel"]
        assert [o.value for o in h._items()[0].options] == list(reg.profile.classes)
        offered = await pick_class_specs(h, cls, specs[0], specs[1])
        assert offered == specs
        await h.press("Continue")
        assert h.modal is not None and set(h.modal._items) == {"first", "last"}
        first = h.modal._items["first"]
        assert (first.min_length, first.max_length) == (2, 12)
        await h.submit(first="Aelia", last="Stormborn")
        assert "✅ Registered main" in h.text and "Aelia Stormborn" in h.text and h.screen["view"] is None
    run(go())
    m = reg.members[NEW_ID]
    c = m.main
    assert (c.name, c.surname, c.cls, c.spec, c.offspec, c.status) == ("Aelia", "Stormborn", cls, specs[0], specs[1], "active")
    assert any("registered Aelia Stormborn" in t for _, t in h.client.ops.lines)


def test_blank_first_name_plans_the_main(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg)
        await pick_class_specs(h, cls, specs[2])
        await h.press("Continue")
        await h.submit(first="", last="")
        assert "✅ Planned main" in h.text and "/me char name" in h.text
    run(go())
    c = reg.members[NEW_ID].main
    assert c.status == "planned" and c.name is None and (c.cls, c.spec, c.offspec) == (cls, specs[2], None)


def test_bad_name_is_refused_and_fix_this_keeps_the_draft(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg)
        await pick_class_specs(h, cls, specs[0], specs[1])
        await h.press("Continue")
        await h.submit(first="Bad Name1", last="Okay")
        assert "❌" in h.text and "2–12 letters" in h.text and h.buttons() == ["Fix this", "Cancel"]
        await h.press("Fix this")
        assert h.modal._items["first"].default == "Bad Name1" and h.modal._items["last"].default == "Okay"
        assert h.modal.title.startswith(f"{cls} {specs[0]}")
        await h.submit(h.modal, first="Goodname", last="Okay")
        assert "✅ Registered main" in h.text
    run(go())
    c = reg.members[NEW_ID].main
    assert c.label == "Goodname Okay" and (c.cls, c.spec, c.offspec) == (cls, specs[0], specs[1])


def test_add_an_alt_with_a_surname(reg):
    m = reg.test_members()[0]
    old_main = m.main.label
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, slot="alt")  # /me char add
        assert "Add an alt" in h.text
        await pick_class_specs(h, cls, specs[1])
        await h.press("Continue")
        await h.submit(first="Twoword", last="Altname")
        assert "✅ Registered alt" in h.text
    run(go())
    m = reg.members[m.discord_id]
    alt = next(c for c in m.active() if c.label == "Twoword Altname")
    assert not alt.is_main and alt.rank == "alt" and m.main.label == old_main


def test_plan_only_skips_the_name_step(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, slot="main", plan_only=True)
        assert h.buttons() == ["Confirm…", "Cancel"]
        await pick_class_specs(h, cls, specs[0], specs[2])
        await h.press("Confirm…")
        assert h.modal is None and h.buttons() == ["Confirm", "Change", "Cancel"]
        assert NEW_ID not in reg.members  # nothing written before Confirm
        await h.press("Confirm")
        assert "✅ Planned main" in h.text
    run(go())
    c = reg.members[NEW_ID].main
    assert c.status == "planned" and (c.spec, c.offspec) == (specs[0], specs[2])


def test_cancel_writes_nothing(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)
    commits = len(reg.store.log()) if hasattr(reg.store, "log") else None

    async def go():
        await FLOWS["register"](h.command(), reg)
        await pick_class_specs(h, cls, specs[0])
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    run(go())
    assert NEW_ID not in reg.members
    if commits is not None:
        assert len(reg.store.log()) == commits


def test_changing_class_resets_the_spec(reg):
    h = harness(reg)
    classes = list(reg.profile.classes)

    async def go():
        await FLOWS["register"](h.command(), reg)
        await h.pick("Class", classes[0])
        await h.pick("Main spec", list(reg.profile.classes[classes[0]])[0])
        await h.pick("Class", classes[1])
        spec_sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Main spec")
        assert [o.value for o in spec_sel.options] == list(reg.profile.classes[classes[1]]) and not any(o.default for o in spec_sel.options)
        assert next(b for b in h._items() if isinstance(b, discord.ui.Button) and b.label == "Continue").disabled
    run(go())


# ---- apply

def test_apply_calls_the_card_hook(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, mode="apply")
        await pick_class_specs(h, cls, specs[0], specs[1])
        await h.press("Continue")
        assert set(h.modal._items) == {"name", "logs", "times", "about"}
        await h.submit(name="Hopeful", logs="https://logs.example/x", times="Tue and Thu evenings", about="Raided a lot")
        assert "✅ Application received for **Hopeful**" in h.text and "applications channel" in h.text
    run(go())
    a = h.posted[0]
    assert (a.name, a.cls, a.spec, a.offspec, a.logs_url, a.availability, a.about) == ("Hopeful", cls, specs[0], specs[1], "https://logs.example/x", "Tue and Thu evenings", "Raided a lot")
    assert reg.applicants[NEW_ID].status == "open"


def test_apply_refusal_reopens_the_form_filled(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)

    async def go():
        await FLOWS["register"](h.command(), reg, mode="apply")
        await pick_class_specs(h, cls, specs[0])
        await h.press("Continue")
        await h.submit(name="No1", about="hello")
        assert "❌" in h.text and not h.posted
        await h.press("Fix this")
        assert h.modal._items["name"].default == "No1" and h.modal._items["about"].default == "hello"
    run(go())
    assert NEW_ID not in reg.applicants


def test_a_member_cannot_apply(reg):
    m = reg.test_members()[0]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    run(FLOWS["register"](h.command(), reg, mode="apply"))
    assert "already a member" in h.text and h.screen["view"] is None


# ---- /me char name

def test_char_name_names_the_planned_main(reg):
    h = harness(reg)
    cls, specs = cls_specs(reg)
    reg.set_plan(NEW_ID, "Newbie", cls, specs[0], None, "main")

    async def go():
        await FLOWS["char_name"](h.command(), reg)
        assert h.modal is not None and h.modal._items["first"].required  # one planned: straight to the names
        await h.submit(first="bad1", last="")
        assert "❌" in h.text
        await h.press("Fix this")
        assert h.modal._items["first"].default == "bad1"
        await h.submit(h.modal, first="Launchday", last="Hero")
        assert "✅" in h.text and "Launchday Hero" in h.text
    run(go())
    c = reg.members[NEW_ID].main
    assert c.status == "active" and c.label == "Launchday Hero"


def test_char_name_with_nothing_planned(reg):
    m = next(m for m in reg.test_members() if not m.planned())
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    run(FLOWS["char_name"](h.command(), reg))
    assert "no planned character" in h.text


# ---- /me char spec

def test_char_spec_offers_only_that_class(reg):
    m = reg.test_members()[0]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = cls_specs(reg)
    other = next(c for c in reg.profile.classes if c != cls)
    reg.add_character(m.discord_id, m.display_name, "Speccy", other, list(reg.profile.classes[other])[0], None, False)

    async def go():
        await FLOWS["char_spec"](h.command(), reg)
        assert h.selects() == ["Character"]
        await h.pick("Character", "Speccy")
        spec_sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == "Main spec")
        assert [o.value for o in spec_sel.options] == list(reg.profile.classes[other])
        new = list(reg.profile.classes[other])[-1]
        await h.pick("Main spec", new)
        await h.press("Confirm")
        assert "✅" in h.text
    run(go())
    c = next(c for c in reg.members[m.discord_id].active() if c.label == "Speccy")
    assert c.spec == list(reg.profile.classes[other])[-1] and c.cls == other


def test_char_spec_cancel_writes_nothing(reg):
    m = reg.test_members()[1]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    before = [(c.label, c.spec, c.offspec) for c in m.active()]

    async def go():
        await FLOWS["char_spec"](h.command(), reg)
        if h.selects() == ["Character"]:
            await h.pick("Character", m.main.label)
        await h.press("Cancel")
    run(go())
    assert [(c.label, c.spec, c.offspec) for c in reg.members[m.discord_id].active()] == before


# ---- /me char main and retire

def test_char_main_swaps_the_main(reg):
    m = reg.test_members()[2]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = cls_specs(reg)
    old = m.main.label
    reg.add_character(m.discord_id, m.display_name, "Newmain", cls, specs[0], None, False)

    async def go():
        await FLOWS["char_main"](h.command(), reg)
        if h.selects() == ["Character"]:
            assert old not in [o.value for o in h._items()[0].options]  # the main is not a candidate
            await h.pick("Character", "Newmain")
        assert h.buttons() == ["Confirm", "Cancel"]
        await h.press("Confirm")
        assert "✅ Main is now" in h.text and f"was {old}" in h.text
    run(go())
    assert reg.members[m.discord_id].main.label == "Newmain"


def test_char_main_refuses_when_the_pick_went_away(reg):
    m = reg.test_members()[3]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = cls_specs(reg)
    reg.add_character(m.discord_id, m.display_name, "Fleeting", cls, specs[0], None, False)
    main_before = m.main.label

    async def go():
        await FLOWS["char_main"](h.command(), reg)
        if h.selects() == ["Character"]:
            await h.pick("Character", "Fleeting")
        reg.retire(m.discord_id, "Fleeting")  # meanwhile, on the website
        await h.press("Confirm")
        assert "❌" in h.text and "no longer" in h.text
    run(go())
    assert reg.members[m.discord_id].main.label == main_before


def test_char_retire(reg):
    m = reg.test_members()[4]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    cls, specs = cls_specs(reg)
    reg.add_character(m.discord_id, m.display_name, "Oldtimer", cls, specs[0], None, False)

    async def go():
        await FLOWS["char_retire"](h.command(), reg)
        await h.pick("Character", "Oldtimer")
        btn = next(b for b in h._items() if isinstance(b, discord.ui.Button) and b.label == "Retire Oldtimer")
        assert btn.style == discord.ButtonStyle.danger
        await h.press("Retire Oldtimer")
        assert "✅ Retired Oldtimer" in h.text
    run(go())
    assert next(c for c in reg.members[m.discord_id].characters if c.label == "Oldtimer").status == "retired"


def test_char_retire_cancel_writes_nothing(reg):
    m = reg.test_members()[5]
    h = harness(reg, uid=m.discord_id, name=m.display_name)
    before = [c.status for c in m.characters]

    async def go():
        await FLOWS["char_retire"](h.command(), reg)
        if h.selects() == ["Character"]:
            await h.pick("Character", m.main.label)
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    run(go())
    assert [c.status for c in reg.members[m.discord_id].characters] == before
