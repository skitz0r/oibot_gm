"""Phase 1 review fixes: ad hoc first-come seating with officer pins / comp-policy bench rules, the officer-only
"not answered" list in the help snapshot, the open paths' class/series, the confirmations toggle in the owner's raid
wizard, and the Members page's regular/rank columns."""
from __future__ import annotations

import time
from datetime import timedelta

import pytest
from conftest import by_role

from oibot_gm import raidcycle as rc
from oibot_gm.registry import ADHOC_SCHEDULE

RID = "barrow_deeps"


def opened(reg, slots=("Tue 19:30",)):
    reg.set_raid_overrides(RID, {"slots": list(slots), "first_open": "2026-01-01T00:00"}, "t")


def soon(reg, days: float = 3, hour: int = 21):
    return (reg.now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def adhoc_with_twelve(reg, rs):
    opened(reg)
    ev = rc.open_run(reg, rs, RID, soon(reg), by="t", schedule=ADHOC_SCHEDULE)
    roles = by_role(reg)
    order = roles["tank"][:2] + roles["healer"][:2] + (roles["melee"] + roles["ranged"])[:8]
    for m in order:
        rc.set_signup(reg, rs, ev, m, None, "in")
        time.sleep(0.002)
    return ev, order


# ---------------------------------------------------------------- ad hoc lock: pins and comp-policy bench rules

@pytest.mark.slow
def test_adhoc_lock_with_an_early_joiner_pinned_out_still_seats_ten(reg, rs):
    """Regression: a Bench pin on an early joiner took a first-come seat, the next joiner came 'late', both were
    forced out and the solve was INFEASIBLE."""
    ev, order = adhoc_with_twelve(reg, rs)
    ev.pins[str(order[4].discord_id)] = "out"
    _, r = rc.propose(reg, rs, ev, save=False)
    seated = {p.signup_name for p in r.selected}
    assert len(seated) == 10 and order[4].display_name not in seated
    assert seated == {m.display_name for m in order[:4] + order[5:11]}, "the next joiner in line takes the pinned-out seat"


@pytest.mark.slow
def test_adhoc_lock_with_always_bench_policy_on_an_early_joiner(reg, rs, monkeypatch):
    from oibot_gm.policy import PolicyStore

    ev, order = adhoc_with_twelve(reg, rs)
    base = PolicyStore.comp_constraints
    monkeypatch.setattr(PolicyStore, "comp_constraints", lambda self: {**base(self), "always_bench": [order[5].display_name]})
    _, r = rc.propose(reg, rs, ev, save=False)
    seated = {p.signup_name for p in r.selected}
    assert len(seated) == 10 and order[5].display_name not in seated and order[10].display_name in seated


@pytest.mark.slow
def test_adhoc_lock_with_a_late_never_bench_joiner(reg, rs, monkeypatch):
    """Regression: a never_bench name among the late joiners landed in both force_in and force_out (INFEASIBLE)."""
    from oibot_gm.policy import PolicyStore

    ev, order = adhoc_with_twelve(reg, rs)
    last = order[11]
    base = PolicyStore.comp_constraints
    monkeypatch.setattr(PolicyStore, "comp_constraints", lambda self: {**base(self), "never_bench": [last.display_name]})
    _, r = rc.propose(reg, rs, ev, save=False)
    seated = {p.signup_name for p in r.selected}
    assert len(seated) == 10 and last.display_name in seated
    assert order[9].display_name not in seated and order[10].display_name not in seated, "the never_bench joiner keeps a seat; first come fills the rest"


# ---------------------------------------------------------------- D1: "not answered" is officer-facing

def test_help_snapshot_hides_not_answered_names_from_members(reg, rs):
    from oibot_gm import help as help_mod

    opened(reg)
    from test_roster_p1_regulars import real

    ms = [real(reg, i) for i in range(3)]
    for m in ms:
        reg.regular_add(m.discord_id, f"{RID}:default", "officer")
    ev = rc.open_run(reg, rs, RID, rc.upcoming(reg, RID, reg.now_local(), 24 * 14)[0][1], by="t")
    rc.set_signup(reg, rs, ev, ms[0], None, "in")
    public = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=False))
    officer = "\n".join(help_mod.run_lines(reg, rs, ev, is_officer=True))
    for m in ms[1:]:
        assert m.display_name not in public, "a member's /ask never learns which regulars haven't answered"
        assert m.display_name in officer
    assert "2 not answered" in public and "not answered (regulars): " in officer


