"""When a run can't split: the reason in code (bodies vs tank/healer minimums), one healthy roster that meets its
minimums, the joiners left over, and the one-line hint that bench + pool could make another run."""
import pytest

from conftest import by_role, join, open_test_run

from oibot_gm import raidcycle as rc


def _thin(reg, rs, tanks=2, healers=2, dps=17):
    """A 10-player Barrow Deeps run whose joiners are `tanks` tanks, `healers` healers and `dps` dps."""
    reg.seed_test_members(30, "t")
    br = by_role(reg)
    ev = open_test_run(reg, rs)
    dd = br["melee"] + br["ranged"]
    join(reg, rs, ev, br["tank"][:tanks] + br["healer"][:healers] + dd[:dps])
    return ev, br


def test_reason_second_run_short(reg, rs):
    ev, _ = _thin(reg, rs)
    reason = rc.run_split_reason(reg, ev)
    assert reason == {"bodies_allow": 2, "roles_allow": 1, "run": 2, "short": {"tank": 2, "healer": 2}}
    assert rc.split_reason_text(reason) == "Enough people for 2 runs — a second is short 2 tanks and 2 healers"
    assert rc.split_reason_text(reason, lambda r, n: f"<{r}> {n}") == "Enough people for 2 runs — a second is short <tank> 2   <healer> 2"


def test_reason_not_even_one(reg, rs):
    ev, _ = _thin(reg, rs, tanks=2, healers=1, dps=8)
    reason = rc.run_split_reason(reg, ev)
    assert reason["roles_allow"] == 0 and reason["short"] == {"healer": 1}
    assert rc.split_reason_text(reason) == "1 run is short 1 healer"


def test_no_reason_when_roles_keep_up(reg, rs):
    reg.seed_test_members(24, "t")
    ev = open_test_run(reg, rs)
    join(reg, rs, ev, reg.test_members())  # 24 joiners, 4 tanks, 7 healers: two runs
    assert rc.run_split_reason(reg, ev) is None
    ev2 = open_test_run(reg, rs, days=4)
    join(reg, rs, ev2, reg.test_members()[:12])  # one run's worth, minimums met
    assert rc.run_split_reason(reg, ev2) is None


def test_leftovers_only_once_full(reg, rs):
    ev, _ = _thin(reg, rs)
    players = rc.players_for(reg, ev)
    half = rc.board_rosters(reg, ev, [[p.signup_name for p in players[:5]]], 10)
    assert rc.leftovers(reg, ev, half) == [], "an open board with seats left: the bank is just unplaced"
    full = rc.board_rosters(reg, ev, [[p.signup_name for p in players[:5]], [p.signup_name for p in players[5:10]]], 10)
    assert len(rc.leftovers(reg, ev, full)) == 11


def test_another_run_hint_from_pool(reg, rs):
    ev, br = _thin(reg, rs)
    players = rc.players_for(reg, ev)
    board = rc.board_rosters(reg, ev, [[p.signup_name for p in players[:5]], [p.signup_name for p in players[5:10]]], 10)
    # the other tanks and healers never answered: the pool could make another run as they are
    hint = rc.another_run_hint(reg, ev, board, rc.run_split_reason(reg, ev))
    assert hint and hint.startswith("Leftovers, bench and pool") and "offspec" not in hint
    # everyone else says No thanks: nothing left to make one from
    join(reg, rs, ev, [m for m in reg.test_members() if str(m.discord_id) not in ev.signups], "out")
    assert rc.another_run_hint(reg, ev, board, rc.run_split_reason(reg, ev)) is None


def test_another_run_hint_needs_alts(reg, rs):
    ev, br = _thin(reg, rs)
    join(reg, rs, ev, [m for m in reg.test_members() if str(m.discord_id) not in ev.signups], "out")
    players = rc.players_for(reg, ev)
    board = rc.board_rosters(reg, ev, [[p.signup_name for p in players[:5]], [p.signup_name for p in players[5:10]]], 10)
    left = rc.leftovers(reg, ev, board)
    # two leftovers have a tank alt, two a healer alt: the swap line appears
    for p, (cls, spec) in zip(left[:4], [("Warrior", "Protection"), ("Paladin", "Protection"), ("Priest", "Holy"), ("Druid", "Restoration")]):
        sg = rc.signup_by_name(ev, p.signup_name)
        reg.add_character(sg.discord_id, sg.display_name, f"Alt{p.signup_name[:6]}", cls, spec, None, False)
    hint = rc.another_run_hint(reg, ev, board, rc.run_split_reason(reg, ev))
    assert hint and hint.endswith("with offspecs or alts.")


