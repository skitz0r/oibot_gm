"""The owner's /gm config wizards (raid rules, aura facts, timezone, about), driven end to end through the fake
interactions in tests/wizard_harness.py. Every modal and screen is linted against Discord's limits by the harness."""
import asyncio
import copy
import subprocess
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import pytest

from oibot_gm import flows_config as fc  # noqa: F401 — registers the flows
from oibot_gm.registry import parse_slots
from oibot_gm.wizard import FLOWS, lint
from wizard_harness import FakeInteraction, Harness

LA = ZoneInfo("America/Los_Angeles")


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


@pytest.fixture
def owner(monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)


async def submit_multi(h: Harness, modal=None, **values) -> None:
    """Harness.submit, but a select may take a list (the Nights multi-select)."""
    modal = modal or h.modal
    payload = []
    for key, item in modal._items.items():
        if key not in values:
            continue
        vals = values[key] if isinstance(values[key], list) else [values[key]]
        if isinstance(item, discord.ui.Select):
            payload.append({"type": 18, "component": {"type": 3, "custom_id": item.custom_id, "values": [str(v) for v in vals]}})
        else:
            payload.append({"type": 18, "component": {"type": 4, "custom_id": item.custom_id, "value": str(vals[0])}})
    modal._refresh(None, payload, {})
    opened_from = h.screens[-1]["message_id"] if h.screens and h.screens[-1]["view"] is not None else None
    itx = FakeInteraction(h, kind=discord.InteractionType.modal_submit, message_id=opened_from)
    h.modal = None
    await modal.on_submit(itx)


def options(h: Harness, placeholder: str) -> list[str]:
    sel = next(s for s in h._items() if isinstance(s, discord.ui.Select) and s.placeholder == placeholder)
    return [o.value for o in sel.options]


async def open_raid(h: Harness, reg, raid: str = "barrow_deeps") -> None:
    await FLOWS["raid_config"](h.command(), reg)
    await h.pick("Pick a raid", raid)
    assert "Pick a section" in h.selects() and "Save" in h.buttons()


# ---------------------------------------------------------------- raid rules

def test_raid_config_multi_section_edit_saves_in_one_commit(reg, store, owner):
    h = Harness(reg)
    n0 = commits(store)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "times")
        await h.press("Add a run time")
        assert isinstance(h.modal, fc.MultiForm) and h.modal._items["nights"].max_values == 7
        await submit_multi(h, nights=["Tue", "Thu"], hour="19", minute="30")
        assert "Tue 7:30 PM" in h.text and "Thu 7:30 PM" in h.text and "19:30" not in h.text
        await h.press("Back to the sections")
        await h.pick("Pick a section", "cadence")
        await h.pick("Pick a run's week", "standard")
        assert "3 unsaved changes" in h.text  # run times + opens + nudge (lock and confirm already match)
        await h.pick("Pick a section", "weights")
        await h.submit(h.modal, rank="5", main="2", sat_out="2", signup_order="1")
        await h.pick("Pick a section", "length")
        await h.submit(h.modal, lockout_days="5", duration_hours="4.5")
        await h.pick("Pick a section", "behaviour")
        await h.press("Fill seats automatically: on")
        await h.pick("How to split a full slot", "rotation")
        await h.press("Back to the sections")
        await h.pick("Pick a section", "notes")
        await h.submit(h.modal, notes="Bring fire resistance.")
        assert commits(store) == n0  # nothing is written before Save
        await h.press("Save")
        assert "Next runs" in h.text and "7:30 PM" in h.text and "19:30" not in h.text and "Rotation" in h.text
        assert "4½ hours" in h.text and "5 days" in h.text and "5 days before" in h.text
        await h.press("Save")
        assert "✅" in h.text and h.screen["view"] is None
    asyncio.run(go())
    assert commits(store) == n0 + 1  # ONE commit for every section
    rd = reg.raid_def("barrow_deeps")
    assert rd["slots"] == ["Tue 19:30", "Thu 19:30"]
    assert (rd["signup_lead_hours"], rd["nudge_hours_before"], rd["lock_hours_before"], rd["confirm_hours_before"]) == (120, 72, 24, 6)
    assert rd["weights"]["rank"] == 5 and rd["lockout_days"] == 5 and rd["duration_hours"] == 4.5
    assert rd["autofill"] is False and rd["split_policy"] == "rotation" and rd["notes"] == "Bring fire resistance."


