"""Plain-text config ops: describe() renders current → new, apply() goes through the registry (owner gating included)."""
import pytest

from conftest import join, open_test_run

from oibot_gm import configops, raidcycle as rc
from oibot_gm.configops import ConfigOp
from oibot_gm.registry import RegistryError


def test_raid_set_describe_and_apply(reg):
    op = ConfigOp(op="raid_set", target="barrow_deeps", field="lock_hours_before", value="12")
    assert configops.describe(reg, op) == "raid barrow_deeps lock_hours_before: 24 → 12"
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, op, "officer", is_owner=False)
    assert configops.apply(reg, op, "owner", is_owner=True) == "barrow_deeps: lock_hours_before = 12"
    assert reg.raid_def("barrow_deeps")["lock_hours_before"] == 12
    assert configops.describe(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="weight_rank", value="5")) == "raid barrow_deeps weight_rank: 3 → 5"
    assert configops.describe(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="healer_max", value="4")) == "raid barrow_deeps healer_max: 3 → 4"
    assert configops.describe(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="slots", value="Tue 19:30")) == "raid barrow_deeps slots: [] → Tue 19:30"
    assert configops.describe(reg, ConfigOp(op="raid_reset", target="barrow_deeps")) == "raid barrow_deeps: overrides {'lock_hours_before': 12} → profile defaults"
    configops.apply(reg, ConfigOp(op="raid_reset", target="barrow_deeps"), "owner", is_owner=True)
    assert reg.raid_def("barrow_deeps")["lock_hours_before"] == 24
    with pytest.raises(RegistryError, match="nudge must fall"):
        configops.apply(reg, ConfigOp(op="raid_set", target="barrow_deeps", field="nudge_hours_before", value="2"), "owner", is_owner=True)


def test_set_channel(reg):
    op = ConfigOp(op="set", path="signup_channel", value="<#123456>")
    assert configops.describe(reg, op) == "signup_channel: — → <#123456>"
    with pytest.raises(RegistryError, match="needs the owner"):
        configops.apply(reg, op, "officer", is_owner=False)
    assert configops.apply(reg, op, "owner", is_owner=True) == "signup_channel = <#123456>"
    assert reg.config.signup_channel_id == 123456
    assert configops.describe(reg, ConfigOp(op="set", path="signup_channel", value="<#7>")) == "signup_channel: <#123456> → <#7>"
    with pytest.raises(RegistryError, match="channel mention"):
        configops.apply(reg, ConfigOp(op="set", path="ops_channel", value="general"), "owner", is_owner=True)
    with pytest.raises(RegistryError, match="unknown setting"):
        configops.apply(reg, ConfigOp(op="set", path="motd", value="hi"), "owner", is_owner=True)
    configops.apply(reg, ConfigOp(op="set", path="timezone", value="Europe/London"), "owner", is_owner=True)
    assert reg.config.timezone == "Europe/London"
    with pytest.raises(Exception):  # ZoneInfo rejects it before anything is saved
        configops.apply(reg, ConfigOp(op="set", path="timezone", value="Mars/Olympus"), "owner", is_owner=True)
    assert reg.config.timezone == "Europe/London"


def test_pin_describe(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    assert configops.describe(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="in")) == f"run {ev.team}: pin {m.display_name} to roster (now: no pin)"
    assert configops.describe(reg, ConfigOp(op="pin", target=ev.team, member=f"<@{m.discord_id}>", value="out")) == f"run {ev.team}: keep {m.display_name} on bench (now: no pin)"
    assert configops.describe(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="clear")) == f"run {ev.team}: clear pin for {m.display_name} (now: no pin)"
    ev.pins[str(m.discord_id)] = "out"
    rs.save(ev, "pin")
    assert configops.describe(reg, ConfigOp(op="pin", target=ev.key, member=m.display_name, value="in")).endswith("(now: kept on bench)")
    assert "in | out | clear" in configops.describe(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="maybe"))


def test_pin_apply_refusals(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    with pytest.raises(RegistryError, match="unknown member"):
        configops.apply(reg, ConfigOp(op="pin", target=ev.team, member="Nobody", value="in"), "officer", is_owner=False, raids=rs)
    with pytest.raises(RegistryError, match="in \\| out \\| clear"):
        configops.apply(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="maybe"), "officer", is_owner=False, raids=rs)
    with pytest.raises(RegistryError, match="no live run"):
        configops.apply(reg, ConfigOp(op="pin", target="bd-0101-0000", member=m.display_name, value="in"), "officer", is_owner=False, raids=rs)
    with pytest.raises(RegistryError, match="isn't on this sheet"):  # a member who never answered can't be pinned
        configops.apply(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="in"), "officer", is_owner=False, raids=rs)
    assert not ev.pins


def test_pin_apply(reg, rs):
    ev = open_test_run(reg, rs)
    m = reg.test_members()[0]
    join(reg, rs, ev, [m])
    out = configops.apply(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="in"), "officer", is_owner=False, raids=rs)
    assert out == f"{ev.team}: pin {m.display_name} to roster"
    assert rs.events[ev.key].pins.get(str(m.discord_id)) == "in"
    assert rc.RaidStore(reg.store, reg.key).events[ev.key].pins.get(str(m.discord_id)) == "in", "saved to the store"
    configops.apply(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="out"), "officer", is_owner=False)  # no live store: read from disk
    assert rc.RaidStore(reg.store, reg.key).events[ev.key].pins.get(str(m.discord_id)) == "out"
    configops.apply(reg, ConfigOp(op="pin", target=ev.team, member=m.display_name, value="clear"), "officer", is_owner=False)
    assert not rc.RaidStore(reg.store, reg.key).events[ev.key].pins.get(str(m.discord_id))