def test_can_cover_matching():
    assert rc._can_cover([{"tank", "healer"}, {"tank"}], ["tank", "healer"])
    assert not rc._can_cover([{"tank"}, {"tank"}], ["tank", "healer"])


@pytest.mark.slow
def test_one_healthy_roster_and_leftovers(reg, rs, monkeypatch):
    monkeypatch.setattr(rc, "PREVIEW_TIME_LIMIT_S", 4.0)
    ev, br = _thin(reg, rs)
    core = {m.display_name for m in br["tank"][:2] + br["healer"][:2]}
    layout, rosters = rc.split_preview(reg, rs, ev, "balanced")
    assert len(rosters) == 1 and len(rosters[0].selected) == 10
    seated = {p.signup_name for p in rosters[0].selected}
    assert core <= seated, "the tanks and healers are rostered first"
    left = rc.leftovers(reg, ev, rosters)
    assert len(left) == 11 and not seated & {p.signup_name for p in left}


@pytest.mark.slow
def test_minimums_beat_weights(reg, rs):
    """Even when the weights favour everyone else, the single roster still holds every healer the sheet has."""
    ev, br = _thin(reg, rs, tanks=2, healers=1, dps=12)
    healer = br["healer"][0]
    reg.config.raids.setdefault("barrow_deeps", {})["weights"] = {"rank": 3, "main": 2, "sat_out": 2, "signup_order": 50}
    ev.signups[str(healer.discord_id)].updated_at = "2099-01-01T00:00:00+00:00"  # the last to answer
    _players, roster = rc.propose(reg, rs, ev, save=False)
    assert healer.display_name in {p.signup_name for p in roster.selected}
    assert sum(1 for p in roster.selected if p.role == "tank") == 2


# ---- the run payload (web/api.py) and the Discord cards

def test_run_payload_carries_reason_and_leftovers(reg, rs, monkeypatch):
    from fastapi.testclient import TestClient
    from test_mcp import TOKEN, FakeBot, bearer

    from oibot_gm.web import app as web

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    client = TestClient(web.create_app(FakeBot(reg, rs)))
    ev, _ = _thin(reg, rs)
    d = client.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert d["split"]["runs"] == 1
    assert d["split"]["reason"] == {"bodies_allow": 2, "roles_allow": 1, "run": 2, "short": {"tank": 2, "healer": 2}}
    assert d["split"]["reason_text"].startswith("Enough people for 2 runs")
    assert d["board"]["leftovers"] == [] and d["board"]["another"] is None, "an empty board has nothing left over yet"
    players = rc.players_for(reg, ev)
    ev.layout = [[p.signup_name for p in players[:5]], [p.signup_name for p in players[5:10]]]
    rs.save(ev, "board")
    d = client.get(f"/api/run/{ev.key}", headers=bearer()).json()
    assert len(d["board"]["leftovers"]) == 11 and set(d["board"]["leftovers"]) <= {s["display_name"] for s in d["board"]["bank"]}
    assert d["board"]["another"].startswith("Leftovers, bench and pool")


def test_health_and_lock_cards_say_why(reg, rs):
    from conftest import lock_with_board

    from oibot_gm import raid_views as views

    tag = lambda kind, key: f"<{kind}:{key}>"  # noqa: E731
    ev, _ = _thin(reg, rs)
    team = rc.run_team(reg, ev)
    text = lambda view: "\n".join(c.content for c in view.walk_children() if c.__class__.__name__ == "TextDisplay")  # noqa: E731
    health = text(views.health_layout(reg, rs, ev, team, tag))
    assert "Enough people for 2 runs — a second is short <role:tank> 2   <role:healer> 2" in health
    assert "2 tank" not in health and "seated" not in health
    players = rc.players_for(reg, ev)
    lock_with_board(reg, rs, ev, [p.signup_name for p in players[:10]])
    card = text(views.lock_layout(reg, ev, team, tag, 0))
    assert "**Leftovers (11)**" in card and "<role:tank> 2" in card
    block = card.split("**Leftovers (11)**\n")[1].split("\n")
    assert all(ln.startswith("<spec:") for ln in block[:11]), "one member per line with the spec icon"
    assert "**Bench**" not in card, "leftovers are not listed twice"
