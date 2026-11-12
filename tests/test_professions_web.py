"""The Professions page's routes over HTTP (design §5.60): the catalog and each profession's crafters for every member,
the finder, the officers' fix for typed recipes the game's list lacks (preview first), and the one-member route taking a character's professions
with the confirm sentences on preview. The finders hide test puppets, so these use real members."""
from __future__ import annotations

from test_people_web import OFFICER, as_user, member, w  # noqa: F401 — `w` is the fixture


def crafter(w, n=0, recipe="Mooncloth Bag", skill=300):
    m = member(w, n)
    w.reg.save_character_table(m.discord_id, [{"label": m.main.label, "professions": [{"id": "tailoring", "skill": skill, "recipes": [{"name": recipe}]}]}], "t", officer=True)
    return w.reg.members[m.discord_id]


def typed_crafter(w, n, recipe):
    """A Tailoring recipe typed before the game's list (armory link §4.3: kept unmatched) — what the officers fix."""
    from oibot_gm.registry import Profession, Recipe

    m = member(w, n)
    m.main.professions = [Profession(id="tailoring", skill=300, recipes=[Recipe(name=recipe)])]
    w.reg.save(m, "typed recipe")
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
    assert tailoring["crafters"] == [{"label": m.main.label, "member": m.display_name, "uid": str(m.discord_id), "skill": 300, "profession": "tailoring", "cls": m.main.cls}]
    assert w.c.get("/api/professions/find?q=mooncloth%20bag", headers=h).json()["recipes"][0]["name"] == "Mooncloth Bag"
    assert w.c.get("/api/professions", headers=as_user(w, OFFICER)).json()["officer"] is True


def test_find_searches_the_whole_list_with_category_makes_and_reagents(w):
    """armory link §4.4: the finder reads the game's list — a listed recipe nobody makes is a row with no crafters, a
    typed recipe the list lacks still names its crafter, and a name the game lacks says so."""
    m = crafter(w)
    typed = typed_crafter(w, 1, "Bold Gloves")
    h = as_user(w, m.discord_id)
    d = w.c.get("/api/professions/find?q=ironfeather%20shoulders", headers=h).json()
    [row] = d["recipes"]
    assert row["name"] == "Ironfeather Shoulders" and row["crafters"] == [] and row["listed"] is True
    assert row["category"] == "Leather Pauldrons" and row["makes"]["name"] == "Ironfeather Shoulders" and row["reagents"]
    assert all({"id", "name", "quantity"} <= set(x) for x in row["reagents"])
    assert d["message"] == "Nobody in the guild makes Ironfeather Shoulders yet."
    d = w.c.get("/api/professions/find?q=mooncl", headers=h).json()
    assert d["recipes"][0]["name"] == "Mooncloth Bag" and d["recipes"][0]["crafters"] and d["recipes"][0]["listed"] is True, "crafted first"
    [row] = w.c.get("/api/professions/find?q=bold%20gloves", headers=h).json()["recipes"]
    assert row["listed"] is False and row["category"] is None and row["makes"] is None and row["reagents"] == []
    assert row["crafters"][0]["label"] == typed.main.label
    d = w.c.get("/api/professions/find?q=thunderfury", headers=h).json()
    assert d["recipes"] == [] and d["message"] == "That isn't a recipe in the game's list."
    cat = w.c.get("/api/professions", headers=h).json()["recipes"]
    assert {r["name"] for r in cat} == {"Mooncloth Bag", "Bold Gloves"}, "the page's catalog is still what somebody makes"


def test_rename_and_merge_routes_are_gone(w):
    """armory link C7: recipe names are the game's; Rename… and Merge… are retired."""
    typed_crafter(w, 0, "Mooncloth Bagg")
    oh = as_user(w, OFFICER)
    for path in ("/api/professions/rename", "/api/professions/merge"):
        assert w.c.post(path, json={"key": "mooncloth bagg", "name": "Mooncloth Bag", "into": "mooncloth bag", "preview": True}, headers=oh).status_code == 404
    assert not {"rename", "merge"} & set(w.c.get("/api/professions", headers=oh).json())


def test_unmatched_recipes_are_fixed_by_officers_preview_first(w):
    """§4.3: officers see the typed recipes the game's list doesn't have and pick the recipe or remove it."""
    m = typed_crafter(w, 0, "Mooncloth Bagg")
    mh = as_user(w, m.discord_id)
    assert w.c.get("/api/professions", headers=mh).json()["unmatched"] == [], "members don't get the fix list"
    body = {"uid": str(m.discord_id), "label": m.main.label, "profession": "tailoring", "name": "Mooncloth Bagg", "recipe_id": 10431}
    assert w.c.post("/api/professions/unmatched", json=body, headers=mh).status_code == 403
    oh = as_user(w, OFFICER)
    d = w.c.get("/api/professions", headers=oh).json()
    assert d["unmatched"] == [{"uid": str(m.discord_id), "member": m.display_name, "label": m.main.label, "profession": "tailoring", "name": "Mooncloth Bagg"}]
    p = w.c.post("/api/professions/unmatched", json={**body, "preview": True}, headers=oh).json()
    assert p == {"sentences": [f"{m.main.label}'s Tailoring lists Mooncloth Bag in place of Mooncloth Bagg."], "message": p["sentences"][0]}
    assert w.reg.members[m.discord_id].main.professions[0].recipes[0].id is None, "a preview writes nothing"
    assert w.c.post("/api/professions/unmatched", json=body, headers=oh).status_code == 200
    assert [(r.id, r.name) for r in w.reg.members[m.discord_id].main.professions[0].recipes] == [(10431, "Mooncloth Bag")]
    assert w.c.get("/api/professions", headers=oh).json()["unmatched"] == []
    r = w.c.post("/api/professions/unmatched", json={**body, "recipe_id": None}, headers=oh)
    assert r.status_code == 400 and r.json()["error"] == f"{m.main.label}'s Tailoring no longer lists Mooncloth Bagg."


