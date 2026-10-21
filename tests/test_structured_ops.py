"""The structured ops path (dual interfaces §2.3, §3.5 S2; design §5.43): typed drafts → configops.ops_from_fields →
describe_ops (Described) → authorize_ops (by privilege) → coalesce → apply_ops (one write per raid / schedule / buff /
family, one commit, one audit line); POST /api/ops/change `changes`; the admin routes as translators; the new
multi-field verbs Registry.set_buff_overrides / set_family_overrides; family slugs; WARN_OPS; MCP tools sending ops."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from conftest import OFFICER_ROLE, OWNER
from fastapi.testclient import TestClient
from test_mcp import TOKEN, FakeBot, bearer
from test_people import real

from oibot_gm import configops
from oibot_gm.configops import ConfigOp, MergedOp
from oibot_gm.registry import ConfigChanged, RegistryError
from oibot_gm.web import app as web


def commits(store) -> int:
    return int(subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=store.root, capture_output=True, text=True).stdout.strip())


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the verbs

def test_set_buff_overrides_is_one_write_and_a_refused_field_restores_the_buff(reg, store):
    n0 = commits(store)
    lines = reg.set_buff_overrides("blood_pact", {"scope": "raid", "strength": "1.5", "note": "tested"}, "Jon")
    assert len(lines) == 3 and commits(store) == n0 + 1
    b = reg.buff("blood_pact")
    assert (b.scope, b.strength, b.note) == ("raid", 1.5, "tested")
    before = dict(reg.config.buffs["blood_pact"])
    with pytest.raises(RegistryError, match="strength must be a number"):
        reg.set_buff_overrides("blood_pact", {"scope": "party", "strength": "lots"}, "Jon")
    assert reg.config.buffs["blood_pact"] == before and reg.buff("blood_pact").scope == "raid"  # the scope did not stick
    assert commits(store) == n0 + 1
    with pytest.raises(RegistryError):
        reg.set_buff_overrides("fortitude", {"status": "nope"}, "Jon")
    assert "fortitude" not in reg.config.buffs  # a buff with no overrides stays without an entry


def test_set_family_overrides_creates_with_a_name_and_restores_on_refusal(reg, store):
    fid = reg.family_slug("Stamina Buffs")
    assert fid == "stamina_buffs"
    n0 = commits(store)
    reg.set_family_overrides(fid, {"name": "Stamina Buffs", "value:all": "3", "value:mana": "2"}, "Jon")
    assert commits(store) == n0 + 1
    f = reg.profile.families[fid]
    assert f.name == "Stamina Buffs" and f.value == {"all": 3.0, "mana": 2.0}
    with pytest.raises(RegistryError, match="can't be negative"):
        reg.set_family_overrides(fid, {"note": "x", "value:tank": "-1"}, "Jon")
    assert reg.profile.families[fid].note != "x" and "note" not in reg.config.families[fid]
    with pytest.raises(RegistryError):
        reg.set_family_overrides("brand_new", {"status": "bogus"}, "Jon")
    assert "brand_new" not in reg.config.families


def test_family_slug_collisions_get_a_number(reg):
    existing = next(iter(reg.profile.families))
    name = reg.profile.families[existing].name
    slug = reg.family_slug(existing.replace("_", " "))
    assert slug == f"{existing}-2"
    reg.config.families[f"{existing}-2"] = {"name": name}
    assert reg.family_slug(existing.replace("_", " ")) == f"{existing}-3"
    buff_id = reg.profile.buffs[0].id
    assert reg.family_slug(buff_id) != buff_id  # a buff's own id is taken too (it is how "stands alone" is stored)
    assert reg.family_slug("!!!") == "family"


def test_aura_reset_sentence_counts_the_overrides(reg):
    assert reg.aura_reset_sentence().startswith("Nothing to reset")
    reg.set_buff_overrides("blood_pact", {"scope": "raid"}, "Jon")
    s = reg.aura_reset_sentence()
    assert "1 buff" in s and reg.buff("blood_pact").short in s


# ---------------------------------------------------------------- ops_from_fields, coalesce, Described, WARN_OPS

def test_ops_from_fields_buff_drafts_only_what_changed_and_new_family_first(reg):
    b = reg.buff("blood_pact")
    ops = configops.ops_from_fields(reg, "buff", "blood_pact", {"scope": b.scope, "strength": b.strength, "note": "seen on PTR", "family_new": "Stamina"})
    assert (ops[0].op, ops[0].field) == ("family_set", "name")  # the family exists before the buff joins it
    assert sorted((o.op, o.field) for o in ops[1:]) == [("aura_set", "family"), ("aura_set", "note")]  # scope / strength unchanged: no op
    assert ops[0].target == reg.family_slug("Stamina") == next(o.value for o in ops if o.field == "family")


def test_two_aura_fields_coalesce_into_one_write_and_one_line(reg, store):
    ops = configops.ops_from_fields(reg, "buff", "blood_pact", {"scope": "raid", "note": "n"})
    merged = configops.coalesce(ops)
    assert len(merged) == 1 and isinstance(merged[0], MergedOp)
    d = configops.describe_ops(reg, ops)
    assert len(d.lines) == 1 and [p.label for p in d.lines[0].parts] == ["Scope", "Note"]
    assert d.lines[0].label == reg.buff("blood_pact").short and d.warn  # a scope change is warn-class
    assert "Scope party → raid" in d.summary() and d.summary().endswith("2 changes")
    n0 = commits(store)
    res = run(configops.apply_ops(reg, ops, by="Jon", by_id=OWNER))
    assert res["applied"] and not res["refused"] and commits(store) == n0 + 1


def test_a_refused_field_restores_the_buff_through_the_core(reg, store):
    ops = configops.ops_from_fields(reg, "buff", "blood_pact", {"scope": "raid", "strength": "abc"})
    d = configops.describe_ops(reg, ops)
    assert d.lines[0].error and "strength must be a number" in d.lines[0].error  # the dry run on a shadow copy says it first
    assert reg.buff("blood_pact").scope == "party"  # …and touched nothing live
    n0 = commits(store)
    res = run(configops.apply_ops(reg, ops, by="Jon", by_id=OWNER))
    assert not res["applied"] and "strength must be a number" in res["error"] and res["results"][0]["ok"] is False
    assert reg.buff("blood_pact").scope == "party" and commits(store) == n0


def test_warn_class_and_consequences(reg):
    W = lambda op: configops.is_warn(reg, op)  # noqa: E731
    assert W(ConfigOp(op="set", path="signup_channel", value="<#5>")) and W(ConfigOp(op="set", path="timezone", value="Europe/London"))
    assert not W(ConfigOp(op="set", path="about", value="hi"))
    assert W(ConfigOp(op="role_add", value="1")) and W(ConfigOp(op="schedule_remove", target="barrow_deeps", field="default"))
    assert W(ConfigOp(op="schedule_set", target="barrow_deeps", field="default.active", value="false"))
    assert not W(ConfigOp(op="schedule_set", target="barrow_deeps", field="default.name", value="x"))
    assert W(ConfigOp(op="aura_set", target="blood_pact", field="family", value="own")) and not W(ConfigOp(op="aura_set", target="blood_pact", field="note", value="x"))
    assert W(ConfigOp(op="aura_reset")) and W(ConfigOp(op="aura_reset", target="blood_pact"))  # one buff too: both surfaces ask first
    assert W(ConfigOp(op="recruit", field="open", value="false")) and not W(ConfigOp(op="recruit", field="open", value="true"))
    assert W(ConfigOp(op="plain", target="dm", value="ignore")) and not W(ConfigOp(op="plain", target="runs", value="everyone"))
    reg.config.ops_channel_id = 777
    assert W(ConfigOp(op="plain", target="<#777>", value="answer"))
    reg.set_buff_overrides("blood_pact", {"note": "x"}, "Jon")
    d = configops.describe_ops(reg, [ConfigOp(op="aura_reset")])
    assert d.review and d.consequences == [reg.aura_reset_sentence()]
    d = configops.describe_ops(reg, [ConfigOp(op="set", path="about", value="Hello")])
    assert not d.review and d.lines[0].parts[0].after == "Hello"


def test_described_renders_mentions_per_surface(reg):
    reg.role_names[42] = "Raid Lead"
    d = configops.describe_ops(reg, [ConfigOp(op="set", path="signup_channel", value="<#9>"), ConfigOp(op="role_add", value="42")])
    chans = [{"id": "9", "name": "raid-signups"}]
    shown = d.json_for(reg, "web", chans)
    flat = json.dumps({**shown, "lines": [{k: x for k, x in ln.items() if k != "ops"} for ln in shown["lines"]]})  # `ops` = the machine form (ids stay ids)
    assert "#raid-signups" in flat and "@Raid Lead" in flat and "<#9>" not in flat and "<@&42>" not in flat
    assert "<#9>" in d.as_discord()
    assert "#raid-signups" in d.as_text(reg, chans)


def test_plain_whole_patch_becomes_one_write(reg, store):
    ops = configops.ops_from_fields(reg, "plain", None, {"groups": {"runs": ["everyone"], "test": []}, "set_channels": {"555": "answer"}, "dm": "answer"})
    assert len(configops.coalesce(ops)) == 1
    n0 = commits(store)
    res = run(configops.apply_ops(reg, ops, by="Jon", by_id=OWNER))
    assert not res["refused"], res
    p = reg.config.plain
    assert reg.plain_who("runs") == ["everyone"] and reg.plain_who("test") == [] and p.channels == {"555": "answer"} and p.dm == "answer"
    assert commits(store) == n0 + 1


# ---------------------------------------------------------------- privilege

def test_authorize_ops_by_privilege(reg):
    m, other = real(reg, 0), real(reg, 1, "Mage", "Fire")
    buff = configops.ops_from_fields(reg, "buff", "blood_pact", {"note": "x"})
    for role in ("member", "officer"):
        allowed, refused = configops.authorize_ops(reg, list(buff), m.discord_id, role=role)
        assert not allowed and refused == ["Only the owner can change raids & auras — on the site's Raids and Auras pages or with /gm config raid | aura."]
    assert configops.authorize_ops(reg, list(buff), OWNER, role="owner")[0]
    rank = ConfigOp(op="rank", member=other.display_name, rank="core")
    allowed, refused = configops.authorize_ops(reg, [rank], m.discord_id, role="member")
    assert not allowed and refused[0].startswith("Only officers can change ranks")
    assert configops.authorize_ops(reg, [ConfigOp(op="rank", member=other.display_name, rank="core")], m.discord_id, role="officer")[0]
    own = ConfigOp(op="dm", value="off")  # a member's own record: allowed (bind_self pins it to them)
    allowed, refused = configops.authorize_ops(reg, [own], m.discord_id, role="member")
    assert allowed and allowed[0].member == f"<@{m.discord_id}>" and not refused
    theirs = ConfigOp(op="dm", member=other.display_name, value="off")
    assert configops.authorize_ops(reg, [theirs], m.discord_id, role="member")[1][0].startswith("Only officers can change someone else's DM setting")
    assert configops.authorize_ops(reg, [ConfigOp(op="plain", target="runs", value="everyone")], OWNER, role="officer")[1] == [configops.HARD_OWNER_OPS["plain"]]
    assert configops.authorize_ops(reg, [ConfigOp(op="dm", value="off")], 1, role="outside")[1][0].startswith("Only members of")


# ---------------------------------------------------------------- POST /api/ops/change (typed changes)

@pytest.fixture
def owner_client(reg, rs, monkeypatch):
    """The owner through the MCP bearer (web/app.py mcp_viewer): role owner, via mcp."""
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(FakeBot(reg, rs)))


CHANGE = "/api/ops/change"


def test_a_routine_save_applies_directly_and_a_warn_class_one_asks_for_review(owner_client, reg, store):
    c = owner_client
    n0 = commits(store)
    r = c.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "seen on PTR", "status": "confirmed"}}], "apply": True}, headers=bearer())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] and not d.get("needs_review") and d["summary"] == "1 saved" and commits(store) == n0 + 1  # two fields, ONE write
    assert reg.buff("blood_pact").note == "seen on PTR"
    r = c.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid"}}], "apply": True}, headers=bearer())
    d = r.json()
    assert d["needs_review"] and d["token"] and d["warn"] and not d.get("applied") and reg.buff("blood_pact").scope == "party"
    line = d["described"]["lines"][0]
    assert line["parts"] == [{"label": "Scope", "before": "party", "after": "raid"}] and line["label"] == reg.buff("blood_pact").short
    r = c.post(CHANGE, json={"token": d["token"], "apply": True}, headers=bearer())
    assert r.status_code == 200 and r.json()["applied"] and reg.buff("blood_pact").scope == "raid"
    assert c.post(CHANGE, json={"token": d["token"], "apply": True}, headers=bearer()).status_code == 400  # single use


def test_confirmed_applies_a_warn_class_save_at_once(owner_client, reg):
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid"}}], "apply": True, "confirmed": True}, headers=bearer())
    assert r.json()["applied"] and reg.buff("blood_pact").scope == "raid"


def test_preview_writes_nothing_and_lists_refusals_per_write(owner_client, reg, store):
    n0 = commits(store)
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid", "strength": "lots"}},
                                                    {"kind": "settings", "fields": {"about": "We raid Tuesdays"}}]}, headers=bearer())
    d = r.json()
    assert r.status_code == 200 and d["token"] and commits(store) == n0 and reg.buff("blood_pact").scope == "party"
    assert [ln["error"] is not None for ln in d["described"]["lines"]] == [True, False]
    r = owner_client.post(CHANGE, json={"token": d["token"], "apply": True}, headers=bearer())
    d = r.json()
    assert d["summary"] == "1 saved, 1 refused" and d["note"].startswith("Everything else listed was saved")
    assert reg.config.about == "We raid Tuesdays" and reg.buff("blood_pact").scope == "party"  # the refused buff kept everything
    assert [x["ok"] for x in d["results"]] == [False, True]


def test_a_stale_section_rev_is_a_409(owner_client, reg):
    rev = owner_client.get("/api/auras", headers=bearer()).json()["rev"]
    reg.set_buff_overrides("fortitude", {"note": "someone else"}, "Ana")
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "mine"}}], "apply": True,
                                        "revs": {"config:buffs": rev}}, headers=bearer())
    assert r.status_code == 409 and r.json()["changed"] == ["buffs"] and "aura settings changed since you looked" in r.json()["error"]
    assert reg.buff("blood_pact").note != "mine"
    fresh = owner_client.get("/api/auras", headers=bearer()).json()["rev"]
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "mine"}}], "apply": True,
                                        "revs": {"config:buffs": fresh}}, headers=bearer())
    assert r.status_code == 200 and reg.buff("blood_pact").note == "mine"


def test_a_section_changed_between_preview_and_apply_is_refused(owner_client, reg):
    rev = reg.config_rev("buffs")
    d = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid"}}], "revs": {"config:buffs": rev}}, headers=bearer()).json()
    reg.set_buff_overrides("fortitude", {"note": "meanwhile"}, "Ana")
    r = owner_client.post(CHANGE, json={"token": d["token"], "apply": True}, headers=bearer())
    assert r.status_code == 409 and r.json()["changed"] == ["buffs"] and reg.buff("blood_pact").scope == "party"


def test_two_buffs_in_one_save_both_apply_against_one_rev(owner_client, reg, store):
    rev = reg.config_rev("buffs")
    n0 = commits(store)
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "a"}},
                                                    {"kind": "buff", "target": "fortitude", "fields": {"note": "b"}}], "apply": True,
                                        "revs": {"config:buffs": rev}}, headers=bearer())
    assert r.status_code == 200 and len(r.json()["applied"]) == 2, r.text  # the batch's own first write is not someone else's change
    assert commits(store) == n0 + 1  # one store.batch: one commit for the Save


def test_stale_token_re_previews_silently_and_applies_when_identical(owner_client, reg):
    changes = [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid"}}]
    d = owner_client.post(CHANGE, json={"changes": changes}, headers=bearer()).json()
    owner_client.app.state.ops_previews.clear()  # it expired
    r = owner_client.post(CHANGE, json={"token": d["token"], "apply": True, "changes": changes, "seen": d["fingerprint"]}, headers=bearer())
    assert r.status_code == 200 and r.json()["applied"] and reg.buff("blood_pact").scope == "raid"


def test_stale_token_with_different_lines_is_shown_again(owner_client, reg):
    changes = [{"kind": "buff", "target": "blood_pact", "fields": {"scope": "raid", "note": "x"}}]
    d = owner_client.post(CHANGE, json={"changes": changes}, headers=bearer()).json()
    owner_client.app.state.ops_previews.clear()
    reg.set_buff_overrides("blood_pact", {"note": "someone else's note"}, "Ana")  # what the person saw moved
    r = owner_client.post(CHANGE, json={"token": d["token"], "apply": True, "changes": changes, "seen": d["fingerprint"]}, headers=bearer())
    out = r.json()
    assert out["updated"] and out["needs_review"] and out["token"] and not out.get("applied") and reg.buff("blood_pact").scope == "party"


def test_a_token_from_before_a_restart(owner_client, reg):
    changes = [{"kind": "buff", "target": "blood_pact", "fields": {"note": "x"}}]
    r = owner_client.post(CHANGE, json={"token": "0000dead.abc", "apply": True}, headers=bearer())
    assert r.status_code == 400 and r.json()["restarted"] is True and "apply needs the token" in r.json()["error"]
    out = owner_client.post(CHANGE, json={"token": "0000dead.abc", "apply": True, "changes": changes, "seen": "whatever"}, headers=bearer()).json()
    assert out["restarted"] and out["needs_review"] and not out.get("applied")  # shown again, never applied without a press


def test_new_family_through_the_route_gets_a_slug(owner_client, reg):
    taken = next(iter(reg.profile.families))
    name = taken.replace("_", " ")
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "family", "fields": {"name": name, "value": {"all": 2}}}], "apply": True}, headers=bearer())
    assert r.status_code == 200, r.text
    assert reg.profile.families[f"{taken}-2"].value == {"all": 2.0}


def test_bad_changes_are_a_400_and_nothing_applies(owner_client, reg):
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "x"}}, {"kind": "galaxy", "fields": {}}], "apply": True},
                          headers=bearer())
    assert r.status_code == 400 and "unknown kind" in r.json()["error"] and reg.buff("blood_pact").note != "x"


# ---- members and officers on the route (web sessions)

def test_privilege_on_the_route_per_role(reg, rs, monkeypatch):
    from test_people_web import as_user, member, w as w_fixture  # noqa: F401

    from test_people_web import OFFICER, SECRET, WebBot
    import types

    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.setenv("OIBOT_WEB_URL", "http://127.0.0.1:8788")
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    monkeypatch.delenv("OIBOT_WEB_SESSION_EPOCH", raising=False)
    bot = WebBot(reg, rs)
    w = types.SimpleNamespace(bot=bot, c=TestClient(web.create_app(bot)), reg=reg, rs=rs)
    m = member(w)
    buff = [{"kind": "buff", "target": "blood_pact", "fields": {"note": "x"}}]
    for uid in (m.discord_id, OFFICER):
        h = as_user(w, uid)
        d = w.c.post(CHANGE, json={"changes": buff, "apply": True}, headers=h).json()
        assert not d["applied"] and d["refused"][0].startswith("Only the owner can change raids & auras"), d
    h = as_user(w, m.discord_id)
    d = w.c.post(CHANGE, json={"ops": [{"op": "dm", "value": "off"}], "apply": True}, headers=h).json()  # their own record: allowed (D40: no box, the route still answers)
    assert d["applied"] and reg.members[m.discord_id].dm_opt_out
    d = w.c.post(CHANGE, json={"ops": [{"op": "rank", "member": "Member0", "rank": "core"}], "apply": True}, headers=h).json()
    assert not d["applied"] and d["refused"][0].startswith("Only officers")
    assert w.c.post(CHANGE, json={"text": "make me core"}, headers=h).status_code == 403  # plain text stays officers' on the site
    h = as_user(w, OFFICER)
    d = w.c.post(CHANGE, json={"ops": [{"op": "dm", "member": m.display_name, "value": "on"}], "apply": True}, headers=h).json()
    assert d["applied"] and not reg.members[m.discord_id].dm_opt_out


# ---------------------------------------------------------------- the admin routes are translators (same answers, same state)

def _route_vs_shadow(c, reg, path, body, old):
    """POST `body` to `path`; `old(sh)` replays what the route did before phase 3 on a shadow copy (the same verbs, no
    write) and returns its message. Same message, same resulting config section."""
    sh = reg.shadow()
    want_msg = old(sh)
    r = c.post(path, json=body, headers=bearer())
    assert r.status_code == 200, r.text
    assert r.json()["message"] == want_msg
    return sh


def test_admin_aura_translator(owner_client, reg):
    body = {"id": "blood_pact", "scope": "raid", "strength": 1.5, "status": reg.buff("blood_pact").status, "note": "tested"}
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/aura", body,
                          lambda sh: "; ".join(sh.set_buff_override("blood_pact", f, body[f], "x") for f in ("scope", "strength", "note")))
    assert reg.config.buffs == sh.config.buffs
    r = owner_client.post("/api/admin/aura", json={"id": "blood_pact", "note": "tested"}, headers=bearer())
    assert r.json()["message"] == "blood_pact: no changes"
    r = owner_client.post("/api/admin/aura", json={"id": "blood_pact", "scope": "galaxy"}, headers=bearer())
    assert r.status_code == 400 and "scope must be one of" in r.json()["error"]


def test_admin_family_translator(owner_client, reg):
    fid = next(f for f in reg.profile.families if reg.profile.families[f].value)
    body = {"id": fid, "name": "Renamed", "value": {"all": 4, "mana": ""}}
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/family", body,
                          lambda sh: "; ".join([sh.set_family_override(fid, "name", "Renamed", "x"), sh.set_family_override(fid, "value", {"all": 4.0}, "x")]))
    assert reg.config.families == sh.config.families
    r = owner_client.post("/api/admin/family", json={"id": fid, "value": "all: 3"}, headers=bearer())
    assert r.status_code == 400 and r.json()["error"] == "value must be a map of beneficiary → number"


def test_admin_aura_reset_translator(owner_client, reg):
    reg.set_buff_overrides("blood_pact", {"note": "x"}, "Jon")
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/aura/reset", {}, lambda sh: sh.clear_aura_overrides("x"))
    assert reg.config.buffs == sh.config.buffs == {}


def test_admin_raid_translator(owner_client, reg):
    cur = reg.raid_def("barrow_deeps")
    body = {"instance": "barrow_deeps", "lock_hours_before": cur["lock_hours_before"] + 2, "signup_lead_hours": cur["signup_lead_hours"],
            "weights": {"rank": int(cur["weights"].get("rank", 0)) + 1}, "comp": {"tank": {"min": 1}}, "nudge": not cur.get("nudge", True)}
    want = {"lock_hours_before": str(body["lock_hours_before"]), "nudge": "true" if body["nudge"] else "false", "weight_rank": str(body["weights"]["rank"])}
    if str(((cur.get("comp") or {}).get("tank") or {}).get("min", "")) != "1":
        want["tank_min"] = "1"
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/raid", body, lambda sh: "; ".join(sh.set_raid_overrides("barrow_deeps", want, "x")))
    assert reg.config.raids["barrow_deeps"] == sh.config.raids["barrow_deeps"]
    assert owner_client.post("/api/admin/raid", json={"instance": "barrow_deeps", "lock_hours_before": body["lock_hours_before"]}, headers=bearer()).json()["message"] == "barrow_deeps: no changes"
    r = owner_client.post("/api/admin/raid", json={"instance": "barrow_deeps", "lock_hours_before": 1, "confirm_hours_before": 5}, headers=bearer())
    assert r.status_code == 400 and "lock must come before" in r.json()["error"]


def test_admin_schedule_translators(owner_client, reg):
    fields = {"kind": "weekly", "slots": ["Sat 20:00"], "name": "Alt run", "rosters": 2}
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/raid/schedule", {"instance": "barrow_deeps", "id": "alt", "fields": fields},
                          lambda sh: "; ".join(sh.set_schedule("barrow_deeps", "alt", fields, "x")))
    assert reg.config.raids["barrow_deeps"] == sh.config.raids["barrow_deeps"]
    fields = {"active": False, "lock_hours_before": None, "weights": {"regular": 6, "rank": None}}
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/raid/schedule", {"instance": "barrow_deeps", "id": "alt", "fields": fields},
                          lambda sh: "; ".join(sh.set_schedule("barrow_deeps", "alt", fields, "x")))
    assert reg.config.raids["barrow_deeps"] == sh.config.raids["barrow_deeps"]
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/raid/schedule/remove", {"instance": "barrow_deeps", "id": "alt"},
                          lambda sh: sh.remove_schedule("barrow_deeps", "alt", "x"))
    assert reg.config.raids["barrow_deeps"] == sh.config.raids["barrow_deeps"]
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/raid/reset", {"instance": "barrow_deeps"}, lambda sh: sh.clear_raid_override("barrow_deeps", "x"))
    assert "barrow_deeps" not in reg.config.raids


def test_admin_config_translator(owner_client, reg):
    sh = _route_vs_shadow(owner_client, reg, "/api/admin/config", {"field": "timezone", "value": "America/New_York"}, lambda sh: "timezone = America/New_York")
    assert reg.config.timezone == "America/New_York"
    r = owner_client.post("/api/admin/config", json={"field": "news_keywords", "value": ["phase 2", "hotfix"]}, headers=bearer())
    assert r.json()["message"] == "news_keywords = phase 2, hotfix" and reg.config.news_keywords == ["phase 2", "hotfix"]
    r = owner_client.post("/api/admin/config", json={"field": "officer_roles", "value": [str(OFFICER_ROLE), "777"]}, headers=bearer())
    assert r.status_code == 200 and r.json()["message"].startswith("officer roles: ") and reg.config.officer_role_ids == [OFFICER_ROLE, 777]
    r = owner_client.post("/api/admin/config", json={"field": "officer_roles", "value": [str(OFFICER_ROLE), "777"]}, headers=bearer())
    assert r.json()["message"].startswith("officer roles unchanged: ")
    r = owner_client.post("/api/admin/config", json={"field": "channel:signup", "value": "12345"}, headers=bearer())
    assert r.status_code == 200 and reg.config.signup_channel_id == 12345
    assert owner_client.post("/api/admin/config", json={"field": "galaxy", "value": 1}, headers=bearer()).json()["error"] == "unknown setting galaxy"


def test_admin_plain_and_recruitment_translators(owner_client, reg):
    patch = {"groups": {"runs": ["everyone"], "test": []}, "channels": {"555": "answer"}, "dm": "answer"}
    sh = reg.shadow()
    want = sh.set_plain_policy(sh.merge_plain_policy(patch), "x")
    r = owner_client.post("/api/admin/plain-permissions", json=patch, headers=bearer())
    assert r.status_code == 200 and r.json()["message"] == want and reg.config.plain == sh.config.plain
    qs = [{"label": "Main spec?", "hint": "the one you raid as", "required": True}, {"label": "Logs?", "hint": None, "required": False}]
    body = {"open": False, "note": "Healers wanted", "questions": qs, "member_role_ids": ["9001"]}
    sh = reg.shadow()
    want = sh.set_recruitment("x", open=False, note="Healers wanted", questions=qs, member_role_ids=[9001])
    r = owner_client.post("/api/admin/recruitment", json=body, headers=bearer())
    assert r.status_code == 200 and r.json()["message"] == want
    assert reg.config.recruitment == sh.config.recruitment and reg.config.member_role_ids == [9001]
    r = owner_client.post("/api/admin/recruitment", json={"note": "x" * 300}, headers=bearer())
    assert r.status_code == 400 and "limited to 200" in r.json()["error"]


def test_admin_routes_are_translators_not_verb_callers():
    """§2.10's second assertion: no /api/admin/* setup route calls a Registry verb directly — they build changes and go
    through the one core (configops.apply_ops)."""
    import inspect

    from oibot_gm.web import api_ops

    src = inspect.getsource(api_ops.install_ops_api)
    body = src[src.index("# ---- the admin routes"):]
    for verb in ("set_buff_override", "set_family_override", "clear_aura_overrides", "set_raid_override", "clear_raid_override", "set_schedule", "remove_schedule",
                 "set_recruitment", "set_plain_policy", "set_channel", "apply_async", "set_officer_role"):
        assert f".{verb}(" not in body, f"an admin route calls {verb} directly"
    from oibot_gm.web import api as api_mod

    api_src = inspect.getsource(api_mod)
    for route in ("/api/admin/aura\"", "/api/admin/family\"", "/api/admin/raid\"", "/api/admin/config\"", "/api/admin/recruitment\"", "/api/admin/plain-permissions\""):
        assert f"@app.post(\"{route[:-1]}\")" not in api_src, f"{route} is declared outside api_ops"


# ---------------------------------------------------------------- MCP sends typed changes

def test_mcp_setup_tools_send_typed_changes(monkeypatch):
    import httpx

    from oibot_gm import mcp_server as ms

    sent = []

    def handler(request):
        p = request.url.path
        if p == "/api/guilds":
            return httpx.Response(200, json={"guilds": [{"key": "demo", "name": "Demo"}]})
        if p == "/api/raids":
            return httpx.Response(200, json={"raids": [{"id": "barrow_deeps", "name": "Barrow Deeps"}]})
        if p == "/api/ops/change":
            b = json.loads(request.read())
            sent.append(b)
            if b.get("apply"):
                return httpx.Response(200, json={"applied": ["blood_pact: note = x"], "refused": [], "summary": "1 saved"})
            return httpx.Response(200, json={"described": {"lines": [{"label": "Blood Pact"}], "consequences": []}, "refused": [], "review": False, "describe": ["d"], "token": "t"})
        return httpx.Response(404, json={"error": "no such route"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        assert ms.aura_set("blood_pact", "note", "x") == "blood_pact: note = x"
        assert sent[-1] == {"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "x"}}], "apply": True, "confirmed": True}
        ms.family_set(None, fields={"name": "Stamina", "value": {"all": 3, "spec:Holy": 1}})
        assert sent[-1]["changes"] == [{"kind": "family", "target": None, "fields": {"name": "Stamina", "value": {"all": 3, "spec:Holy": 1}}}]  # typed, never 'all: 3'
        ms.raid_set("barrow", fields={"slots": ["Tue 19:30"], "weight_rank": 3})
        assert sent[-1]["changes"][0] == {"kind": "raid", "target": "barrow_deeps", "fields": {"slots": ["Tue 19:30"], "weight_rank": 3}}
        ms.schedule_set("barrow", "alt", fields={"active": False})
        assert sent[-1]["changes"][0] == {"kind": "schedule", "target": "barrow_deeps:alt", "fields": {"active": False}}
        ms.set_channel("signup", "#raids")
        assert sent[-1]["changes"][0] == {"kind": "settings", "fields": {"channel:signup": "#raids"}}
        pv = ms.aura_set("blood_pact", "scope", "raid", preview=True)
        assert "apply" not in sent[-1] and pv["preview"] == [{"label": "Blood Pact"}]  # the preview IS the Described structure
    finally:
        ms.use(None)


def test_mcp_tools_end_to_end_against_the_bot(owner_client, reg, monkeypatch):
    """The tool → the real route → the core (the owner's bearer): a refused write raises with the reason."""
    import httpx

    from mcp.server.fastmcp.exceptions import ToolError

    from oibot_gm import mcp_server as ms

    def handler(request):
        r = owner_client.request(request.method, request.url.path, content=request.read(), headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    ms.use(ms.Api(url="http://bot", token=TOKEN, transport=httpx.MockTransport(handler)))
    try:
        out = ms.aura_set("blood_pact", fields={"scope": "raid", "note": "via MCP"})
        assert "scope = raid" in out and reg.buff("blood_pact").note == "via MCP"
        with pytest.raises(ToolError, match="strength must be a number"):
            ms.aura_set("blood_pact", "strength", "lots")
        pv = ms.aura_reset(preview=True)
        assert pv["review"] and pv["consequences"] == [reg.aura_reset_sentence()]
    finally:
        ms.use(None)


# ---------------------------------------------------------------- review fixes (G1a findings)

def test_a_family_value_that_is_not_a_number_is_a_refusal_never_a_500(owner_client, reg):
    """Regression: ops_from_fields compared float('abc') with the stored value and raised a bare ValueError, which the
    route did not catch (500). The op now goes through and the verb refuses it in its own words (§2.7)."""
    fid = "windfury_totem"
    key = next(iter(reg.profile.families[fid].value))
    before = dict(reg.profile.families[fid].value)
    ops = configops.ops_from_fields(reg, "family", fid, {f"value:{key}": "abc"})  # no ValueError
    assert [(o.op, o.field, o.value) for o in ops] == [("family_set", f"value:{key}", "abc")]
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "family", "target": fid, "fields": {f"value:{key}": "abc"}}], "apply": True}, headers=bearer())
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d["applied"] and "isn't a number" in d["results"][0]["line"] and d["note"] == "Nothing was changed."
    r = owner_client.post("/api/admin/family", json={"id": fid, f"value:{key}": "abc"}, headers=bearer())
    assert r.status_code == 400 and "isn't a number" in r.json()["error"]
    assert reg.profile.families[fid].value == before