def test_a_refused_cadence_is_caught_in_its_section_and_writes_nothing(reg, store, owner):
    h = Harness(reg)
    n0, before = commits(store), copy.deepcopy(reg.config.raids)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "cadence")
        await h.pick("Pick a run's week", "custom")
        await h.submit(h.modal, signup_lead_hours="120", nudge_hours_before="24", lock_hours_before="48", confirm_hours_before="6")
        assert h.text.startswith("**Raid rules**\n❌") and "nudge" in h.text
        assert h.buttons() == ["Fix this", "Back to the sections", "Cancel"]
        await h.press("Fix this")  # the same form, with the refused picks restored
        chosen = {k: [o.value for o in it.options if o.default] for k, it in h.modal._items.items()}
        assert chosen["lock_hours_before"] == ["48"] and chosen["nudge_hours_before"] == ["24"]
        await h.submit(h.modal, signup_lead_hours="120", nudge_hours_before="48", lock_hours_before="48", confirm_hours_before="6")
        assert "3 unsaved changes" in h.text  # opens, nudge and lock differ from what is stored; confirm doesn't
        await h.press("Cancel")
    asyncio.run(go())
    assert reg.config.raids == before and commits(store) == n0


def test_a_refusal_at_save_keeps_the_draft_and_leaves_config_and_git_untouched(reg, store, owner):
    h = Harness(reg)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "cadence")
        await h.pick("Pick a run's week", "custom")
        await h.submit(h.modal, signup_lead_hours="120", nudge_hours_before="72", lock_hours_before="24", confirm_hours_before="12")
        await h.press("Save")
        assert "Save these changes" in h.text
        reg.set_raid_override("barrow_deeps", "lock_hours_before", 8, "someone else")  # the stored rules move under the wizard
        n, stored = commits(store), copy.deepcopy(reg.config.raids)
        await h.press("Save")
        assert "❌" in h.text and "Nothing was saved" in h.text and h.buttons() == ["Back to the sections", "Cancel"]
        assert commits(store) == n and reg.config.raids == stored  # set_raid_overrides refused as a whole
        await h.press("Back to the sections")
        assert "unsaved change" in h.text  # the draft is kept
    asyncio.run(go())
    assert reg.raid_def("barrow_deeps")["confirm_hours_before"] == 6


def test_cancel_from_the_hub_writes_nothing(reg, store, owner):
    h = Harness(reg)
    n0, before = commits(store), copy.deepcopy(reg.config.raids)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "cadence")
        await h.pick("Pick a run's week", "same_week")
        await h.pick("Pick a section", "notes")
        await h.submit(h.modal, notes="x")
        await h.press("Cancel")
        assert "nothing was changed" in h.text
    asyncio.run(go())
    assert reg.config.raids == before and commits(store) == n0


def test_run_times_add_and_remove_round_trip_through_parse_slots(reg, owner):
    h = Harness(reg)
    wiz = {}

    async def go():
        await FLOWS["raid_config"](h.command(), reg, raid="barrow_deeps", section="times")  # another flow jumps straight in
        assert "Add a run time" in h.buttons()
        wiz["w"] = h.screen["view"]
        await h.press("Add a run time")
        await submit_multi(h, nights=["Sat", "Tue"], hour="21", minute="15")
        assert wiz["w"].draft["slots"] == ["Tue 21:15", "Sat 21:15"]  # sorted by the week
        await h.press("✕ Tue 9:15 PM")
        assert wiz["w"].draft["slots"] == ["Sat 21:15"] and "Dropped Tue 9:15 PM" in h.text
        await h.press("Add a run time")
        await submit_multi(h, nights=["Sat"], hour="21", minute="15")
        assert "❌" in h.text and "Sat 9:15 PM is already a run time" in h.text
        await h.press("Back to the sections")
        await h.press("Save")
        await h.press("Save")
    asyncio.run(go())
    slots = reg.raid_def("barrow_deeps")["slots"]
    assert slots == ["Sat 21:15"] and parse_slots(slots) == slots


