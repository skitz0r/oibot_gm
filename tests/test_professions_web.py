"""The Professions page's routes over HTTP (design §5.60): the catalog and each profession's crafters for every member,
the finder, the officers' rename / merge (preview first), and the one-member route taking a character's professions
with the confirm sentences on preview. The finders hide test puppets, so these use real members."""
from __future__ import annotations

from test_people_web import OFFICER, as_user, member, w  # noqa: F401 — `w` is the fixture


def crafter(w, n=0, recipe="Mooncloth Bag", skill=300):
    m = member(w, n)
    w.reg.save_character_table(m.discord_id, [{"label": m.main.label, "professions": [{"id": "tailoring", "skill": skill, "recipes": [{"name": recipe}]}]}], "t", officer=True)
    return w.reg.members[m.discord_id]


def test_professions_page_reads_catalog_and_find(w):
    m = crafter(w)
    h = as_user(w, m.discord_id)
    r = w.c.get("/api/professions", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert [p["id"] for p in d["professions"]][:3] == ["alchemy", "blacksmithing", "enchanting"] and d["officer"] is False
    assert d["recipes"][0]["name"] == "Mooncloth Bag" and d["recipes"][0]["crafters"][0]["skill"] == 300
    tailoring = next(p for p in d["professions"] if p["id"] == "tailoring")
    assert tailoring["crafters"] == [{"label": m.main.label, "member": m.display_name, "uid": str(m.discord_id), "skill": 300, "profession": "tailoring"}]
    assert w.c.get("/api/professions/find?q=mooncloth%20bag", headers=h).json()["recipes"][0]["name"] == "Mooncloth Bag"
    assert w.c.get("/api/professions", headers=as_user(w, OFFICER)).json()["officer"] is True


def test_rename_and_merge_are_officers_only_and_preview_first(w):
    m = crafter(w)
    crafter(w, 1, recipe="Mooncloth Bags")
    mh = as_user(w, m.discord_id)
    assert w.c.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Satchel"}, headers=mh).status_code == 403
    oh = as_user(w, OFFICER)
    p = w.c.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Satchel", "preview": True}, headers=oh).json()
    assert p["sentences"][0] == "Mooncloth Bag is renamed Mooncloth Satchel."
    assert w.reg.members[m.discord_id].main.professions[0].recipes[0].name == "Mooncloth Bag", "a preview writes nothing"
    assert w.c.post("/api/professions/rename", json={"key": "mooncloth bag", "name": "Mooncloth Satchel"}, headers=oh).status_code == 200
    assert w.c.get("/api/professions/find?q=mooncloth%20satchel", headers=oh).json()["recipes"]
    r = w.c.post("/api/professions/rename", json={"key": "mooncloth satchel", "name": "Mooncloth Bags"}, headers=oh)
    assert r.status_code == 400 and r.json()["error"] == "Mooncloth Bags is already a recipe — merge into it instead."
    p = w.c.post("/api/professions/merge", json={"key": "mooncloth satchel", "into": "mooncloth bags", "preview": True}, headers=oh).json()
    assert p["sentences"][0] == "Mooncloth Satchel is merged into Mooncloth Bags." and p["message"] == p["sentences"][0]
    assert w.c.post("/api/professions/merge", json={"key": "mooncloth satchel", "into": "mooncloth bags"}, headers=oh).status_code == 200
    cat = w.c.get("/api/professions", headers=oh).json()["recipes"]
    assert [(e["name"], len(e["crafters"])) for e in cat] == [("Mooncloth Bags", 2)]


def test_members_set_takes_professions_with_the_confirm_sentences(w):
    m = member(w)
    uid, label = m.discord_id, m.main.label
    h = as_user(w, OFFICER)
    body = {"uid": str(uid), "character": label, "professions": [{"id": "alchemy", "skill": 300}]}
    p = w.c.post("/api/members/set", json={**body, "preview": True}, headers=h)
    assert p.status_code == 200, p.text
    assert p.json()["sentences"] == [f"{w.reg.members[uid].display_name}'s Alchemy is recorded at 300."]
    assert not w.reg.members[uid].main.professions, "a preview writes nothing"
    assert w.c.post("/api/members/set", json=body, headers=h).status_code == 200
    rows = w.c.get("/api/members", headers=h).json()["rows"]
    row = next(r for r in rows if r["uid"] == str(uid))
    assert row["characters"][0]["professions"][0]["id"] == "alchemy"
    meta = w.c.get("/api/meta", headers=h).json()["professions"]
    assert meta["skill_cap"] == 300 and meta["trade_slots"] == 2 and meta["list"][0] == {"id": "alchemy", "name": "Alchemy", "group": "crafting", "icon": "trade_alchemy"}