def test_an_unexpected_error_translating_a_draft_is_reported_not_a_bare_500(owner_client, reg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug in a translator")
    monkeypatch.setattr(configops, "ops_from_changes", boom)
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "buff", "target": "blood_pact", "fields": {"note": "x"}}], "apply": True}, headers=bearer())
    assert r.status_code == 500 and r.json() == {"error": configops.GENERIC_ERROR}  # the generic line, never a traceback or a type name
    r = owner_client.post("/api/admin/aura", json={"id": "blood_pact", "note": "x"}, headers=bearer())
    assert r.status_code == 500 and r.json() == {"error": configops.GENERIC_ERROR}


def test_officer_roles_is_the_whole_list_never_a_bare_string(owner_client, reg):
    """Regression: a string '123' was looped character by character (roles 1, 2, 3) and the real officer roles removed."""
    reg.config.officer_role_ids = [OFFICER_ROLE]
    with pytest.raises(RegistryError, match="whole list"):
        configops.ops_from_fields(reg, "settings", None, {"officer_roles": "123"})
    r = owner_client.post(CHANGE, json={"changes": [{"kind": "settings", "fields": {"officer_roles": "123"}}], "apply": True, "confirmed": True}, headers=bearer())
    assert r.status_code == 400 and "whole list" in r.json()["error"]
    r = owner_client.post("/api/admin/config", json={"field": "officer_roles", "value": "123"}, headers=bearer())
    assert r.status_code == 400
    assert reg.config.officer_role_ids == [OFFICER_ROLE]
    assert [o.op for o in configops.ops_from_fields(reg, "settings", None, {"officer_roles": []})] == ["role_remove"]  # [] = none, said as a list