def test_group_make_up_pages_past_25_and_never_offers_min_above_max(reg, owner):
    h = Harness(reg)

    async def go():
        await open_raid(h, reg, "onyxias_lair")  # 40-player: 0..40 is two pages
        await h.pick("Pick a section", "comp")
        await h.pick("Pick a role", "dps")
        assert "page 2 of 2" in h.text and "26" in options(h, "Fewest dps")  # opens on the page holding the current min
        await h.pick("Fewest dps", "27")
        assert options(h, "Most dps")[0] == "27"  # the max starts at the chosen min
        await h.pick("Most dps", "30")
        await h.press("Save")
        await h.press("Save")
    asyncio.run(go())
    comp = reg.raid_def("onyxias_lair")["comp"]["dps"]
    assert (comp["min"], comp["max"]) == (27, 30)


def _next_dst_change(after: date) -> date:
    d = after
    while (datetime(d.year, d.month, d.day, 12, tzinfo=LA).utcoffset() == datetime(after.year, after.month, after.day, 12, tzinfo=LA).utcoffset()):
        d += timedelta(days=1)
    return d


async def _pick_first_open(h: Harness, target: date, hour: str) -> None:
    """Day list, narrowed by week until the target day is on it."""
    await h.press("Pick the date and time")
    while target.isoformat() not in [o.value for o in h.modal._items["day"].options]:
        await h.submit(h.modal, day="later", hour=hour, minute="0")
        weeks = options(h, "Pick the week")
        monday = (target - timedelta(days=target.weekday())).isoformat()
        await h.pick("Pick the week", monday if monday in weeks else weeks[-1])
    await h.submit(h.modal, day=target.isoformat(), hour=hour, minute="0")


def test_first_open_across_dst(reg, owner):
    h = Harness(reg)
    change = _next_dst_change(reg.now_local().date() + timedelta(days=30))
    before, after = change - timedelta(days=3), change + timedelta(days=3)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "first_open")
        await _pick_first_open(h, before, "15")
        assert "First lockout opens" in h.text and "3:00 PM" in h.text and "<t:" in h.text
        await h.pick("Pick a section", "first_open")
        await _pick_first_open(h, after, "15")
        await h.press("Save")
        assert "3:00 PM" in h.text and "T15:00" not in h.text
        await h.press("Save")
    asyncio.run(go())
    fo = reg.first_open("barrow_deeps")
    local = fo.astimezone(LA)
    assert local.date() == after and (local.hour, local.minute) == (15, 0)
    assert fo.utcoffset() == datetime(after.year, after.month, after.day, 15, tzinfo=LA).utcoffset()
    assert fo.utcoffset() != datetime(before.year, before.month, before.day, 15, tzinfo=LA).utcoffset()


def test_reset_clears_the_raid_override(reg, store, owner):
    reg.set_raid_override("barrow_deeps", "notes", "old", "t")
    h = Harness(reg)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "reset")
        await h.press("Reset")
        assert "✅" in h.text
    asyncio.run(go())
    assert "barrow_deeps" not in reg.config.raids


def test_raid_config_is_owner_only(reg, store):
    h = Harness(reg)  # is_owner not patched: the harness user is not the owner
    n0 = commits(store)

    async def go():
        await FLOWS["raid_config"](h.command(), reg)
        assert h.text == "Owner only." and h.screen["view"] is None
    asyncio.run(go())
    assert commits(store) == n0


# ---------------------------------------------------------------- aura facts: a draft hub per buff, one Save (design §5.43)

def test_aura_benefit_edits_are_a_draft_and_one_save_writes_them(reg, store, owner):
    h = Harness(reg)
    before = dict(reg.profile.families["windfury_totem"].value)
    melee = f"Melee {before['melee']:g}"  # from the profile, so a vetted value change doesn't break the test

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "windfury_totem")
        n0 = commits(store)
        await h.pick("What to change", "benefits")
        assert melee in h.text and "Feral 4" in h.text
        await h.pick("Pick who", "mana")
        await h.pick("How many points", "2")
        assert "Mana users 2" in h.text and melee in h.text  # the benefits screen shows the drafted map
        await h.pick("Pick who", "__spec")
        await h.pick("Pick a class", "Shaman")
        await h.pick("Pick a spec", "Elemental")
        await h.pick("How many points", "3")
        await h.pick("Pick who", "tank")
        await h.pick("How many points", "0")  # remove one key: only that one
        await h.press("Back")
        assert "Not saved yet:" in h.text and "3 changes" in h.text and commits(store) == n0  # nothing written yet
        await h.press("Save this buff")
        assert "✅" in h.text and commits(store) == n0 + 1  # ONE write for the family's three keys
        await h.press("Done")
        assert h.text.count("✅") == 1 and h.text.count("value:") == 3  # one write, its three keys
    asyncio.run(go())
    after = reg.profile.families["windfury_totem"].value
    expect = {k: v for k, v in before.items() if k != "tank"} | {"mana": 2.0, "spec:Elemental": 3.0}
    assert after == expect


