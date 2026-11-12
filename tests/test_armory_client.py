"""The armory boundary (armory link, phase 2 — spec §5.3, §9): armory.Armory over recorded, anonymised profile
responses (tests/fixtures/armory/profile; invented guild "Lantern Wardens", invented characters), never the network."""
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from oibot_gm import armory, battlenet
from oibot_gm.armory import (NOT_MODIFIED, Armory, ArmoryDown, ArmoryError, ArmoryMissing, ArmoryShape, CharacterFacts, GuildFacts,
                             Reply, RosterRow, address, available, client_for, guild_slug)
from oibot_gm.profiles import GameProfile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "tests" / "fixtures" / "armory" / "profile"
FOREVER = GameProfile.load(ROOT / "profiles" / "forever")
TOKEN = "tok-SECRET-0123456789"
STAMP = "Thu, 12 Nov 2026 19:59:57 GMT"


class Fake:
    """The injected transport: path → (status, body, headers); every call is kept with its headers."""

    def __init__(self, routes: dict[str, tuple]):
        self.routes, self.calls = routes, []

    def __call__(self, url: str, headers: dict[str, str]) -> Reply:
        u = urllib.parse.urlsplit(url)
        self.calls.append((u.path, dict(urllib.parse.parse_qsl(u.query)), dict(headers)))
        route = self.routes.get(u.path)
        if route is None:
            raise AssertionError(f"not recorded: {u.path}")
        if isinstance(route, Exception):
            raise route
        status, body, hdrs = (*route, {})[:3]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return Reply(status, {k.lower(): v for k, v in hdrs.items()}, body or b"")


def fixture(name: str) -> dict:
    return json.loads((PROFILE / f"{name}.json").read_text(encoding="utf-8"))


def client(routes: dict, token: str | None = TOKEN) -> tuple[Armory, Fake]:
    fake = Fake(routes)
    return client_for_profile(fake, token), fake


def client_for_profile(fake, token=TOKEN) -> Armory:
    a = FOREVER.armory
    return Armory("us", "profile-forever-us", "static-forever-us", fetch=fake, token=(lambda: token) if token else None,
                  classes=a["classes"], rulesets=a["rulesets"], professions={p.blizzard_id: p.id for p in FOREVER.professions.values()},
                  limiter=battlenet.Limiter(1000))


G = "/data/wow/guild/lantern-wardens"
C = "/profile/wow/character/tamsin-pell"


# ------------------------------------------------------------------ names


def test_address_of_accented_name():
    assert address("Bram", "Kettle") == "bram-kettle"
    assert address("Ébba", "Thistle") == "%C3%A9bba-thistle"
    assert address("Pip", None) == "pip"
    assert address("Old Pip", "Wobble") == "old-pip-wobble"


def test_guild_slug_from_a_typed_name():
    assert guild_slug("Lantern Wardens") == "lantern-wardens"
    assert guild_slug("  The Keeper's   Lantern! ") == "the-keepers-lantern"
    assert guild_slug("Owl’s Rest") == "owls-rest"


# ------------------------------------------------------------------ reads


def test_guild_facts_from_the_fixture():
    a, fake = client({G: (200, fixture("guild"), {"Last-Modified": STAMP})})
    g = a.guild("lantern-wardens")
    assert g == GuildFacts(id=418362, slug="lantern-wardens", name="Lantern Wardens", faction="Horde", kind="Normal", members=8,
                           created=datetime.fromtimestamp(1793012345, tz=timezone.utc))
    path, query, headers = fake.calls[0]
    assert query == {"namespace": "profile-forever-us", "locale": "en_US"}
    assert headers == {"Authorization": f"Bearer {TOKEN}"}  # nothing else, nothing personal


def test_roster_rows_from_the_fixture():
    a, _ = client({f"{G}/roster": (200, fixture("roster"), {"Last-Modified": STAMP})})
    rows, modified = a.roster("lantern-wardens")
    assert modified == STAMP
    assert len(rows) == 8 and all(isinstance(r, RosterRow) for r in rows)
    by = {r.slug: r for r in rows}
    assert by["bram-kettle"] == RosterRow(id=700101, name="Bram", surname="Kettle", slug="bram-kettle", level=60, cls="Warrior")
    assert by["bram-ashvale"].cls == "Priest"  # same first name, another class
    assert by["pip-wobble"].level == 1
    assert by["ébba-thistle"].name == "Ébba" and by["ébba-thistle"].cls == "Mage"
    assert {r.cls for r in rows} == {"Warrior", "Priest", "Druid", "Mage", "Rogue", "Shaman", "Warlock", "Hunter"}
    assert not hasattr(rows[0], "rank")  # D7: the rank is never read


def test_roster_class_missing_from_the_table_reads_as_none():
    doc = fixture("roster")
    doc["members"][0]["character"]["playable_class"]["id"] = 6
    a, _ = client({f"{G}/roster": (200, doc)})
    rows, modified = a.roster("lantern-wardens")
    assert rows[0].cls is None and modified is None


