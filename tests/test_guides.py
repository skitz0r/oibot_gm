"""Design §5.55: the leveling-guide tool. A member's RestedXP import string + the BattleTag it was issued to → their own
folder of outputs (the companion addon, the one-file explorer, readable text) on the Me page alone. The decoder round-trips
a string the inverse builds; the wrong key is a refusal in words; the routes resolve the folder from the session and never
from a parameter; MCP reads a status but never builds."""
from __future__ import annotations

import base64
import gzip
import importlib
import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from test_mcp import TOKEN, FakeBot, bearer

from oibot_gm import guides as g
from oibot_gm.guides import rxp_decode
from oibot_gm.web import app as web

build_mod = importlib.import_module("oibot_gm.guides.build")  # the package exports build() the function under the same name
SECRET = "s" * 40
ME, OTHER = 320_000_000_000_000_001, 320_000_000_000_000_002
TAG = "Jon#1234"

GUIDE_A = """#name 1-6 Testwood
#displayname 1-6 Testwood
#group Test Alliance
#next 6-12 Testshore
<<Alliance
step
.goto 1429/0,-8949.95,-132.49
.accept 783 >>Accept |cRXP_FRIENDLY_A Threat Within|r
step
.goto 1429/0,-8900,-150
.turnin 783 >>Turn in A Threat Within
.xp 2
step
#optional
.dungeon DM
.goto Westfall,45,60 >>Walk to the farm
step
.goto 1429/0,-8800,-200
.fly Stormwind >>Fly to Stormwind
"""
GUIDE_B = """#name 6-12 Testshore
#displayname 6-12 Testshore
#group Test Alliance
step
.goto 1436/0,-10600,1030
.accept 1 >>Pick up the first quest
step
.goto 1436/0,-10620,1050
.turnin 1 >>Turn it in
step
.hs Sentinel Hill
"""
STARTER = """#name 1-5 Starter Isle
#displayname 1-5 Starter Isle
#group Free Starter
step
.goto 1429/0,-8949,-132
.accept 5 >>A free step
"""


def fixture_string(sections: int = 1) -> str:
    return rxp_decode.encode([GUIDE_A, GUIDE_B], TAG, sections=sections)


@pytest.fixture
def game(profile) -> g.GuideGame:
    return g.GuideGame.from_profile(profile.guides)


@pytest.fixture(autouse=True)
def _guides_out(tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "OUT", tmp_path / "guides-out")
    monkeypatch.delenv(build_mod.ADDON_ENV, raising=False)


# ---------------------------------------------------------------- the decoder

def test_a_string_the_inverse_builds_decodes_back_to_the_same_guides():
    for sections in (1, 2):
        s = fixture_string(sections)
        assert s.startswith("2|") and s.count("%") == sections
        assert g.decode("  guide: " + s + "\n", TAG) == [GUIDE_A, GUIDE_B], "junk around the string is dropped, like the addon does"
    assert g.decode(fixture_string(), TAG.lower()) == [GUIDE_A, GUIDE_B], "the key is case-insensitive (the addon lowercases it)"


def test_the_wrong_key_and_a_non_string_are_refusals_in_words():
    with pytest.raises(g.GuideError, match="doesn't unlock"):
        g.decode(fixture_string(), "Jon#4321")
    with pytest.raises(g.GuideError, match="isn't a RestedXP import string"):
        g.decode("hello there", TAG)
    with pytest.raises(g.GuideError, match="isn't a RestedXP import string"):
        g.decode("2|", TAG)
    for bad, why in (("", "Enter the BattleTag"), ("Jon", "name, a # and 4 to 7 digits"), ("Jon#12", "name, a # and 4 to 7 digits"), ("Jo n#1234", "name, a # and")):
        with pytest.raises(g.GuideError, match=why):
            g.validate_battletag(bad)
    assert g.validate_battletag(" Jon#12345 ") == "Jon#12345"


# ---------------------------------------------------------------- the build