# ---------------------------------------------------------------- the owner's raid wizard sets confirmations

def test_raid_wizard_toggles_confirmations_for_the_raid_and_a_schedule(reg, monkeypatch):
    import asyncio

    from test_flows_config import open_raid
    from wizard_harness import Harness

    from oibot_gm import discord_registry
    from oibot_gm import flows_config  # noqa: F401 — registers the flows

    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    assert reg.raid_def(RID)["confirm"] is True
    h = Harness(reg)

    async def go():
        await open_raid(h, reg)
        await h.pick("Pick a section", "behaviour")
        assert "Ask the rostered to confirm: **on**" in h.text
        await h.press("Ask the rostered to confirm: on")
        assert "Ask the rostered to confirm → off" in h.text
        await h.press("Back to the sections")
        await h.pick("Pick a section", "times")
        await h.pick("Pick a schedule", "alt")
        await h.pick("Change this schedule", "behaviour")
        await h.pick("Change a switch", "confirm=on")
        assert "Ask the rostered to confirm: **on**" in h.text
        await h.press("Back")
        await h.press("Back to the sections")
        await h.press("Save")
        assert "Ask the rostered to confirm" in h.text  # the review names the change
        await h.press("Save")
    asyncio.run(go())
    assert reg.raid_def(RID)["confirm"] is False
    assert reg.schedule_def(RID, "alt")["confirm"] is True and reg.schedule_def(RID, "default")["confirm"] is False


# ---------------------------------------------------------------- every open path stamps kind / series / key suffix

def _wizard(reg, rs):
    import test_flows_raid as tfr
    from wizard_harness import Harness

    from oibot_gm import flows_raid  # noqa: F401 — registers the flows

    bot = tfr.FakeBot(rs)
    return Harness(reg, rs, client=bot), bot


@pytest.fixture
def raid_officer(monkeypatch):
    from oibot_gm import discord_registry

    monkeypatch.setattr(discord_registry, "is_officer", lambda i, r: True)
    monkeypatch.setattr(discord_registry, "is_owner", lambda i, r: True)


def test_raid_open_next_run_is_recurring_on_the_default_series(reg, rs, raid_officer):
    import asyncio

    from oibot_gm.wizard import FLOWS

    opened(reg)
    h, bot = _wizard(reg, rs)
    t0 = rc.upcoming(reg, RID, reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)[0][1]

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", RID)
        await h.press(f"Next run — {reg.local12(t0)}")
        await h.press("Open")
    asyncio.run(go())
    ev = rs.events[rc.event_key(RID, t0)]
    assert (ev.kind, ev.series) == ("recurring", f"{RID}:default")
    assert ev.key == rc.event_key(RID, t0) and "-adhoc-" not in ev.key


def test_raid_open_another_upcoming_run_keeps_its_schedules_series(reg, rs, raid_officer):
    import asyncio

    from oibot_gm.wizard import FLOWS

    opened(reg)
    reg.set_schedule(RID, "alt", {"slots": "Sat 20:00", "name": "Alt run"}, "t")
    h, bot = _wizard(reg, rs)
    alt_t = next(t for s, t in rc.upcoming(reg, RID, reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS) if s["id"] == "alt")

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", RID)
        await h.press("Another upcoming run…")
        await h.pick("Pick the run", f"{alt_t.isoformat()}|alt")
        await h.press("Open")
    asyncio.run(go())
    ev = rs.events[rc.event_key(RID, alt_t, "alt")]
    assert (ev.kind, ev.series) == ("recurring", f"{RID}:alt") and ev.key.split("-")[3] == "alt"