def test_aura_scope_family_and_note_are_drafted_then_saved_together(reg, store, owner):
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        n = commits(store)
        assert "Save this buff" in h.buttons() and next(b for b in h._items() if getattr(b, "label", None) == "Save this buff").disabled
        await h.pick("What to change", "family")
        await h.pick("New family", "fortitude")
        await h.pick("What to change", "family")
        await h.pick("New family", "own")  # back to how it is: drops out of the draft
        assert "Not saved yet" not in h.text
        await h.pick("What to change", "scope")
        await h.pick("New scope", "raid")
        await h.pick("What to change", "note")
        await h.submit(h.modal, note="tested on the PTR")
        assert "Scope party → raid" in h.text and "2 changes" in h.text and commits(store) == n
        await h.press("Save this buff")
        assert commits(store) == n + 1  # scope and note: ONE set_buff_overrides write
        await h.press("Done")
    asyncio.run(go())
    b = reg.buff("blood_pact")
    assert b.scope == "raid" and b.note == "tested on the PTR" and b.family_id == "blood_pact"


def test_aura_done_with_a_dirty_draft_asks_save_discard_back(reg, store, owner):
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        n = commits(store)
        await h.pick("What to change", "scope")
        await h.pick("New scope", "raid")
        await h.press("Another buff")
        assert "aren't saved" in h.text and h.buttons() == ["Save this buff", "Discard them", "Back"]
        await h.press("Back")
        assert "Not saved yet" in h.text  # the draft survived
        await h.press("Done")
        await h.press("Discard them")
        assert "nothing was changed" in h.text and commits(store) == n
    asyncio.run(go())
    assert reg.buff("blood_pact").scope == "party"


def test_aura_dirty_draft_saved_from_the_leave_screen(reg, store, owner):
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        await h.pick("What to change", "status")
        await h.pick("New status", "confirmed")
        await h.press("Another buff")
        await h.press("Save this buff")
        assert "Which buff?" in h.text  # saved, then on to the buff list
    asyncio.run(go())
    assert reg.buff("blood_pact").status == "confirmed"


def test_aura_new_family_is_named_never_typed_as_an_id(reg, store, owner):
    h = Harness(reg)
    slug = reg.family_slug("Stamina buffs")

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        await h.pick("What to change", "family")
        assert "__new" in options(h, "New family")
        await h.pick("New family", "__new")
        await h.submit(h.modal, name="Stamina buffs")
        assert slug in h.text
        await h.pick("What to change", "benefits")
        await h.pick("Pick who", "all")
        await h.pick("How many points", "3")
        await h.press("Back")
        await h.press("Save this buff")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.buff("blood_pact").family_id == slug and reg.profile.families[slug].name == "Stamina buffs" and reg.profile.families[slug].value == {"all": 3.0}


def test_aura_save_after_someone_else_changed_the_auras_offers_a_rebase(reg, store, owner):
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        await h.pick("What to change", "scope")
        await h.pick("New scope", "raid")
        reg.set_buff_overrides("fortitude", {"note": "someone else"}, "Ana")  # the section rev the wizard snapshotted moved
        await h.press("Save this buff")
        assert "changed since you looked" in h.text and "Reload and keep my changes" in h.buttons()
        assert reg.buff("blood_pact").scope == "party"
        await h.press("Reload and keep my changes")
        assert "Scope party → raid" in h.text
        await h.press("Save this buff")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.buff("blood_pact").scope == "raid" and reg.buff("fortitude").note == "someone else"