def test_build_writes_the_four_files_and_a_status_the_page_reads(tmp_path, game):
    folder = tmp_path / "me"
    assert g.status(folder) is None
    st = g.build(fixture_string(), TAG, folder, game)
    assert st["guides"] == 2 and st["steps"] == 7 and st["range"] == "1–12" and st["starter"] == 0
    assert set(g.FILES) <= {p.name for p in folder.iterdir()} and (folder / "guides.json").is_file() and (folder / "status.json").is_file()
    assert g.status(folder)["guides"] == 2 and set(st["sizes"]) == set(g.FILES)
    [row] = st["groups"]
    assert row["group"] == "Test Alliance" and row["guides"] == 2 and row["steps"] == 7 and (row["lo"], row["hi"]) == (1, 12)
    assert row["zones"][:2] == ["Elwynn Forest", "Westfall"] and row["dungeons"] == ["The Deadmines"] and row["km"] >= 0 and not row["starter"]

    html = (folder / "explorer.html").read_text()
    assert html.startswith("<!doctype html>") and "fetch('guides.txt')" in html, "the vendored page keeps its file mode for a served folder"
    m = re.search(r'<script type="text/plain" id="guides-data">([A-Za-z0-9+/=]+)</script>', html)
    assert m, "the dataset is inlined, so the one file works from file://"
    data = json.loads(gzip.decompress(base64.b64decode(m.group(1))))
    assert [x["name"] for x in data["guides"]] == ["1-6 Testwood", "6-12 Testshore"] and data["guides"][0]["src"] == "paid"
    assert data["guides"][0]["steps"][0]["pts"][0]["m"] == 1429 and data["zones"]["1429"]["name"] == "Elwynn Forest" if "1429" in data["zones"] else data["zones"][1429]["name"] == "Elwynn Forest"
    assert data["guides"][0]["steps"][2]["dungeon"] == [{"t": "DM", "cond": ""}] and data["guides"][0]["steps"][1]["xp"] == 2
    assert "GUIDES_DATA</script>" not in html

    with zipfile.ZipFile(folder / f"{build_mod.ADDON_NAME}.zip") as z:
        names = z.namelist()
        assert "RXPGuides_LocalGuides/RXPGuides_LocalGuides.toc" in names and "RXPGuides_LocalGuides/guides.xml" in names
        assert sorted(n for n in names if n.endswith(".lua")) == ["RXPGuides_LocalGuides/guides/1-6_Testwood.lua", "RXPGuides_LocalGuides/guides/6-12_Testshore.lua"]
        toc = z.read("RXPGuides_LocalGuides/RXPGuides_LocalGuides.toc").decode()
        assert "## Interface: 16001, 11509" in toc and "## Dependencies: RXPGuides" in toc
        lua = z.read("RXPGuides_LocalGuides/guides/1-6_Testwood.lua").decode()
        assert lua.startswith("RXPGuides.RegisterGuide([=[\n#name 1-6 Testwood") and lua.rstrip().endswith("]=])")
        assert '<Script file="guides/1-6_Testwood.lua"/>' in z.read("RXPGuides_LocalGuides/guides.xml").decode()
    with zipfile.ZipFile(folder / "guide-text.zip") as z:
        assert sorted(z.namelist()) == ["guides/Test Alliance.md", "summary/Test Alliance - summary.md"]
        md = z.read("guides/Test Alliance.md").decode()
        assert "## 1-6 Testwood" in md and "- Accept **A Threat Within**" in md and "📍 -8949.95, -132.49" in md and "*(only if: Alliance)*" not in md
        assert "**3.**\n   - Walk to the farm" in md and "<sub>optional</sub>" in md and "Fly to Stormwind" in md
        summary = z.read("summary/Test Alliance - summary.md").decode()
        assert "| 1-6 Testwood | 4 | Elwynn Forest (×2) → Westfall | The Deadmines |" in summary


def test_a_failed_build_keeps_the_previous_one_and_clear_removes_it(tmp_path, game):
    folder = tmp_path / "me"
    g.build(fixture_string(), TAG, folder, game)
    with pytest.raises(g.GuideError):
        g.build(fixture_string(), "Jon#9999", folder, game)
    assert g.status(folder)["guides"] == 2 and not folder.with_name("me.tmp").exists()
    with pytest.raises(g.GuideError, match="Paste the import string first"):
        g.build("   ", TAG, folder, game)
    with pytest.raises(g.GuideError, match="bigger than any guide export"):
        g.build("1|" + "A" * (g.TEXT_MAX + 1), TAG, folder, game)
    assert g.clear(folder) and g.status(folder) is None and not g.clear(folder)