def test_character_from_the_fixture():
    a, _ = client({C: (200, fixture("character"), {"Last-Modified": STAMP})})
    c = a.character("tamsin-pell")
    assert c == CharacterFacts(id=700103, name="Tamsin", surname="Pell", cls="Druid", level=60, guild_id=418362, modified=STAMP)


def test_character_without_a_guild():
    doc = fixture("character")
    del doc["guild"]
    a, _ = client({C: (200, doc)})
    assert a.character("tamsin-pell").guild_id is None


def test_if_modified_since_304_is_NOT_MODIFIED():
    a, fake = client({f"{G}/roster": (304, b"", {"Last-Modified": STAMP}), C: (304, b"", {"Last-Modified": STAMP})})
    rows, modified = a.roster("lantern-wardens", since=STAMP)
    assert rows is NOT_MODIFIED and modified == STAMP
    assert fake.calls[0][2]["If-Modified-Since"] == STAMP
    assert a.character("tamsin-pell", since=STAMP) is NOT_MODIFIED
    assert fake.calls[1][2]["If-Modified-Since"] == STAMP
    a.roster("lantern-wardens")
    assert "If-Modified-Since" not in fake.calls[2][2]


def test_professions_map_to_profile_ids_and_skip_unknown_professions():
    doc = fixture("professions")
    doc["secondaries"].append({"profession": {"name": "Archaeology", "id": 794},
                               "tiers": [{"skill_points": 5, "max_skill_points": 75, "tier": {"name": "Archaeology", "id": 1},
                                          "known_recipes": [{"name": "Dig", "id": 1}]}]})
    a, _ = client({f"{C}/professions": (200, doc, {"Last-Modified": STAMP})})
    got = a.professions("tamsin-pell")
    assert [p["id"] for p in got] == ["leatherworking", "enchanting", "cooking", "first_aid"]
    lw = got[0]
    assert lw["skill"] == 300
    assert lw["recipes"][0] == {"id": 55184, "name": "Forceful Medium Armor Kit"}
    assert {"id": 999001, "name": "Gleaming Spindle Wrap"} in lw["recipes"]  # the list is behind the game: kept, by id
    assert got[1]["skill"] == 157
    listed = [r["id"] for p in got for r in p["recipes"] if r["id"] in FOREVER.recipes]
    assert len(listed) == sum(len(p["recipes"]) for p in got) - 1  # every other recorded recipe is on the list


def test_professions_of_a_character_with_none():
    a, _ = client({f"{C}/professions": (200, {"character": {"id": 700103, "name": "Tamsin"}})})
    assert a.professions("tamsin-pell") == []


# ------------------------------------------------------------------ failures


def test_404_is_missing():
    a, _ = client({C: (404, (PROFILE / "missing.json").read_bytes()), G: (404, (PROFILE / "missing.json").read_bytes())})
    with pytest.raises(ArmoryMissing) as e:
        a.character("tamsin-pell")
    assert "level 10" in e.value.words
    with pytest.raises(ArmoryMissing) as e:
        a.guild("lantern-wardens")
    assert e.value.words == "The armory doesn't find a guild by that name."


def test_429_is_down_with_retry_after():
    a, fake = client({C: (429, b"", {"Retry-After": "7"})})
    with pytest.raises(ArmoryDown) as e:
        a.character("tamsin-pell")
    assert e.value.retry_after == 7.0
    assert e.value.words == "The armory can't be reached right now. Try again in a few minutes."
    assert len(fake.calls) == 1  # a 429 is not hammered


def test_5xx_and_network_are_down_after_one_more_try(monkeypatch):
    monkeypatch.setattr(armory.time, "sleep", lambda s: None)
    a, fake = client({C: (503, b"busy"), G: OSError("connection reset")})
    with pytest.raises(ArmoryDown) as e:
        a.character("tamsin-pell")
    assert e.value.retry_after is None and len(fake.calls) == 2
    with pytest.raises(ArmoryDown):
        a.guild("lantern-wardens")
    assert len(fake.calls) == 4


def test_a_401_takes_a_new_token_once():
    tokens = iter(["old", "new"])
    dropped = []

    class Tok:
        value = None

        def __call__(self):
            self.value = next(tokens)
            return self.value

    seen = []

    def fetch(url, headers):
        seen.append(headers["Authorization"])
        return Reply(401, {}, b"") if headers["Authorization"] == "Bearer old" else Reply(200, {}, json.dumps(fixture("guild")).encode())

    a = Armory("us", "profile-forever-us", "static-forever-us", fetch=fetch, token=Tok(), drop=dropped.append, limiter=battlenet.Limiter(1000))
    assert a.guild("lantern-wardens").id == 418362
    assert seen == ["Bearer old", "Bearer new"] and dropped == ["old"]


