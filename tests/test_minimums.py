"""One minimum per role or class (design D16): stored as raids.<id>.minimums, read by Registry.minimums / role_bounds,
written by Registry.set_minimums, lifted from an older file's comp limits and aims (lift_minimums) and lowered back for
older code (lower_minimums). A class short is shown on the run and never asked for."""
import copy

import pytest

from conftest import GUILD, OFFICER_ROLE, OWNER, join, open_test_run
from oibot_gm import raid_views as views
from oibot_gm import raidcycle as rc
from oibot_gm import registry as R
from oibot_gm.models import Player
from oibot_gm.registry import RegistryError

RID = "barrow_deeps"  # 10 players; the profile's minimums: 2 tanks, 2 healers


def base_config() -> dict:
    return {"key": GUILD, "name": "Demo Guild", "game_profile": "forever", "discord_guild_id": 1, "owner_discord_id": OWNER,
            "timezone": "America/Los_Angeles", "officer_role_ids": [OFFICER_ROLE]}


def test_a_minimum_is_one_number_per_slot(reg):
    lines = reg.set_minimums(RID, {"healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}}, "t")
    assert reg.config.raids[RID]["minimums"] == {"healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}}
    assert reg.minimums(RID)["healer"] == 3 and reg.role_bounds(RID, 10)["healer"] == {"min": 3}
    assert lines == ["Barrow Deeps · Healers: at least 3", "Barrow Deeps · Paladins: at least 2 (blessings)"]
    assert not {"comp", "comp_targets"} & set(reg.config.raids[RID])
    assert reg.set_minimums(RID, {"Paladin": None}, "t") == ["Barrow Deeps · Paladins: back to the game's minimum"]
    assert reg.minimums(RID) == {"tank": 2, "healer": 3}


def test_role_bounds_carry_no_maximum(reg):
    b = reg.role_bounds(RID, 10)
    assert b == {"tank": {"min": 2}, "healer": {"min": 2}, "dps": {"min": 0}}


def test_a_maximum_is_refused_and_nothing_changes(reg):
    before = copy.deepcopy(reg.config.raids)
    with pytest.raises(RegistryError, match="no upper limit") as e:
        reg.set_minimums(RID, {"healer": {"min": 2, "max": 3}}, "t")
    assert str(e.value) == R.NO_UPPER_LIMIT
    assert reg.config.raids == before


def test_a_minimum_above_the_raid_size_is_refused(reg):
    with pytest.raises(RegistryError, match="10"):
        reg.set_minimums(RID, {"Mage": {"min": 11}}, "t")
    assert "minimums" not in (reg.config.raids.get(RID) or {})


def test_a_run_key_is_refused(reg, rs):
    ev = open_test_run(reg, rs)
    with pytest.raises(RegistryError, match="belong to the raid") as e:
        reg.set_minimums(ev.team, {"healer": {"min": 2}}, "t")
    assert str(e.value) == R.MINIMUM_ON_RAID


def test_minimums_scale_with_the_roster_size(reg):
    reg.set_minimums("hyjal_summit_forever", {"healer": {"min": 6}}, "t")
    assert reg.minimums("hyjal_summit_forever", 10)["healer"] == 3


def test_the_profile_holds_one_minimum_per_role(profile):
    for raid in profile.raids.values():
        assert "comp" not in raid
    assert profile.raids["hyjal_summit_forever"]["minimums"] == {"tank": 2, "healer": 5}
    assert all(set(b) == {"min"} for b in profile.comp_rules["roles"].values())


def test_lifting_comp_and_aims(profile):
    cfg = R.GuildConfig(**{**base_config(), "raids": {RID: {"comp": {"tank": {"min": 2, "max": 3}, "healer": {"min": 3, "max": 4}, "dps": {"min": 5, "max": 6}},
                                                            "comp_targets": {"Paladin": {"min": 2, "max": 3, "note": "blessings"}, "healer": {"min": 4}, "melee": {"min": 3}}}}})
    lines = R.lift_minimums(cfg)
    assert cfg.raids[RID] == {"minimums": {"tank": {"min": 2}, "healer": {"min": 3}, "Paladin": {"min": 2, "note": "blessings"}, "melee": {"min": 3}}}
    assert lines and R.lift_minimums(cfg) == []


def test_lifting_drops_a_run_dicts_own_aims():
    run = {"key": "bd-1212-2000", "instance": RID, "name": "Barrow Deeps", "comp_targets": {"Mage": {"min": 1}}}
    cfg = R.GuildConfig(**{**base_config(), "rosters": [run]})
    lines = R.lift_minimums(cfg)
    assert "comp_targets" not in cfg.rosters[0] and any("Barrow Deeps" in x for x in lines)


def test_lowering_and_lifting_again_round_trips_the_minimums(profile):
    mins = {"tank": {"min": 3}, "healer": {"min": 2}, "Paladin": {"min": 2, "note": "blessings"}, "ranged": {"min": 4}}
    cfg = R.GuildConfig(**{**base_config(), "raids": {RID: {"notes": "x", "minimums": copy.deepcopy(mins)}}})
    lowered = R.lower_minimums(cfg, profile.raids)
    assert cfg.raids[RID] == {"notes": "x", "comp": {"tank": {"min": 3}, "healer": {"min": 2}},
                              "comp_targets": {"Paladin": {"min": 2, "note": "blessings"}, "ranged": {"min": 4}}}
    assert lowered
    R.lift_minimums(cfg)
    assert cfg.raids[RID] == {"notes": "x", "minimums": mins}


def test_lowering_one_role_writes_the_other_from_the_profile(profile):
    cfg = R.GuildConfig(**{**base_config(), "raids": {RID: {"minimums": {"healer": {"min": 3}}}}})
    R.lower_minimums(cfg, profile.raids)
    assert cfg.raids[RID] == {"comp": {"tank": {"min": 2}, "healer": {"min": 3}}}


def _p(name, cls, spec, role, pos):
    return Player(signup_name=name, pos=pos, status="signed", cls=cls, spec=spec, role=role, character=name, map_confidence="high", unmapped=False)


def test_first_come_takes_a_late_damage_dealer_before_an_extra_healer():
    players = [_p("h1", "Priest", "Holy", "healer", 1), _p("h2", "Priest", "Holy", "healer", 2)]
    players += [_p(f"m{i}", "Mage", "Fire", "ranged", 2 + i) for i in range(1, 5)]
    assert rc.first_come_out(players, 5, {"healer": 1}, set()) == {"h2"}


def test_an_extra_healer_takes_a_seat_nobody_else_wants():
    players = [_p("h1", "Priest", "Holy", "healer", 1), _p("h2", "Priest", "Holy", "healer", 2), _p("m1", "Mage", "Fire", "ranged", 3)]
    assert rc.first_come_out(players, 5, {"healer": 1}, set()) == set()


def test_a_class_short_is_named_and_never_asked_for(reg, rs):
    reg.set_minimums(RID, {"Paladin": {"min": 2}}, "t")
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, [m for m in reg.test_members() if m.main.cls != "Paladin"])
    g = rc.gaps(reg, ev)
    assert g["classes"] == {"Paladin": 2}
    nd = rc.needs(reg, ev)
    assert "Paladin" not in nd["roles"]
    assert "2 Paladins" in views.gaps_text(lambda kind, x: x, nd)
    assert "short 2 Paladins" in (rc.short_why(reg, ev) or "")


def test_a_class_minimum_reaches_the_lock(reg, rs, monkeypatch):
    reg.set_minimums(RID, {"Paladin": {"min": 1}, "healer": {"min": 3}}, "t")
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members())
    seen = {}

    def fake(profile, players, raid_id, opts, **kw):
        seen["opts"] = opts
        raise RuntimeError("stop")
    monkeypatch.setattr(rc.solver, "solve_rosters", fake)
    with pytest.raises(RuntimeError):
        rc._solve_run(reg, rs, ev, whatif=False)
    assert seen["opts"].minimums["Paladin"] == 1 and seen["opts"].minimums["healer"] == 3 and seen["opts"].minimums["tank"] == 2


def test_an_older_file_is_lifted_at_load_and_saved_once(data_root, profile):
    import subprocess

    import yaml

    from oibot_gm.registry import Registry
    from oibot_gm.store import GitStore

    doc = {**base_config(), "raids": {RID: {"comp": {"healer": {"min": 3, "max": 4}}, "comp_targets": {"Mage": {"min": 1, "note": "Intellect"}}}}}
    (data_root / GUILD / "guild.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "old file"], cwd=data_root, check=True)
    reg = Registry(GitStore(data_root, push=False), GUILD, profile)
    assert reg.config.raids[RID] == {"minimums": {"healer": {"min": 3}, "Mage": {"min": 1, "note": "Intellect"}}}
    assert reg.minimums(RID)["healer"] == 3  # read right before any save
    reg.migrate_schedules()
    saved = yaml.safe_load((data_root / GUILD / "guild.yaml").read_text())
    assert saved["raids"][RID] == {"minimums": {"healer": {"min": 3}, "Mage": {"min": 1, "note": "Intellect"}}}
    said = list(reg.timing_saved)
    assert len(said) == 1 and said[0].startswith("Make-up is one minimum per role or class now: ")
    assert reg.migrate_schedules() == [] and list(reg.timing_saved) == said


def test_scaling_rounds_half_up_and_never_loses_a_minimum(reg):
    reg.set_minimums("hyjal_summit_forever", {"healer": {"min": 5}, "Paladin": {"min": 1}, "Druid": {"min": 3}}, "t")
    m = reg.minimums("hyjal_summit_forever", 10)  # half of a 20-player raid
    assert m["healer"] == 3 and m["Druid"] == 2  # 2.5 → 3 and 1.5 → 2: half up, never to the even number
    assert m["Paladin"] == 1  # half of one Paladin is still one Paladin
    assert reg.minimums("hyjal_summit_forever", 5)["Paladin"] == 1


def test_lifting_an_aim_with_only_a_maximum_keeps_no_minimum():
    cfg = R.GuildConfig(**{**base_config(), "raids": {RID: {"comp_targets": {"Paladin": {"max": 2}, "Mage": {"min": 0}, "Hunter": {"min": 1}}}}})
    lines = R.lift_minimums(cfg)
    assert cfg.raids[RID] == {"minimums": {"Hunter": {"min": 1}}}
    assert "at least 0" not in lines[0] and "at most 2 Paladins" in lines[0] and "Mages with no minimum" in lines[0]


def test_the_analytics_card_hint_is_a_minimum(reg):
    from test_analytics_cards import text_of

    from oibot_gm import discord_pool

    view, _ = discord_pool.pool_layout(reg, reg.raid_shell(RID), lambda kind, x: "")
    text = text_of(view)
    assert "at least 2 Paladins in Barrow Deeps" in text
    assert not any(w in text for w in ("cap ", "at most", "ideals", "over cap", "Over cap"))


def test_the_auras_section_prints_each_minimum_alone(reg, rs):
    from test_parity_d2 import state

    s = state(reg, rs, OWNER, True, "does windfury totem stack with anything?")
    line = next(x for x in s.splitlines() if x.startswith("comp rules: "))
    assert "at least 2 tanks" in line and "at least 5 healers" in line and "None" not in line
    assert "–" not in line.split(", per group")[0]