def test_aura_reset_everything_uses_the_decide_screen(reg, store, owner):
    reg.set_buff_overrides("blood_pact", {"scope": "raid"}, "Jon")
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.press("Reset everything to game defaults")
        assert reg.aura_reset_sentence() in h.text and h.buttons() == ["Reset everything", "Cancel"]
        await h.press("Reset everything")
        assert "Which buff?" in h.text and "Reset everything to game defaults" not in h.buttons()
    asyncio.run(go())
    assert reg.config.buffs == {} and reg.buff("blood_pact").scope == "party"


def test_aura_stands_alone_then_who_benefits_edits_the_buffs_own_family(reg, store, owner):
    """Regression: on a buff in a shared family, 'Stands alone' then Who benefits wrote the SHARED family."""
    h = Harness(reg)
    shared_before = dict(reg.profile.families["leader_of_the_pack"].value)
    short = reg.buff("moonkin_aura").short

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "moonkin_aura")
        assert "shared with" in h.text
        await h.pick("What to change", "family")
        await h.pick("New family", "own")
        await h.pick("What to change", "benefits")
        assert f"**{short}** family" in h.text and "Leader of the Pack" not in h.text.split("\n")[0]
        await h.pick("Pick who", "spell")
        assert f"**{short}** family" in h.text
        await h.pick("How many points", "2")
        await h.press("Back")
        assert "Not saved yet" in h.text and "Leader of the Pack" not in h.text.split("Not saved yet")[1]
        await h.press("Save this buff")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.buff("moonkin_aura").family_id == "moonkin_aura" and reg.profile.families["moonkin_aura"].value == {"spell": 2.0}
    assert reg.profile.families["leader_of_the_pack"].value == shared_before


def test_aura_reset_one_buff_shows_the_sentence_from_code(reg, store, owner):
    from oibot_gm.configops import aura_reset_one_sentence

    reg.set_buff_overrides("blood_pact", {"scope": "raid"}, "Jon")
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        await h.pick("Pick a buff", "blood_pact")
        await h.pick("What to change", "reset")
        assert aura_reset_one_sentence(reg, "blood_pact") in h.text and h.buttons()[0] == "Reset"
        await h.press("Reset")
        assert "✅" in h.text
    asyncio.run(go())
    assert "blood_pact" not in reg.config.buffs


def test_aura_is_owner_only(reg):
    h = Harness(reg)

    async def go():
        await FLOWS["aura"](h.command(), reg)
        assert h.text == "Owner only."
    asyncio.run(go())


def test_buff_list_fits_one_select(reg):
    assert len(reg.profile.buffs) <= 25  # the flow pages past 25 anyway


# ---------------------------------------------------------------- timezone

def test_timezone_paging_reaches_a_city_past_the_first_25(reg, store, owner):
    h = Harness(reg)
    zones = fc.tz_cities("America")
    target = "America/Sao_Paulo"
    assert len(zones) > 100 and zones.index(target) >= 25
    n0 = commits(store)

    async def go():
        await FLOWS["timezone"](h.command(), reg)
        await h.press("Another zone…")
        await h.pick("Pick a region", "America")
        seen = set(options(h, "Pick a city"))
        while target not in options(h, "Pick a city"):
            await h.press("More →")
            seen |= set(options(h, "Pick a city"))
        await h.pick("Pick a city", target)
        assert "America/Sao_Paulo" in h.text and "Now:" in h.text and "Save" in h.buttons()
        assert commits(store) == n0
        await h.press("Save")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.config.timezone == target and commits(store) == n0 + 1


def test_timezone_every_city_is_reachable_and_cancel_writes_nothing(reg, store, owner):
    h = Harness(reg)
    n0 = commits(store)

    async def go():
        await FLOWS["timezone"](h.command(), reg)
        await h.press("Another zone…")
        await h.pick("Pick a region", "America")
        seen = set(options(h, "Pick a city"))
        while "More →" in h.buttons():
            await h.press("More →")
            seen |= set(options(h, "Pick a city"))
        assert seen == set(fc.tz_cities("America"))  # never clipped
        await h.press("Other region")
        await h.press("Back")
        await h.press("Eastern · New York")
        assert "America/New_York" in h.text
        await h.press("Cancel")
    asyncio.run(go())
    assert reg.config.timezone == "America/Los_Angeles" and commits(store) == n0