def test_resetting_one_aura_is_reviewed_with_its_sentence_on_every_surface(owner_client, reg):
    """The Discord AuraWizard asks before a per-buff reset (Wizard.decide); the web's Save and MCP's preview now ask too,
    with the same sentence from code (configops.aura_reset_one_sentence)."""
    reg.set_buff_overrides("blood_pact", {"scope": "raid"}, "Jon")
    sentence = configops.aura_reset_one_sentence(reg, "blood_pact")
    assert reg.buff("blood_pact").short in sentence and "its own family's beneficiaries" in sentence
    d = owner_client.post(CHANGE, json={"changes": [{"kind": "aura_reset", "target": "blood_pact"}], "apply": True}, headers=bearer()).json()
    assert d["needs_review"] and not d.get("applied") and reg.buff("blood_pact").scope == "raid"
    assert sentence in d["described"]["consequences"]
    r = owner_client.post(CHANGE, json={"token": d["token"], "apply": True}, headers=bearer())
    assert r.json()["applied"] and reg.buff("blood_pact").scope == "party"
    assert configops.aura_reset_one_sentence(reg, "blood_pact").startswith("Nothing to reset")


def test_stands_alone_targets_the_buffs_own_family_in_ops_and_verbs(reg, store):
    """Regression: 'Stands alone' on a buff in a shared family, then a Who-benefits edit, wrote the SHARED family
    (Leader of the Pack) — profile.families has no entry for the buff's own id yet, and the code fell back to the
    family it was leaving. The edit now targets the buff's own family (id = the buff's id)."""
    from oibot_gm import flows_config as fc

    shared_before = dict(reg.profile.families["leader_of_the_pack"].value)
    w = fc.AuraWizard(reg, OWNER)
    w.bid = "moonkin_aura"
    w.put("family", "own")
    assert w.target_family() == ("moonkin_aura", {}) and w.target_name() == reg.buff("moonkin_aura").short
    w.fam_draft["value:spell"] = "2"
    ch = w.changes()
    assert [(c["kind"], c["target"]) for c in ch] == [("family", "moonkin_aura"), ("buff", "moonkin_aura")]
    d = w.described()
    assert d.lines[0].label.startswith(reg.buff("moonkin_aura").short) and "Leader" not in d.lines[0].label
    res = run(configops.apply_ops(reg, configops.ops_from_changes(reg, ch), by="Jon", by_id=OWNER))
    assert not res["refused"], res
    assert reg.buff("moonkin_aura").family_id == "moonkin_aura" and reg.profile.families["moonkin_aura"].value == {"spell": 2.0}
    assert reg.profile.families["leader_of_the_pack"].value == shared_before and reg.buff("leader_of_the_pack").family_id == "leader_of_the_pack"