def test_raid_open_ad_hoc_pickup_template_is_adhoc_without_a_series(reg, rs, raid_officer):
    import asyncio

    from oibot_gm.wizard import FLOWS

    opened(reg)
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")
    h, bot = _wizard(reg, rs)
    day = (reg.now_local() + timedelta(days=2)).date().isoformat()

    async def go():
        await FLOWS["raid_open"](h.command(), reg)
        await h.pick("Pick the raid", RID)
        await h.press("Ad hoc run…")
        await h.press("Pickup — pick a time")
        await h.submit(h.modal, day=day, hour="21", minute="0")
        await h.press("Open")
    asyncio.run(go())
    ev = next(e for e in rs.events.values() if e.key.split("-")[3] == "pug")
    assert (ev.kind, ev.series) == ("adhoc", None) and ev.start.hour == 21
    assert not rc.run_confirms(reg, ev) and rc.expected_from(reg, ev) == "none"


def test_plain_text_run_open_applies_with_the_right_class(reg, rs):
    """configops run_open through apply_async (the path plain text and the site's Apply take), not just describe()."""
    import asyncio

    import test_flows_raid as tfr

    from oibot_gm import configops
    from oibot_gm.configops import ConfigOp

    opened(reg)
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")
    bot = tfr.FakeBot(rs)
    apply = lambda op: asyncio.run(configops.apply_async(reg, op, "Owner", True, bot=bot))  # noqa: E731
    t0 = rc.upcoming(reg, RID, reg.now_local(), 24 * rc.OPEN_HORIZON_DAYS)[0][1]
    line = apply(ConfigOp(op="run_open", target=RID))
    ev = rs.events[rc.event_key(RID, t0)]
    assert line.startswith(f"opened {ev.key}") and (ev.kind, ev.series) == ("recurring", f"{RID}:default")
    when = soon(reg, 9, 22)
    apply(ConfigOp(op="run_open", target=RID, value=when.strftime("%Y-%m-%d %H:%M")))
    one = rs.events[rc.event_key(RID, when, ADHOC_SCHEDULE)]
    assert (one.kind, one.series) == ("adhoc", None) and "-adhoc-" in one.key
    pug_t = soon(reg, 10, 20)
    apply(ConfigOp(op="run_open", target=RID, field="pug", value=pug_t.strftime("%Y-%m-%d %H:%M")))
    pug = rs.events[rc.event_key(RID, pug_t, "pug")]
    assert (pug.kind, pug.series) == ("adhoc", None)


# ---------------------------------------------------------------- FillAsk: the tier rename and the appendix A fields

def test_fill_ask_old_tier_names_load_as_the_new_ones():
    old = {"discord_id": 1, "display_name": "A", "role": "tank", "character": "A", "spec": "Protection", "reason": "short 1 tank"}
    assert rc.FillAsk(**old, kind="pool").kind == "regular"
    assert rc.FillAsk(**old, kind="other_roster").kind == "other"
    a = rc.FillAsk(**old, kind="offspec")
    assert (a.kind, a.phase, a.approved_by) == ("offspec", "fill", None)
    assert rc.FillAsk.model_validate(a.model_dump()).model_dump() == a.model_dump()


# ---------------------------------------------------------------- dead config: GuildConfig.slots / auto_propose_hour / TEAM_DEFAULTS

def test_dead_config_is_gone_and_old_files_still_load(reg):
    from oibot_gm.registry import GuildConfig

    old = {**reg.config.model_dump(), "slots": ["Tue 19:30"], "auto_propose_hour": 12}
    cfg = GuildConfig.model_validate(old)
    assert "slots" not in cfg.model_dump() and "auto_propose_hour" not in cfg.model_dump()
    assert not hasattr(rc, "TEAM_DEFAULTS")