def test_timezone_save_goes_through_the_core_under_the_lock_with_the_settings_rev(reg, store, owner, monkeypatch):
    """Regression: the wizard wrote reg.config.timezone on the loop without config_lock. The Save now goes through
    configops.apply_ops (worker thread, config_lock, the settings rev snapshotted when the wizard opened)."""
    import threading

    h = Harness(reg)
    seen = []
    real = reg.save_config

    def spy(msg, *a, **k):
        seen.append((threading.current_thread() is threading.main_thread(), reg.config_lock._is_owned()))
        return real(msg, *a, **k)
    monkeypatch.setattr(reg, "save_config", spy)

    async def go():
        await FLOWS["timezone"](h.command(), reg)
        await h.press("Eastern · New York")
        reg.config.about = "someone else"
        real("about by someone else")  # the settings section moved after the wizard opened
        seen.clear()
        await h.press("Save")
        assert "changed since you looked" in h.text and reg.config.timezone == "America/Los_Angeles"
        await h.press("Look again")
        await h.press("Eastern · New York")
        await h.press("Save")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.config.timezone == "America/New_York" and seen == [(False, True)]


def test_about_save_refuses_a_stale_settings_rev_and_keeps_the_text(reg, store, owner):
    h = Harness(reg)

    async def go():
        await FLOWS["about"](h.command(), reg)
        await h.submit(h.modal, text="We raid Tuesdays.")
        reg.set_buff_overrides("blood_pact", {"note": "x"}, "Ana")  # another section: not a conflict
        reg.config.timezone = "Europe/London"
        reg.save_config("timezone by someone else")
        await h.press("Save")
        assert "changed since you looked" in h.text and "Your text is kept" in h.text and reg.config.about != "We raid Tuesdays."
        await h.press("Edit")
        assert h.modal._items["text"].default == "We raid Tuesdays."
        await h.submit(h.modal, text="We raid Tuesdays.")
        await h.press("Save")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.config.about == "We raid Tuesdays."


# ---------------------------------------------------------------- about

def test_about_is_capped_at_600(reg, store, owner):
    h = Harness(reg)
    before = reg.config.about

    async def go():
        await FLOWS["about"](h.command(), reg)
        box = h.modal._items["text"]
        assert box.max_length == 600 and "600" in h.modal.children[0].description and lint(h.modal) == []
        await h.submit(h.modal, text="x" * 700)  # the client enforces max_length; the server refuses anyway
        assert "❌" in h.text and "700" in h.text
        assert reg.config.about == before
        await h.press("Fix this")
        assert h.modal._items["text"].default.startswith("x")  # the draft is kept
        await h.submit(h.modal, text="We raid Tuesdays and Thursdays.")
        await h.press("Save")
        assert "✅" in h.text
    asyncio.run(go())
    assert reg.config.about == "We raid Tuesdays and Thursdays."


def test_about_cancel_writes_nothing(reg, store, owner):
    h = Harness(reg)
    n0 = commits(store)

    async def go():
        await FLOWS["about"](h.command(), reg)
        await h.submit(h.modal, text="draft")
        await h.press("Cancel")
    asyncio.run(go())
    assert commits(store) == n0 and reg.config.about != "draft"


def test_an_aura_field_set_back_to_the_games_value_is_no_override(reg):
    """Seen live 2026-10-24: a plain-text change wrote Moonkin Aura's family = the family the profile already gives it, and the
    row went amber. Amber means "differs from the game": a default written back drops out, and a buff with nothing left is gone
    from the guild's overrides (the reset sentence counts only real changes)."""
    reg.set_buff_overrides("moonkin_aura", {"scope": "raid", "family": "leader_of_the_pack"}, "Jon")
    assert reg.config.buffs["moonkin_aura"] == {"scope": "raid"}
    assert "moonkin_aura" not in reg.config.families
    base = next(b for b in reg.base_profile.buffs if b.id == "moonkin_aura")
    reg.set_buff_overrides("moonkin_aura", {"scope": base.scope}, "Jon")
    assert "moonkin_aura" not in reg.config.buffs
    assert reg.aura_reset_sentence().startswith("Nothing to reset")