def test_plain_text_diff_prints_the_described_structure(reg):
    """The Discord plain-text confirm (discord_policy.diff_text) prints setup writes as Described.as_discord(): one
    bullet per write as apply_ops will make it (two fields of one buff = one line), consequences as ⚠; run actions keep
    their describe() line; a plain-text change that takes 'act' away from the asking channel says so."""
    from oibot_gm.discord_policy import diff_text

    reg.set_buff_overrides("blood_pact", {"note": "old"}, "Jon")
    ops = [ConfigOp(op="aura_set", target="blood_pact", field="scope", value="raid"), ConfigOp(op="aura_set", target="blood_pact", field="note", value="tested"),
           ConfigOp(op="aura_reset")]
    text = run(diff_text(reg, ops, ["Only the owner can change x"], 4242))
    short = reg.buff("blood_pact").short
    assert text.count(f"• **{short}** —") == 1 and "Scope party → **raid**" in text and "Note old → **tested**" in text
    assert f"⚠ {reg.aura_reset_sentence()}" in text and text.endswith("⛔ Only the owner can change x")
    reg.config.plain.channels["4242"] = "act"
    text = run(diff_text(reg, [ConfigOp(op="plain", target="<#4242>", value="ignore")], [], 4242))
    assert "-# In this channel the bot would ignore @mentions" in text