def test_a_changed_shape_is_ArmoryShape_not_a_KeyError():
    broken = [
        (G, {"id": "418362", "name": "x"}, lambda a: a.guild("lantern-wardens")),
        (f"{G}/roster", {"guild": {}, "members": [{"character": {"name": "Bram"}}]}, lambda a: a.roster("lantern-wardens")),
        (f"{G}/roster", {"guild": {}}, lambda a: a.roster("lantern-wardens")),
        (C, {"id": 1, "name": "Tamsin", "level": "sixty"}, lambda a: a.character("tamsin-pell")),
        (C, b"<html>not json</html>", lambda a: a.character("tamsin-pell")),
        (C, [1, 2], lambda a: a.character("tamsin-pell")),
        (f"{C}/professions", {"primaries": [{"profession": {"id": 165, "name": "Leatherworking"}, "tiers": [{"skill_points": None}]}]},
         lambda a: a.professions("tamsin-pell")),
        (f"{C}/professions", {"primaries": {"oops": 1}}, lambda a: a.professions("tamsin-pell")),
    ]
    for path, body, call in broken:
        a, _ = client({path: (200, body)})
        with pytest.raises(ArmoryShape) as e:
            call(a)
        assert isinstance(e.value, ArmoryError) and e.value.words
        assert not isinstance(e.value.__cause__, (KeyError, TypeError, ValueError))


def test_no_token_in_any_exception_text(monkeypatch):
    monkeypatch.setattr(armory.time, "sleep", lambda s: None)

    def boom(url, headers):
        raise OSError(f"proxy said no to {headers['Authorization']}")

    cases = [(429, b""), (404, b""), (500, b"Authorization: Bearer " + TOKEN.encode()), (200, b"{"), (200, b'{"id": "x"}'), (418, b"")]
    errors = []
    for status, body in cases:
        a, _ = client({C: (status, body)})
        try:
            a.character("tamsin-pell")
        except ArmoryError as e:
            errors.append(e)
    a = client_for_profile(boom)
    with pytest.raises(ArmoryDown) as e:
        a.character("tamsin-pell")
    errors.append(e.value)
    assert len(errors) == len(cases) + 1
    for e in errors:
        for text in (str(e), repr(e), e.words, str(e.__cause__ or ""), str(e.__context__ or "")):
            assert TOKEN not in text
        assert e.__cause__ is None and e.__context__ is None


def test_a_refused_token_is_access_not_down():
    def refuse():
        raise battlenet.TokenRefused("the token request was refused (401)")

    a = Armory("us", "profile-forever-us", "static-forever-us", fetch=Fake({}), token=refuse, limiter=battlenet.Limiter(1000))
    with pytest.raises(armory.ArmoryAccess) as e:
        a.guild("lantern-wardens")
    assert "Blizzard" in e.value.words


# ------------------------------------------------------------------ the limiter, the token, the gate


def test_limiter_holds_ten_a_second():
    now = [100.0]
    lim = battlenet.Limiter(10, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    times = []
    for _ in range(35):
        lim.acquire()
        times.append(now[0])
    assert times[:10] == [100.0] * 10  # a burst of ten goes at once
    assert all(times[i + 10] - times[i] >= 1.0 - 1e-9 for i in range(len(times) - 10))  # never eleven within a second
    assert times[-1] - times[0] < 3.5


def test_one_token_for_the_process():
    posts = []

    def post(cid, secret):
        posts.append(cid)
        return {"access_token": f"t{len(posts)}", "expires_in": 3600}

    now = [0.0]
    tok = battlenet.Token(post=post, clock=lambda: now[0], env={"BLIZZARD_CLIENT_ID": "id", "BLIZZARD_CLIENT_SECRET": "s"})
    assert tok.get() == "t1" and tok.get() == "t1" and posts == ["id"]
    tok.drop("t1")
    assert tok.get() == "t2"
    now[0] = 3600
    assert tok.get() == "t3"  # renewed before it expires
    with pytest.raises(battlenet.TokenRefused):
        battlenet.Token(post=post, env={}).get()


def test_the_recipe_importer_shares_the_token_and_the_limiter(monkeypatch):
    from oibot_gm.importers import blizzard

    monkeypatch.setattr(battlenet.TOKEN, "get", lambda: "shared")
    acquired = []
    monkeypatch.setattr(battlenet.LIMITER, "acquire", lambda: acquired.append(1))
    bz = blizzard.Blizzard(fetch=lambda url: {})
    assert bz.token() == "shared"
    blizzard._pace()
    assert acquired == [1] and blizzard.MAX_PER_SECOND == battlenet.MAX_PER_SECOND == 10


def test_available_and_client_for(monkeypatch):
    reg = SimpleNamespace(profile=FOREVER)
    tbc = SimpleNamespace(profile=GameProfile.load(ROOT / "profiles" / "tbc"))
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "secret")
    assert available(reg) and not available(tbc)
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET")
    assert not available(reg)
    a = client_for(reg)
    assert a is client_for(SimpleNamespace(profile=FOREVER))  # one per region
    assert (a.region, a.profile_ns, a.static_ns) == ("us", "profile-forever-us", "static-forever-us")
    assert a.classes[11] == "Druid" and a.rulesets[15] == "Roleplay PvP" and a.profession_ids[165] == "leatherworking"
    assert a._limiter is battlenet.LIMITER
    with pytest.raises(ArmoryError):
        client_for(tbc)