def test_the_hosts_addon_zip_adds_the_free_starter_guides_to_the_explorer_only(tmp_path, game):
    zp = tmp_path / "RXPGuides.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("RXPGuides/Guides/forever/starter.lua", "RXPGuides.RegisterGuide([=[\n" + STARTER + "]=])\nRXPGuides.RegisterGuide([[\n#name 1-3 Other\n#group Free Starter\nstep\n.accept 2 >>x\n]])\n")
        z.writestr("RXPGuides/Guides/forever/dungeon/dm.lua", "RXPGuides.RegisterGuide([=[\n#name Deadmines\n#group Dungeons\nstep\n.goto 1436/0,-11200,1500\n]=])\n")
        z.writestr("RXPGuides/Guides/tbc/old.lua", "RXPGuides.RegisterGuide([=[\n#name 58-60 Old\n#group Old\nstep\n.accept 9 >>x\n]=])\n")
    assert len(build_mod.starter_guides(zp, "forever")) == 2, "the folder's own files, not the dungeon or another version's"
    folder = tmp_path / "me"
    st = g.build(fixture_string(), TAG, folder, game, addon_zip=zp)
    assert st["starter"] == 2 and st["guides"] == 2, "the paid count is the member's own"
    assert [r["group"] for r in st["groups"]] == ["Free Starter", "Test Alliance"] and st["groups"][0]["starter"]
    html = (folder / "explorer.html").read_text()
    data = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="guides-data">([^<]+)<', html).group(1))))
    assert [x["src"] for x in data["guides"]] == ["starter", "starter", "paid", "paid"]
    with zipfile.ZipFile(folder / "RXPGuides_LocalGuides.zip") as z:
        assert not any("Starter" in n for n in z.namelist()), "the addon ships the free guides itself"
    assert build_mod.starter_guides(tmp_path / "missing.zip", "forever") == []


# ---------------------------------------------------------------- the routes

class FakeMember:
    def __init__(self, uid, name):
        self.id, self.display_name = uid, name


class FakeGuild:
    def __init__(self, gid):
        self.id, self.icon, self.roles = gid, None, []


class GuideBot(FakeBot):
    """FakeBot with a Discord guild and two members, so a session cookie resolves to a viewer."""

    def __init__(self, reg, rs):
        super().__init__(reg, rs)
        self.guild = FakeGuild(reg.config.discord_guild_id)
        self.people = {ME: FakeMember(ME, "Jon"), OTHER: FakeMember(OTHER, "Ash")}

    def get_guild(self, gid):
        return self.guild

    async def cached_member(self, guild, uid, max_age=300):
        return self.people.get(int(uid))


@pytest.fixture
def client(reg, rs, monkeypatch):
    monkeypatch.setenv(web.MCP_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("OIBOT_WEB_SECRET", SECRET)
    monkeypatch.delenv("OIBOT_WEB_DEV", raising=False)
    return TestClient(web.create_app(GuideBot(reg, rs)))


def as_user(client, uid):
    client.cookies.set(web.COOKIE, URLSafeSerializer(SECRET, salt="session").dumps({"uid": str(uid), "name": "x", "iat": time.time()}))
    return {"X-Requested-With": "oibot"}


def test_a_member_builds_reads_and_clears_their_own_guide_and_nobody_elses(client, reg):
    h = as_user(client, ME)
    st = client.get("/api/me/guide", headers=h).json()
    assert st["available"] and not st["built"] and [f["name"] for f in st["files"]] == ["explorer.html", "RXPGuides_LocalGuides.zip", "guide-text.zip"] and not st["starter_pack"]
    assert client.get("/api/me/guide/explorer", headers=h).status_code == 404
    r = client.post("/api/me/guide", json={"text": "", "battletag": TAG}, headers=h)
    assert r.status_code == 400 and "Paste the import string first" in r.json()["error"]
    r = client.post("/api/me/guide", json={"text": fixture_string(), "battletag": "Jon#4321"}, headers=h)
    assert r.status_code == 400 and "doesn't unlock" in r.json()["error"]

    r = client.post("/api/me/guide", json={"text": fixture_string(), "battletag": TAG}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["message"] == "2 guides unlocked · 7 steps" and d["built"] and d["guides"] == 2 and d["range"] == "1–12"
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d{1,2}:\d\d [AP]M", d["built_when"]), d["built_when"]
    assert d["groups"][0]["group"] == "Test Alliance" and "built_at" not in d, "words, never the machine stamp"
    assert g.member_dir(reg.key, ME).is_dir()

    ex = client.get("/api/me/guide/explorer", headers=h)
    assert ex.status_code == 200 and ex.headers["content-type"].startswith("text/html") and 'id="guides-data"' in ex.text
    assert ex.headers["content-security-policy"].startswith("sandbox allow-scripts") and ex.headers["cache-control"] == "no-store"
    f = client.get("/api/me/guide/file/RXPGuides_LocalGuides.zip", headers=h)
    assert f.status_code == 200 and f.headers["content-type"] == "application/zip" and "RXPGuides_LocalGuides.zip" in f.headers["content-disposition"]
    assert "RXPGuides_LocalGuides/RXPGuides_LocalGuides.toc" in zipfile.ZipFile(io.BytesIO(f.content)).namelist()
    assert client.get("/api/me/guide/file/guides.json", headers=h).status_code == 404, "only the three downloads, never the folder"
    assert client.get("/api/me/guide/file/..%2Fstatus.json", headers=h).status_code == 404

    # someone else: their own (empty) folder, never this one
    h2 = as_user(client, OTHER)
    assert not client.get("/api/me/guide", headers=h2).json()["built"]
    assert client.get("/api/me/guide/explorer", headers=h2).status_code == 404
    assert client.get("/api/me/guide/file/explorer.html", headers=h2).status_code == 404

    # a second build while one runs is refused, not queued
    g.lock_for(reg.key, ME).acquire()
    try:
        r = client.post("/api/me/guide", json={"text": fixture_string(), "battletag": TAG}, headers=as_user(client, ME))
        assert r.status_code == 409 and "being built already" in r.json()["error"]
    finally:
        g.lock_for(reg.key, ME).release()

    r = client.post("/api/me/guide/clear", json={}, headers=as_user(client, ME))
    assert r.status_code == 200 and "gone" in r.json()["message"] and not r.json()["built"]
    assert client.get("/api/me/guide/explorer", headers=as_user(client, ME)).status_code == 404
    assert "Nothing is built yet" in client.post("/api/me/guide/clear", json={}, headers=as_user(client, ME)).json()["message"]


def test_mcp_reads_the_status_but_never_builds(client):
    assert client.get("/api/me/guide", headers=bearer()).json()["available"]
    r = client.post("/api/me/guide", json={"text": fixture_string(), "battletag": TAG}, headers=bearer())
    assert r.status_code == 403 and "transcripts" in r.json()["error"]


def test_a_game_version_without_the_tables_has_no_tool(client, reg, monkeypatch):
    monkeypatch.setattr(reg.profile, "guides", {})
    h = as_user(client, ME)
    assert not client.get("/api/me/guide", headers=h).json()["available"]
    r = client.post("/api/me/guide", json={"text": fixture_string(), "battletag": TAG}, headers=h)
    assert r.status_code == 400 and "no leveling-guide tool" in r.json()["error"]
    assert client.get("/api/meta", headers=h).json()["guides"] is False
    monkeypatch.undo()
    monkeypatch.setattr(build_mod, "OUT", build_mod.OUT)  # the autouse fixture's patch was undone with the rest; keep writes out of the real folder
    assert client.get("/api/meta", headers=h).json()["guides"] is True


def test_the_me_page_has_the_tab_and_the_component_renders_from_the_status_only():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    me = (src / "pages" / "Me.tsx").read_text()
    assert 'Tabs.Tab value="guide">Leveling guide' in me and "meta.guides" in me and "<GuideTools meta={meta} />" in me
    comp = (src / "components" / "GuideTools.tsx").read_text()
    assert 'sandbox="allow-scripts allow-downloads"' in comp and "/api/me/guide/explorer" in comp
    assert "battletag: tag.trim()" in comp and "setText(\"\")" in comp and "setTag(\"\")" in comp, "the string and the key are dropped after the build"
    assert "localStorage" not in comp and "guides.json" not in comp