def test_find_scopes_to_a_profession_and_counts_every_match(w):
    m = crafter(w)
    h = as_user(w, m.discord_id)
    d = w.c.get("/api/professions/find?q=radiant%20gloves&profession=tailoring", headers=h).json()
    assert [(r["profession"], r["id"]) for r in d["recipes"]] == [("tailoring", 55533)] and d["total"] == 1
    d = w.c.get("/api/professions/find?q=a", headers=h).json()
    assert len(d["recipes"]) == 25 and d["total"] > 100
    assert d["message"].splitlines()[-1] == f"And {d['total'] - 25} more recipes match."
    assert w.c.get("/api/professions/find?q=", headers=h).json() == {"q": "", "recipes": [], "total": 0, "message": ""}


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
    assert meta["skill_cap"] == 300 and meta["trade_slots"] == 2 and meta["list"][0] == {"id": "alchemy", "name": "Alchemy", "group": "crafting", "icon": "trade_alchemy", "makes": True}
    makes = {d["id"]: d["makes"] for d in meta["list"]}
    assert makes["fishing"] is False and makes["skinning"] is False and makes["cooking"] is True, "Fishing makes nothing"


def test_me_characters_preview_carries_the_professions_sentences(w):
    """The Me page's Professions… editor previews through /api/me/characters: the confirm prints the professions
    sentences in "your" voice (the Confirm rule — never an empty preview that saves on the first press); the write follows."""
    m = member(w)
    uid, label = m.discord_id, m.main.label
    h = as_user(w, uid)
    rev = w.c.get("/api/me", headers=h).json()["characters_rev"]
    body = {"rows": [{"label": label, "professions": [{"id": "alchemy", "skill": 300}]}], "rev": rev}
    p = w.c.post("/api/me/characters", json={**body, "preview": True}, headers=h)
    assert p.status_code == 200, p.text
    assert p.json()["sentences"] == ["Your Alchemy is recorded at 300."]
    assert not w.reg.members[uid].main.professions, "a preview writes nothing"
    r = w.c.post("/api/me/characters", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert w.reg.members[uid].main.professions[0].id == "alchemy"
    same = w.c.post("/api/me/characters", json={**body, "rev": r.json()["characters_rev"], "preview": True}, headers=h)
    assert same.status_code == 400 and same.json()["error"].endswith("professions are already as given.")


def test_a_skill_not_said_round_trips_as_null(w):
    """A profession recorded before its skill (a recipe said first, design §5.60 follow-up): null over HTTP, last among the
    crafters, and the editor's empty box sends null."""
    m = crafter(w, skill=None)
    other = crafter(w, 1, skill=120)
    h = as_user(w, OFFICER)
    d = w.c.get("/api/professions", headers=h).json()
    tailoring = next(p for p in d["professions"] if p["id"] == "tailoring")
    assert [(c["label"], c["skill"]) for c in tailoring["crafters"]] == [(other.main.label, 120), (m.main.label, None)]
    body = {"uid": str(m.discord_id), "character": m.main.label, "professions": [{"id": "alchemy", "skill": None}]}
    p = w.c.post("/api/members/set", json={**body, "preview": True}, headers=h)
    assert p.status_code == 200, p.text
    assert p.json()["sentences"][0] == f"{m.display_name}'s Alchemy is recorded."
    assert w.c.post("/api/members/set", json=body, headers=h).status_code == 200
    row = next(r for r in w.c.get("/api/members", headers=h).json()["rows"] if r["uid"] == str(m.discord_id))
    assert row["characters"][0]["professions"] == [{"id": "alchemy", "skill": None, "recipes": []}]


def test_the_site_shows_a_skill_not_said_as_the_icon_alone():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    line = (src / "components" / "ProfessionLine.tsx").read_text()
    assert "skill not said yet" in line and "p.skill != null &&" in line, "the icon alone, the tooltip says why"
    ed = (src / "components" / "ProfessionsEditor.tsx").read_text()
    assert "leave empty if you don't know" in ed and "skill: null" in ed, "an empty box is null, never 0"
    page = (src / "pages" / "Professions.tsx").read_text()
    assert "(b.skill ?? -1) - (a.skill ?? -1)" in page and "c.skill != null ?" in page, "no bar, sorted last"
    assert "skill: number | null" in (src / "api.ts").read_text()