def test_a_run_without_its_run_dict_reads_the_raids_settings(reg, rs):
    opened(reg)
    reg.set_raid_overrides(RID, {"lock_hours_before": 12, "nudge": False, "autofill": False}, "t")
    ev = rc.open_run(reg, rs, RID, soon(reg, 6), by="t")
    reg.config.rosters = [t for t in reg.config.rosters if t["key"] != ev.team]  # the run dict is gone (cleanup, an old file)
    team = rc.run_team(reg, ev)
    assert rc.team_setting(team, "cutoff_hard_hours") == 12 and rc.team_setting(team, "reminders") == "none"
    assert rc.team_setting(team, "autofill") is False
    assert rc.team_setting({}, "cutoff_hard_hours") == 24 and rc.team_setting({}, "autofill") is True  # RAID_DEFAULTS


# ---------------------------------------------------------------- regulars coverage per series: one computation, four readers

def _regulars(reg, n=4):
    from test_roster_p1_regulars import real

    opened(reg)
    ms = [real(reg, i) for i in range(n)]  # Warrior Prot, Priest Holy, Shaman Resto, Rogue Combat
    for m in ms:
        reg.regular_add(m.discord_id, f"{RID}:default", "officer")
    return ms


def test_series_coverage_counts_regulars_against_the_minimums(reg):
    reg.set_schedule(RID, "pug", {"kind": "pickup", "name": "Pickup"}, "t")
    _regulars(reg)
    cov = reg.series_coverage(RID)
    assert [c["key"] for c in cov] == [f"{RID}:default"], "ad hoc templates have no regulars"
    c = cov[0]
    need = {r: int(reg.role_bounds(RID, c["size"])[r]["min"]) for r in ("tank", "healer")}
    assert c["counts"]["tank"] == 1 and c["counts"]["healer"] == 2 and c["counts"]["melee"] == 1
    assert c["need"] == need and c["short"] == {r: n - c["counts"][r] for r, n in need.items() if c["counts"][r] < n}
    assert len(c["regulars"]) == 4 and c["seats"] == c["size"]


def test_analytics_card_and_readiness_show_regulars_coverage(reg):
    from test_analytics_cards import text_of

    from oibot_gm import comp as comp_mod
    from oibot_gm import discord_pool as dp
    from oibot_gm import help as help_mod  # noqa: F401

    ico = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731
    _regulars(reg)
    d = comp_mod.readiness(reg, RID)
    assert [{k: v for k, v in cv.items() if k != "forecast"} for cv in d["series"]] == reg.series_coverage(RID)  # + the Phase 2 forecast
    body = text_of(dp.pool_layout(reg, reg.raid_shell(RID), ico)[0])
    line = next(ln for ln in body.splitlines() if ln.startswith("**Raid nights**"))
    assert "<role:tank> 1/" in line and "<role:healer> 2/" in line and "· 4/" in line
    assert "tank" not in line.replace("<role:tank>", ""), "icons stand alone"


def test_roster_overview_has_a_regulars_field(reg, monkeypatch):
    import asyncio
    import types

    import discord
    from discord import app_commands

    from oibot_gm import discord_registry as dr

    _regulars(reg)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    dr.register_commands(tree, types.SimpleNamespace(for_interaction=lambda i: reg), None, lambda kind, key: f"<{kind}:{key}>")
    overview = tree.get_command("roster").get_command("overview")
    said = []

    async def send_message(content=None, **kw):
        said.append(kw)
    itx = types.SimpleNamespace(guild_id=reg.config.discord_guild_id, user=types.SimpleNamespace(id=1, roles=[]),
                                response=types.SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(dr, "is_officer", lambda i, r: True)
    asyncio.run(overview.callback(itx))
    field = next(f for f in said[0]["embed"].fields if f.name == "Regulars")
    assert field.value.startswith("**Raid nights** 🔴 <role:tank> 1/") and "· 4/" in field.value  # Phase 2: the coverage level dot


def test_help_readiness_names_regulars_coverage(reg, rs):
    from oibot_gm import help as help_mod

    _regulars(reg)
    text = "\n".join(help_mod._readiness(help_mod.Ask(reg=reg, rs=rs, user_id=1, officer=True, question="readiness")))
    line = next(ln for ln in text.splitlines() if "regulars of Raid nights" in ln)
    assert "4 for" in line and "tank 1/" in line and "healer 2/" in line
