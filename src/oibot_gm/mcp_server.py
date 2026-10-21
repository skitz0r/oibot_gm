"""oibot-mcp: a stdio MCP server that lets a Claude Code session act as a guild's owner through the RUNNING bot.

Thin by design (design.md §5.24): every tool is one HTTP call to the bot's JSON API (`OIBOT_MCP_URL`, default
http://127.0.0.1:8788) with `Authorization: Bearer <OIBOT_MCP_TOKEN>` (read from .env at start; never logged) and
the site's CSRF marker. So a write here is the same verb the site and the Discord commands run — sheets, cards,
DMs and ops lines follow — and the bot's request log shows `via=mcp`. If the bot is down every tool says so.

Several guilds (design §5.32 "MCP"): every tool takes an optional `guild` (key or name). Omitted → `OIBOT_MCP_GUILD`
(from the environment or .env, non-overriding, so a job's pinned value wins), else the only guild; with several and no
default the bot answers with the list. The header always carries the KEY (a name is resolved here through
/api/guilds: names may be non-ASCII and headers are Latin-1). `list_guilds` lists them. Each call acts as the OWNER OF
THE CHOSEN GUILD. A job pins its session to one guild with `OIBOT_MCP_GUILD_LOCK=1` beside `OIBOT_MCP_GUILD` (the news
review and apply jobs set both in the child's environment): then a tool's `guild=` naming any other guild is refused, so
text the session reads (news posts are member text) can never steer it into another guild. The guild list is re-read
whenever a name misses and on every `list_guilds`, so a server claimed while this process runs can be named at once.

Names: members may be given as a display name, a character name or a Discord id; runs as a run key
(`bd-1209-1930-2026-12-09`), a roster key (`bd-1209-1930`) or the raid's name ("Barrow Deeps" = the one live run
for that raid). The server resolves them and answers with the candidates when a name is ambiguous.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # OIBOT_MCP_TOKEN (never printed), OIBOT_MCP_URL — at import so tools work when called directly too
DEFAULT_URL = "http://127.0.0.1:8788"
TOKEN_ENV, URL_ENV, GUILD_ENV = "OIBOT_MCP_TOKEN", "OIBOT_MCP_URL", "OIBOT_MCP_GUILD"
LOCK_ENV = "OIBOT_MCP_GUILD_LOCK"  # "1": every call acts on OIBOT_MCP_GUILD only (the jobs' pinned sessions)
ACTOR_ENV = "OIBOT_MCP_ACTOR"  # D8: the Discord id of the officer at the keyboard; sent ONLY by the loot / people decision tools
GUILD_HEADER = "X-Oibot-Guild"
ACTOR_HEADER = "X-Oibot-Actor"
# every tool's last parameter, documented once
Guild = Annotated[str | None, Field(description="guild: key or name; omit for OIBOT_MCP_GUILD or the only guild (list_guilds lists them)")]
HEADERS = {"X-Requested-With": "oibot", "Accept": "application/json"}
STATUSES = ("in", "sub", "out")
POLICY_TEXT_MAX = 1500  # guild_overview's excerpt of each policy document (policy_get has the whole text)


class Api:
    """The bot's JSON API as the owner of the chosen guild. `transport` is for tests (httpx.MockTransport). `guild`: the
    default guild (key or name) for every call — `OIBOT_MCP_GUILD` when not given; the one place that default lives
    (scripts/agents reuse this class). A call's own `guild=` wins over it — unless `lock` (`OIBOT_MCP_GUILD_LOCK=1`
    when not given): then a `guild=` resolving to any other guild is refused (a job's session acts on its guild only)."""

    def __init__(self, url: str | None = None, token: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 60.0,
                 guild: str | None = None, lock: bool | None = None):
        self.url = (url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else (os.environ.get(TOKEN_ENV) or "")
        self.guild = guild if guild is not None else (os.environ.get(GUILD_ENV) or "").strip() or None
        self.lock = lock if lock is not None else (os.environ.get(LOCK_ENV) or "").strip().lower() in ("1", "true", "yes", "on")
        self._client = httpx.Client(base_url=self.url, headers=HEADERS, transport=transport, timeout=timeout)
        self._guilds: list[dict] | None = None  # GET /api/guilds (name → key); re-read on a miss and by list_guilds

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ToolError(f"{TOKEN_ENV} is not set: add it to {ROOT / '.env'} (the same value the bot reads) and restart the MCP server")
        return {"Authorization": f"Bearer {self.token}"}

    def _send(self, method: str, path: str, guild: str | None = None, scoped: bool = True, actor: bool = False, **kw) -> Any:
        """`scoped=False`: no guild header at all (GET /api/guilds, which lists every guild for the bearer). `actor`: a
        decision about an award or a person (D8) — X-Oibot-Actor carries OIBOT_MCP_ACTOR when it is set; the bot
        validates it as an officer of the chosen guild and refuses the decision without it (the rule lives there)."""
        headers = self._headers()
        who = (os.environ.get(ACTOR_ENV) or "").strip() if actor else ""
        if who:
            headers[ACTOR_HEADER] = who
        key = self.key_for(guild or self.guild) if scoped else None
        if scoped and self.lock and guild and key != self.key_for(self.guild):
            pinned = self.guild or "the bot's only guild"
            raise ToolError(f"this session is pinned to {pinned}: guild={guild!r} is refused (it acts on its own guild only)")
        if key:
            headers[GUILD_HEADER] = key
        try:
            r = self._client.request(method, path, headers=headers, **kw)
        except httpx.ConnectError:
            raise ToolError(f"oibot is not running on {self.url}; start it with `uv run oibot discord` (or point {URL_ENV} at it)") from None
        except httpx.TimeoutException:
            raise ToolError(f"oibot on {self.url} did not answer in time ({method} {path})") from None
        except httpx.HTTPError as e:
            raise ToolError(f"{method} {path}: {e}") from e
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]} if r.status_code >= 400 else r.text
        if r.status_code == 401:
            raise ToolError(f"the bot rejected the token ({TOKEN_ENV} in .env must match the bot's)")
        if r.status_code == 400 and scoped and not key:
            self._guilds = None  # "Which guild?": the bot serves more guilds than this process last read
        if r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            e = ToolError(str(err or data or f"HTTP {r.status_code}"))
            e.status = r.status_code
            raise e
        return data

    # ---- guilds
    def guilds(self, fresh: bool = False) -> list[dict]:
        """Every guild the bot serves ({key, name, discord_guild_id, owner_discord_id, game_profile}); kept until a
        name misses (key_for re-reads once), the bot answers "Which guild?", or `fresh` (list_guilds). A bot from
        before several guilds (no /api/guilds: 404) serves exactly one: one row with an empty key, which sends no
        header — the jobs run from this checkout and may meet the running bot before it restarts. That fallback is
        never kept: the next call asks again, so a bot restarted on the new code is seen at once."""
        if self._guilds is None or fresh:
            try:
                self._guilds = list(self._send("GET", "/api/guilds", scoped=False).get("guilds", []))
            except ToolError as e:
                if getattr(e, "status", None) != 404:
                    raise
                self._guilds = None
                return [{"key": "", "name": ""}]
        return self._guilds

    def key_for(self, ref: str | None) -> str | None:
        """A guild reference (key or name, case-insensitive) → its key; None/'' → None (the bot then uses the only
        guild, or answers with the list). Unknown or ambiguous → ToolError listing the guilds."""
        ref = (ref or "").strip()
        if not ref:
            return None
        for fresh in (False, True):  # a miss re-reads the list once: a server claimed since the last read
            rows = self.guilds(fresh=fresh)
            if any(g["key"] == ref for g in rows):
                return ref
            named = [g for g in rows if str(g.get("name", "")).casefold() == ref.casefold()] or [g for g in rows if g["key"].casefold() == ref.casefold()]
            if len(named) == 1:
                return named[0]["key"]
            if named:
                break  # ambiguous, not missing: re-reading won't help
        listing = ", ".join(f"{g['key']} ({g.get('name')})" for g in rows) or "none configured"
        raise ToolError(f"{'which guild' if named else 'no guild'} {ref!r}? Pass guild= one of: {listing}")

    def default_key(self) -> str | None:
        """The guild a call without `guild=` acts on: the resolved default, else the only guild, else None."""
        key = self.key_for(self.guild)
        if key:
            return key
        rows = self.guilds()
        return rows[0]["key"] if len(rows) == 1 else None

    # ---- verbs
    def get(self, path: str, guild: str | None = None, **params) -> Any:
        return self._send("GET", path, guild=guild, params={k: v for k, v in params.items() if v is not None} or None)

    def post(self, path: str, body: dict | None = None, guild: str | None = None, actor: bool = False) -> Any:
        return self._send("POST", path, guild=guild, actor=actor, json=body or {})

    def put(self, path: str, body: dict | None = None, guild: str | None = None) -> Any:
        return self._send("PUT", path, guild=guild, json=body or {})


class Scoped:
    """`api(guild)`: the same client, every call for that guild."""

    def __init__(self, base: Api, guild: str):
        self.base, self.guild = base, guild

    def get(self, path: str, **params) -> Any:
        return self.base.get(path, guild=self.guild, **params)

    def post(self, path: str, body: dict | None = None, actor: bool = False) -> Any:
        return self.base.post(path, body, guild=self.guild, actor=actor)

    def put(self, path: str, body: dict | None = None) -> Any:
        return self.base.put(path, body, guild=self.guild)


_api: Api | None = None


def api(guild: str | None = None) -> Api | Scoped:
    global _api
    if _api is None:
        _api = Api()
    return Scoped(_api, guild) if guild else _api


def use(a: Api | None) -> None:
    """Swap the client (tests)."""
    global _api
    _api = a


mcp = FastMCP(
    "oibot_gm",
    instructions="Tools for the oibot_GM guild bot (WoW raiding guild). They act as the OWNER OF THE CHOSEN GUILD through the running bot: "
                 "pass `guild` (key or name) when the bot serves several guilds — `list_guilds` lists them; omitted, the "
                 "default is OIBOT_MCP_GUILD or the only guild. "
                 "reads are free; every write has the same side effects as the officer doing it on the site or in Discord "
                 "(sheets re-rendered, members DMed, ops lines written). Look before you write (get_run / get_member), "
                 "prefer preview tools (propose_split, fill_seats without send, plain_change without apply) and quote "
                 "the returned message line back to the person.",
)


def msg(data: Any) -> str:
    """Write tools answer with the server's message line(s)."""
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False))
    return str(data)


def ops_change(guild: str | None, changes: list[dict], preview: bool = False) -> str | dict:
    """THE write path of the setup tools (design §5.43): typed changes ({kind, target, fields}) to POST /api/ops/change —
    the bot builds the ops (configops.ops_from_fields), so no tool carries the plain-text value grammar. `preview`:
    the Described preview (lines current → new, refused lines with who may and where, consequences, review), nothing
    saved — read it to the person. Otherwise applied at once (`confirmed`: the person asked for it here), answering the
    applied lines; a write that was refused raises with why (anything else in the call still applied, and says so)."""
    if preview:
        d = api(guild).post("/api/ops/change", {"changes": changes})
        out = d.get("described") or {}
        return {"preview": out.get("lines", []), "refused": d.get("refused", []), "consequences": out.get("consequences", []), "review": d.get("review", False),
                "describe": d.get("describe", [])}
    d = api(guild).post("/api/ops/change", {"changes": changes, "apply": True, "confirmed": True})
    applied, refused = d.get("applied") or [], d.get("refused") or []
    if refused and not applied:
        raise ToolError("; ".join(refused))
    if not applied and not refused:
        return d.get("summary") or "nothing to change"
    return "; ".join(applied) + (f" — refused: {'; '.join(refused)} (the rest was saved)" if refused else "")


def excerpt(text: str | None, n: int = POLICY_TEXT_MAX) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n].rstrip() + " … (truncated; policy_get has the whole text)"


def run_summary(ev: dict) -> dict:
    return {"key": ev["key"], "raid": ev.get("raid"), "run": ev.get("run"), "when": ev.get("when"), "rel": ev.get("rel"), "state": ev.get("state"), "fill_state": ev.get("fill_state"),
            "size": ev.get("size"), "counts": ev.get("counts"), "rostered": ev.get("rostered"), "n_rosters": ev.get("n_rosters")}


# ---------------------------------------------------------------- reads

@mcp.tool()
def list_guilds() -> dict:
    """Every guild the bot serves: key, name, Discord server id, owner id, game profile, and `default` = the one a tool
    acts on when `guild` is omitted (OIBOT_MCP_GUILD, else the only guild; none when several and no default). Pass a
    key or a name as `guild` to any tool; each call acts as the owner of that guild."""
    base = api()
    rows = base.guilds(fresh=True)  # always the bot's current list (a server claimed while this process runs)
    default = base.default_key()
    return {"guilds": [{**g, "default": g["key"] == default} for g in rows], "default": default}


@mcp.tool()
def guild_overview(guild: Guild = None) -> dict:
    """The guild at a glance: name, timezone, owner, officer roles, member roles (holders register without applying),
    news keywords, the channels the bot uses ({id, name, missing}: missing = set but no longer a channel the bot can
    see), the policy documents (compiled or not, summary, the text up to 1500 characters), each raid's cadence and
    live-run count, the live runs (key, when, state, counts), and the test bench (puppets, test runs). `guild` says
    which guild answered (key, name, Discord id). Start here."""
    cfg = api(guild).get("/api/config")
    raids = api(guild).get("/api/raids")
    rosters = api(guild).get("/api/rosters")
    live = [run_summary(e) for r in rosters.get("raids", []) for e in r.get("open", []) + r.get("locked", [])] + [run_summary(e) for e in rosters.get("orphans", [])]
    g = cfg.get("guild") or {}
    return {
        "guild": {"key": g.get("key"), "name": g.get("name"), "discord_guild_id": g.get("discord_guild_id"), "timezone": cfg["settings"].get("timezone"), "owner_id": cfg["settings"].get("owner_id"), "about": cfg["settings"].get("about"), "ask_audience": cfg["settings"].get("ask_audience"),
                  "officer_roles": [r["name"] for r in cfg["settings"].get("officer_roles", [])], "officer_roles_pending": cfg["settings"].get("officer_roles_pending"),
                  "member_roles": [r["name"] for r in cfg["settings"].get("member_roles", [])], "news_keywords": cfg["settings"].get("news_keywords", [])},
        "channels": {k: {"id": v.get("id"), "name": v.get("name"), "missing": bool(v.get("missing"))} for k, v in cfg.get("channels", {}).items()},
        "policy_docs": {k: {"compiled": v.get("compiled"), "summary": v.get("summary"), "text": excerpt(v.get("text"))} for k, v in cfg.get("docs", {}).items()},
        "raids": [{"id": r["id"], "name": r["name"], "size": r["size"], "slots": r["slots"], "lockout_days": r["lockout_days"], "signup_lead_hours": r["signup_lead_hours"],
                   "lock_hours_before": r["lock_hours_before"], "confirm_hours_before": r["confirm_hours_before"], "split_policy": r["split_policy"], "opened": r["opened"], "live": r["live"],
                   "schedules": [{"id": s["id"], "name": s["name"], "label": s["label"], "class": s.get("class"), "regulars": s.get("regulars", 0), "next": s.get("next", [])} for s in r.get("schedules", [])]} for r in raids.get("raids", [])],
        "live_runs": live,
        "test_bench": cfg.get("test_bench"),
    }


@mcp.tool()
def list_runs(guild: Guild = None) -> dict:
    """Live sheets per raid (open / locked, with counts and the roster state) plus the upcoming slots the scheduler
    will open and the last closed runs. Use the `key` of a run with get_run and the write tools."""
    data = api(guild).get("/api/rosters")
    return {"tz": data.get("tz"), "raids": [{"id": r["id"], "name": r["name"], "size": r["size"], "opened": r["opened"], "first_open": r.get("first_open"),
                                            "open": [run_summary(e) for e in r.get("open", [])], "locked": [run_summary(e) for e in r.get("locked", [])],
                                            "upcoming": r.get("upcoming", []), "past": [run_summary(e) for e in r.get("past", [])]} for r in data.get("raids", [])],
            "orphans": [run_summary(e) for e in data.get("orphans", [])]}


@mcp.tool()
def get_run(run: str, guild: Guild = None) -> dict:
    """One run in full: timeline (nudge / lock / confirm), counts, every signup with character/spec/role/status/pin,
    `kind` (recurring | adhoc) and `series`, `not_answered` = who the bot expects and hasn't heard from (the series'
    regulars; `expected_from` says regulars | everyone (no regulars set) | test | none (ad hoc)), `regulars` = each regular
    with what they answered, absences that day, double-booked members, what the roster still needs, the board (bank and
    rosters with their groups; after lock the actual rosters), confirmations, fill asks, callouts (with the member's
    note), split settings, the run's log, `rev` (pass it to set_layout) and, for an open sheet, `health` (headcount
    against the seats, per role have/need/level with who could cover, party buffs nobody provides). `run` = run key,
    roster key, or the raid's name when it has exactly one live run."""
    return api(guild).get(f"/api/run/{run}")


@mcp.tool()
def list_raids(guild: Guild = None) -> dict:
    """Every raid of the game profile with the guild's effective cadence: size, slots, lockout, signup lead, nudge/lock/
    confirm hours, fill-ask hours, weights, split policy, comp bounds, which fields the guild overrides, live-run count,
    and its schedules (id, name, kind, class recurring|adhoc, series key, regulars count, a plain-words label, own cadence
    overrides vs the effective values with where each comes from (own | raid | adhoc defaults), seat weights, the next runs)."""
    return api(guild).get("/api/raids")


@mcp.tool()
def get_raid(raid: str, guild: Guild = None) -> dict:
    """One raid's effective settings (see list_raids). `raid` = the raid id (e.g. barrow_deeps) or its name."""
    data = api(guild).get("/api/raids")
    n = raid.strip().lower()
    hits = [r for r in data.get("raids", []) if r["id"].lower() == n or r["name"].lower() == n] or [r for r in data.get("raids", []) if n in r["id"].lower() or n in r["name"].lower()]
    if len(hits) != 1:
        raise ToolError(f"unknown raid {raid}; options: " + ", ".join(f"{r['id']} ({r['name']})" for r in data.get("raids", [])) if not hits else f"{raid}: which one? " + ", ".join(r["id"] for r in hits))
    return {**hits[0], "weight_keys": data.get("weight_keys"), "split_policies": data.get("split_policies")}


@mcp.tool()
def raid_readiness(raid: str | None = None, guild: Guild = None) -> dict:
    """Pool readiness (what the analytics channel's card shows, with the reasons): per raid the headcount against its
    size, each role's have/need/level with who could cover a shortfall (offspec, flex, alt), buffs nobody provides,
    the desired comp lines (want/min/max, have, level, why, the officer's note and whether it is derived or officer-set),
    what is short or over, and the pool plan. `raid` = a raid id for one raid; default every raid. Read-only."""
    return api(guild).get("/api/readiness", raid=raid)


@mcp.tool()
def list_members(filter: str | None = None, guild: Guild = None) -> dict:
    """Every registered member with their characters (class/spec/offspec/role/rank/confirmed/rosters), upcoming absences,
    open placement asks, and privilege (owner/officer/member/outside/test). `filter` narrows by display name, character
    name, class, spec, role or rank (case-insensitive substring)."""
    data = api(guild).get("/api/members")
    rows = data.get("rows", [])
    if filter:
        f = filter.strip().lower()
        rows = [r for r in rows if f in r["display_name"].lower() or any(f in " ".join(str(x) for x in (c.get("name"), c.get("cls"), c.get("spec"), c.get("role"), c.get("rank"))).lower() for c in r.get("characters", []))]
    return {"members": len(data.get("members", rows)) if isinstance(data.get("members"), int) else len(rows), "shown": len(rows), "rows": rows}


@mcp.tool()
def get_member(member: str, guild: Guild = None) -> dict:
    """One member's record: characters, every absence on file, placement asks, live sheets they answered, DM setting.
    `member` = display name, character name or Discord id (ambiguous names come back with the candidates)."""
    return api(guild).get("/api/member", name=member)


@mcp.tool()
def list_absences(include_past: bool = False, guild: Guild = None) -> dict:
    """Upcoming absences (and current ones) across the guild, earliest first, with who recorded them."""
    return api(guild).get("/api/absences", all="true" if include_past else None)


@mcp.tool()
def list_auras(guild: Guild = None) -> dict:
    """The buff matrix as the guild has it: every aura (providers, scope, family, strength, status, note, overrides)
    and the stacking families with their beneficiary map. Owner edits go through aura_set / family_set / aura_reset."""
    return api(guild).get("/api/auras")


@mcp.tool()
def ops_log(limit: int = 40, precedents_limit: int = 10, ledger_limit: int = 10, member: str | None = None, changes_limit: int = 20, guild: Guild = None) -> dict:
    """The bot's status (data-repo head, push, LLM usage, loot feed, uptime), the last `limit` ops-feed lines (newest
    first), the last `changes_limit` registry change lines (the analytics change log, since the bot started), and the
    loot precedents (council overrides of the bot's pick, up to 50) and award-log rows (up to 100), newest first — those
    two come from the Loot pages (list_precedents / loot_log have the whole views). `member` (name, character or id)
    narrows precedents and awards to that member's characters."""
    data = api(guild).get("/api/ops", changes_limit=max(0, min(int(changes_limit), 200)))
    pl, ll = max(0, min(int(precedents_limit), 50)), max(0, min(int(ledger_limit), 100))
    prec = api(guild).get("/api/loot/precedents", member=member or None, limit=pl) if pl else {"rows": []}
    log = api(guild).get("/api/loot/log", member=member or None, limit=ll, scores="true") if ll else {"rows": []}
    n = max(1, min(int(limit), 500))
    return {"head": data.get("head"), "push": data.get("push"), "llm": data.get("llm"), "feed": data.get("feed"), "up_seconds": data.get("up"),
            "rows": data.get("rows", [])[:n], "changes": data.get("changes", []), "precedents": prec.get("rows", [])[:pl], "ledger": log.get("rows", [])[:ll],
            "member": prec.get("member") if member else None}


# ---------------------------------------------------------------- writes: runs

@mcp.tool()
def open_run(raid: str, when: str | None = None, schedule: str | None = None, guild: Guild = None) -> str:
    """Open a signup sheet now for `raid` (id or name): its next scheduled run, or a start `when` as 'YYYY-MM-DD
    HH:MM' in guild time — one of its upcoming runs at that exact time opens as that run, any other time is a one-off
    AD HOC run (first come, no regulars, no reminder, no confirmations, locks 2 h before). `schedule` (an id from
    get_raid's schedules) picks that schedule's next run, or with `when` its settings — a pickup template (ad hoc)
    always needs `when`. Posts the sheet in the signup channel (and DMs members if the schedule says so)."""
    rid = get_raid(raid, guild)["id"]
    return msg(api(guild).post(f"/api/raid/{rid}/open", {"when": when or "", "schedule": schedule or ""}))


@mcp.tool()
def set_answer(run: str, member: str, status: str, character: str | None = None, note: str | None = None, callout: bool = True,
               guild: Guild = None) -> str:
    """Answer a sheet for someone: status in (Join) / sub (Bench) / out (No thanks); `character` swaps which character
    they play. After lock, `in` seats them and asks them to confirm, `out` frees the seat and starts filling it.
    `out` with `callout` true (the default) is "Can't make it": a callout like /raid out (hours before, late after the
    lock cutoff, the updates-thread line); `callout=False` is a plain No thanks — an answer change, no callout record
    (the same split as the site's menu and /raid set). `note` (a callout only) is its reason: officers see it (get_run
    callouts, the run's updates thread), the public sheet never does. The member gets a notice on their Me page (never
    a DM); their open DM card is edited."""
    if status not in STATUSES:
        raise ToolError(f"status must be one of {', '.join(STATUSES)} (in = Join, sub = Bench, out = No thanks)")
    if note and (status != "out" or not callout):
        raise ToolError("a note goes with a callout only (status out, callout true): it is the reason they can't make it")
    body: dict[str, Any] = {"member": member, "status": status, "character": character}
    if note:
        body["note"] = note
    if status == "out" and callout:
        body["callout"] = True  # /raid out's verb (RaidMixin.call_out), note or not
    return msg(api(guild).post(f"/api/run/{run}/set", body))


@mcp.tool()
def member_inbox(member: str, guild: Guild = None) -> dict:
    """A member's inbox as their Me page shows it: `asks` (every open Confirm / Can't, fill, swap or pre-lock ask, with
    where it waits — channel dm | web — and its deadline in words) and `notices` (outcomes they were told on the site
    instead of by DM: not rostered, seat released, answered for them, run cancelled, …). Answer an ask for them with
    confirm_for / fill_answer."""
    return api(guild).get("/api/admin/inbox", member=member)


@mcp.tool()
def set_layout(run: str, groups: list[list[str]], rev: str | None = None, guild: Guild = None) -> dict:
    """Set the board: `groups` = lists of display names per group (only members who answered Join count). Before lock it
    is the layout the lock will use; after lock it edits the roster (someone added from the bench is asked to confirm,
    someone removed is freed). Pass `rev` from get_run: when someone changed the board since, nothing is written and
    the error says so (read it again). Returns the board and the roster's needs."""
    body: dict[str, Any] = {"groups": groups}
    if rev:
        body["rev"] = rev
    data = api(guild).post(f"/api/run/{run}/layout", body)
    return {"message": data.get("message", "layout saved"), "needs": data.get("needs"), "board": data.get("board")}


@mcp.tool()
def propose_split(run: str, strategy: str | None = None, avoid: list[list[list[str]]] | None = None, guild: Guild = None) -> dict:
    """PREVIEW only (nothing saved): a fresh layout for an open run — one roster, or several under `strategy`
    (balanced | first | rotation; see list_raids split_policies). `avoid` = layouts already shown, to get a different one.
    Apply the layout you like with set_layout, or remember the strategy with set_strategy. When the tanks/healers
    allow fewer runs than the headcount, `why_not_more` says what another run is short, `leftovers` lists the joiners
    the roster left out and `another_run` says whether leftovers + bench + pool could make one (offspecs/alts)."""
    data = api(guild).post(f"/api/run/{run}/split", {"strategy": strategy, "avoid": avoid})
    board = data.get("board") or {}
    return {"strategy": data.get("strategy"), "layout": data.get("layout"), "synergy": data.get("synergy"), "total": data.get("total"), "gap": data.get("gap"), "board": board,
            "why_not_more": data.get("reason_text"), "leftovers": board.get("leftovers"), "another_run": board.get("another")}


@mcp.tool()
def set_strategy(run: str, strategy: str, guild: Guild = None) -> str:
    """Remember the split philosophy on a run (Auto-fill and the scheduled lock use it)."""
    return msg(api(guild).post(f"/api/run/{run}/strategy", {"strategy": strategy}))


@mcp.tool()
def autofill(run: str, guild: Guild = None) -> dict:
    """Let the solver shape the board of an open run (the roster's split strategy, pins, role bounds). Saves the layout;
    the lock will use it. Returns the board and what the roster still needs."""
    data = api(guild).post(f"/api/run/{run}/autofill")
    return {"message": "board auto-filled", "needs": data.get("needs"), "board": data.get("board")}


@mcp.tool()
def pin(run: str, member: str, pin: str | None = None, guild: Guild = None) -> str:
    """Pin someone for the lock: 'in' = must be rostered, 'out' = stays on the bench, None/'clear' = unpinned."""
    p = None if pin in (None, "", "clear", "none") else pin
    if p not in ("in", "out", None):
        raise ToolError("pin must be in, out or clear")
    return msg(api(guild).post(f"/api/run/{run}/pin", {"member": member, "pin": p}))


@mcp.tool()
def lock_run(run: str, guild: Guild = None) -> str:
    """Lock an open run now: the roster is built from the board (or solved), roster cards are posted and everyone seated
    gets a Confirm / Can't make it DM. What the scheduled lock does, early."""
    return msg(api(guild).post(f"/api/run/{run}/lock"))


@mcp.tool()
def fill_seats(run: str, send: bool = False, rev: str = "", picks: dict | None = None, guild: Guild = None) -> dict:
    """Who to ask to cover a run's shortfall. ALWAYS preview first (send=false, nothing sent), show the person the
    batch, then send with the returned `plan.rev`. The preview: before lock the planner's SUGGESTIONS for the
    tank/healer minimums, after lock the seats to fill — `plan.seats[i].options[0]` is the suggestion, the rest are
    alternates, each with a code-written `why`; `plan.lines` in words; `plan.batch_lines` = exactly the asks a send
    DMs now (one per line, role swaps marked SWAP, the tied backfill named; after lock at most 3 asks out at once);
    `batch` the same as data; `plan.still_short`; `plan.rev`. Quote the lines back — never invent a suggestion.
    `send=true` with `rev` = the preview's plan.rev sends exactly that batch (optional `picks` {seat index: option
    index} asks an alternate); a plan that changed since (board or answers) is refused and the current plan returned —
    show it and preview again. Every ask is a DM Yes/No; before lock a yes changes the member's signup and seats
    nobody, and unanswered asks close at lock. Without `rev` a send is refused before lock, and after lock refused
    ("preview first") whenever the batch holds a role swap — only a same-role batch goes out without one."""
    if not send:
        return api(guild).post(f"/api/run/{run}/fill/preview")
    payload: dict = {}
    if rev:
        payload["rev"] = rev
    if picks:
        payload["picks"] = {str(k): int(v) for k, v in picks.items()}
    return {"message": msg(api(guild).post(f"/api/run/{run}/fill", payload))}


@mcp.tool()
def cancel_run(run: str, reason: str | None = None, notify: bool = True, guild: Guild = None) -> str:
    """Cancel a live run the way /raid cancel does. Nobody is DMed: the sheet and officer cards show it cancelled, every
    open ask (confirmation, fill, pre-lock) closes and its DM card is edited to say Cancelled, everyone on the run gets a
    notice on their Me page, and with `notify` (default
    true) one fixed line goes to the signup channel — never the reason, which stays in the run's thread and the ops
    feed. `notify=false` posts no channel line. A done or cancelled run is refused."""
    return msg(api(guild).post(f"/api/run/{run}/cancel", {"reason": reason or "", "notify": bool(notify)}))


@mcp.tool()
def remind_unconfirmed(run: str, guild: Guild = None) -> str:
    """After lock: send the Confirm / Can't make it DM again to everyone rostered who hasn't answered (against their
    existing ask; members with DMs off wait on the site). At most once an hour per run — the refusal says when."""
    return msg(api(guild).post(f"/api/run/{run}/remind"))


@mcp.tool()
def pause_autofill(run: str, paused: bool = True, guild: Guild = None) -> str:
    """Pause (paused=true) or resume (paused=false) the fill engine for one run: paused, nothing goes out on its own
    (the scheduler, a freed seat, an answer); an officer's own fill send (fill_seats send=true) still goes."""
    return msg(api(guild).post(f"/api/run/{run}/pause", {"paused": bool(paused)}))


@mcp.tool()
def withdraw_ask(run: str, member: str, guild: Guild = None) -> str:
    """Close the fill or pre-lock ask already sent to `member` on this run: their DM card is edited to say never mind (no
    new DM), the tied half of a swap is released, and the seat counts as open again for the planner."""
    return msg(api(guild).post(f"/api/run/{run}/withdraw", {"member": member}))


@mcp.tool()
def reopen_run(run: str, guild: Guild = None) -> str:
    """Undo an END by mistake, within the hour: the run goes back to the state it ended from (a locked roster as it was).
    A cancelled run is never reopened — open a new one."""
    return msg(api(guild).post(f"/api/run/{run}/reopen"))


@mcp.tool()
def confirm_for(run: str, member: str, yes: bool = True, guild: Guild = None) -> str:
    """Answer a member's Confirm / Can't make it ask on their behalf (yes = confirmed, no = can't make it, which frees
    the seat). Same ripple as the member pressing the button; use it when they told you out of band."""
    return msg(api(guild).post("/api/admin/placement", {"member": member, "run": run, "answer": "yes" if yes else "no"}))


@mcp.tool()
def fill_answer(run: str, member: str, yes: bool = True, guild: Guild = None) -> str:
    """Answer a member's FILL ask (the seat or swap question the fill engine DMed them after lock) on their behalf:
    yes = they take it (seated the way they were asked), no = they can't (a tied partner's ask is withdrawn and the
    next person is asked). The fill DM buttons' own verb; for the Confirm / Can't make it ask a lock sends, use
    confirm_for. get_run's fill_asks lists who is being asked."""
    return msg(api(guild).post("/api/admin/fill-answer", {"run": run, "member": member, "yes": bool(yes)}))


@mcp.tool()
def repost_sheet(run: str, channel: str | None = None, guild: Guild = None) -> str:
    """Post a fresh copy of a live sheet (/raid sheet): in `channel` (a name like #raids, or an id), default the
    signup channel. The buttons and later updates follow the new message."""
    return msg(api(guild).post(f"/api/run/{run}/sheet", {"channel": channel or ""}))


# ---------------------------------------------------------------- writes: raids and auras (owner)

@mcp.tool()
def raid_set(raid: str, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Override raid settings for the guild (owner): one `field` + `value`, or several at once as `fields`
    {field: value} — ONE validated write, so settings that are only legal together (signup lead and nudge raised
    together) go through, and a refusal leaves the raid as it was. field = slots (a list ['Tue 19:30', 'Thu 19:30']),
    cadence (standard|short|same_week), signup_lead_hours, nudge_hours_before, lock_hours_before, confirm_hours_before,
    fill_ask_hours, lockout_days, duration_hours, notes, split_policy, nudge/autofill/open_dm/confirm (booleans; confirm =
    the lock asks the rostered to Confirm), first_open ('YYYY-MM-DDTHH:MM' guild time), weight_<key> (see list_raids
    weight_keys), or tank_min/tank_max/healer_min/healer_max/dps_min/dps_max. Values are typed (numbers, booleans,
    lists); the bot builds the change. `preview=True`: what would change (current → new, consequences), nothing saved."""
    rid = get_raid(raid, guild)["id"]
    want = dict(fields or {})
    if field:
        want[field] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return ops_change(guild, [{"kind": "raid", "target": rid, "fields": want}], preview)


@mcp.tool()
def schedule_set(raid: str, schedule: str, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, preview: bool = False,
                 guild: Guild = None) -> str | dict:
    """Change settings of a raid's schedule (owner) — or create the schedule. One `field` + `value`, or several at
    once as `fields` {field: value} in ONE validated write (a new schedule's kind and run times together). Schedules are
    listed per raid by list_raids / get_raid (`schedules`: id, name, kind, plain-words label, next runs); `default` is
    the raid's own weekly slots. field = kind (weekly|lockout|pickup) | name | active (bool) | rosters (1-4) | slots (a
    list ['Sat 20:00', 'Sun 20:00']) | days (a list [1, 3] = days of each lockout, 1 = reset day) | time ('20:00') |
    signup_lead_hours | nudge_hours_before | lock_hours_before | confirm_hours_before | fill_ask_hours | nudge | autofill |
    open_dm | confirm (bools) | split_policy | weights (the schedule's own seat weights {rank, main, sat_out,
    signup_order, regular}; a key set to null follows the raid). A cadence field set to null goes back to the raid's
    value. A new schedule starts with its kind, slots, or days/time. `preview=True`: what would change, nothing saved."""
    rid = get_raid(raid, guild)["id"]
    want = {str(k).strip(): v for k, v in (fields or {}).items()}
    if field:
        want[field.strip()] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return ops_change(guild, [{"kind": "schedule", "target": f"{rid}:{schedule}", "fields": want}], preview)


@mcp.tool()
def schedule_remove(raid: str, schedule: str, preview: bool = False, guild: Guild = None) -> str | dict:
    """Remove a raid's schedule (owner). Removing `default` clears the raid's weekly slots; runs already open keep
    their times; its regulars come off it (they see a notice, nobody is DMed). `preview=True` names them first."""
    rid = get_raid(raid, guild)["id"]
    return ops_change(guild, [{"kind": "schedule_remove", "target": f"{rid}:{schedule}"}], preview)


@mcp.tool()
def raid_reset(raid: str, preview: bool = False, guild: Guild = None) -> str | dict:
    """Drop every guild override on a raid (owner): back to the game profile's cadence. `preview=True` lists what goes."""
    rid = get_raid(raid, guild)["id"]
    return ops_change(guild, [{"kind": "raid_reset", "target": rid}], preview)


@mcp.tool()
def aura_set(buff: str, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """What the guild learns about a buff (owner): field = scope (party|raid), family (a family id or another buff's id;
    'own' = stands alone), strength (number, 1 = full), status (confirmed|reported|assumed), note — or several as
    `fields` {field: value}: ONE write, a refused field leaves the buff as it was. `family_new` = a NEW family's name
    (its id is made from the name) that the buff joins. `buff` = buff id from list_auras (e.g. fortitude,
    windfury_totem). `preview=True`: current → new, nothing saved."""
    want = dict(fields or {})
    if field:
        want[field] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return ops_change(guild, [{"kind": "buff", "target": buff, "fields": want}], preview)


@mcp.tool()
def family_set(family: str | None = None, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, preview: bool = False,
               guild: Guild = None) -> str | dict:
    """A stacking family (owner): field = name, status, note, value (the WHOLE beneficiary map as an object
    {"all": 3, "mana": 2, "spec:Holy": 1}) or value:<key> (ONE beneficiary: a number, or null to remove it — keys: all,
    physical, spell, mana, melee, ranged, healer, tank, spec:<Name>) — or several as `fields`, in ONE write. `family`
    empty + a `name` field = a NEW family (its id is made from the name, never typed). `preview=True`: current → new."""
    want = dict(fields or {})
    if field:
        want[field] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return ops_change(guild, [{"kind": "family", "target": family or None, "fields": want}], preview)


@mcp.tool()
def aura_reset(id: str | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Back to the game defaults (owner): one buff or family id, or everything when `id` is empty (`preview=True` says
    what every-aura reset drops)."""
    return ops_change(guild, [{"kind": "aura_reset", "target": id or None}], preview)


# ---------------------------------------------------------------- writes: members

@mcp.tool()
def add_absence(member: str, start: str, end: str | None = None, reason: str | None = None, preview: bool = False, guild: Guild = None) -> str:
    """Record that someone is away (dates YYYY-MM-DD, `end` defaults to `start`): the sheets in that window get their
    No thanks (a rostered seat after lock is freed and filled) and the absences card updates — what the member's own
    'I'll be away' does. `preview=true` writes nothing and says which live sheets it would touch and how: show the
    person that line first, then call again without preview."""
    body: dict[str, Any] = {"member": member, "start": start, "end": end, "reason": reason}
    if preview:
        body["preview"] = True
    return msg(api(guild).post("/api/members/absence", body))


@mcp.tool()
def clear_absence(member: str, start: str, guild: Guild = None) -> str:
    """Someone is back early: the absence starting on `start` (YYYY-MM-DD) goes and the sheets it had answered for
    them re-open; the lines say which."""
    return msg(api(guild).post("/api/members/absence/clear", {"member": member, "start": start}))


@mcp.tool()
def save_characters(member: str, rows: list[dict], guild: Guild = None) -> str:
    """Edit someone's character table the way the Members page saves it. Each row: {label, name?, surname?, cls, spec,
    offspec?, main: bool, flex?: [tank|healer|melee|ranged] (extra roles beyond the spec/offspec; [] clears)} for an
    existing character (label from get_member) or a new one (cls + spec, name optional = planned), or {label, delete: true}
    / {label, retire: true}. Only the rows you pass change. Same verb as the Members page: a seat a deleted or retired
    character holds is handed back and the bench asked; a respec rewrites the sheet answers."""
    deletes = [str(r.get("label")) for r in rows if isinstance(r, dict) and r.get("delete")]
    chars = [r for r in rows if isinstance(r, dict) and not r.get("delete")]
    decides = any(r.get("rank") or r.get("confirm") for r in chars)  # D8: a rank / a confirmation is made as OIBOT_MCP_ACTOR
    return msg(api(guild).post("/api/members/save", {"rows": [{"member": member, "deletes": deletes, "characters": chars}]}, actor=decides))


@mcp.tool()
def set_member(member: str, character: str | None = None, rank: str | None = None, confirm: bool | None = None, main: str | None = None, guild: Guild = None) -> str:
    """Officer edits of one member's record: `rank` (trial|raider|core|alt|social — the MEMBER's rank, stored on their main;
    naming an alt as `character` is refused), `confirm=true` (verify `character`, default the main), `main` (character
    name that becomes their main — the rank moves with it). Any subset. A rank or a confirm is a decision about a person
    (D8): it is sent as OIBOT_MCP_ACTOR (X-Oibot-Actor) and refused without it; `main` alone is the owner's token."""
    body = {"member": member, "character": character, "rank": rank, "confirm": bool(confirm) if confirm else None, "main": main}
    return msg(api(guild).post("/api/members/set", {k: v for k, v in body.items() if v is not None}, actor=bool(rank or confirm)))  # D8


@mcp.tool()
def set_dm(member: str, on: bool, guild: Guild = None) -> str:
    """Switch a member's DMs from the bot on or off (off = they answer asks on the site only)."""
    return msg(api(guild).post("/api/members/dm", {"member": member, "on": bool(on)}))


@mcp.tool()
def set_regular(raid: str, member: str, on: bool = True, schedule: str | None = None, character: str | None = None, guild: Guild = None) -> str:
    """Add (`on` true) or remove a REGULAR of a recurring schedule: `raid` id or name, `schedule` id from get_raid's
    schedules (default "default" = the raid's own run times; weekly or lockout only — pickup templates and one-offs are
    ad hoc and have no regulars), `member` (name, character or id), `character` = which of theirs raids there (default
    their main; one per member per schedule). Regulars are expected on every sheet of the schedule (get_run's
    not_answered lists the ones who haven't answered; the reminder goes to them) and weigh more at lock; nothing is
    answered for them. Same route as the Rosters page."""
    return msg(api(guild).post("/api/admin/raid/regulars", {"raid": raid, "schedule": schedule or "default", "member": member, "character": character or "", "on": bool(on)}))


# ---------------------------------------------------------------- writes: config (owner) and the test bench

@mcp.tool()
def set_channel(kind: str, channel: str | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Point a bot channel at a server channel (owner): kind = ops | applications | signup | roster | registration |
    analytics | absences | news | loot (the public award log); `channel` = a channel name (#general), an id, or empty to
    unset. Cards are (re)posted. `preview=True`: current → new and what it does, nothing saved."""
    return ops_change(guild, [{"kind": "settings", "fields": {f"channel:{kind}": channel or None}}], preview)


@mcp.tool()
def set_config(field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Guild settings (owner): timezone (IANA name), ask_audience, about (text), news_keywords (a list of words or
    phrases that make a news item relevant), loot_log (bool: the public award log), officer_roles (the WHOLE list of role
    ids or names the bot knows, always a list: ["123456789", "Officer"]; [] for none — a bare string is refused), channel:<kind> (as set_channel) — one `field` + `value` or several as `fields`.
    `preview=True`: current → new and the consequences, nothing saved."""
    want = dict(fields or {})
    if field:
        want[field] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return ops_change(guild, [{"kind": "settings", "fields": want}], preview)


@mcp.tool()
def test_bench(action: str, count: int = 20, raid: str | None = None, tempo: str | None = None, start_in: int | None = None, lock_in: int | None = None,
               confirm_in: int | None = None, nudge_in: int | None = None, dm_open: bool = False, run: str | None = None, join: int = 0, bench: int = 0,
               out: int = 0, yes: int = 0, no: int = 0, member: str | None = None, status: str | None = None, add: list[dict] | None = None,
               remove: list[str] | None = None, guild: Guild = None) -> str | dict:
    """The test bench (/gm test; the site's Test bench): action = seed (`count` puppet members from the built-in test
    roster — existing ones are kept, a count above the roster's size is capped to it, count=0 = the whole roster), run
    (`raid` + `tempo` = quick | standard | slow, or minutes to the start, or the four marks start_in > nudge_in > lock_in
    > confirm_in; refused without a signup channel — the sheet needs somewhere to go), answer (`run` + a random mix
    join/bench/out on the sheet, yes/no on the puppets' open Confirm / fill asks, or one `member` with `status` in | sub |
    out | yes | no), clear (cancel test runs, delete every puppet), compose (`add` = [{cls, spec, offspec?, count}]
    puppets of exactly that mix, `remove` = puppet ids from get_member/list_members), comp (`raid`: every puppet joined
    to a sandbox run two weeks out, not posted — then propose_split on its key builds the best comp), feed (the test
    feed: every card, puppet DM, step and answer of the test runs; `run` narrows it), state (puppets, live test runs,
    tempos, the clear consequence). Puppets' DMs reach the tester as one digest per batch; real members are never touched."""
    actions = ("seed", "run", "answer", "clear", "compose", "comp", "feed", "state")
    if action not in actions:
        raise ToolError("action must be " + ", ".join(actions[:-1]) + " or " + actions[-1])
    if action == "feed":
        return api(guild).get("/api/test/feed" + (f"?run={run}" if run else ""))
    if action == "state":
        return api(guild).get("/api/test")
    body: dict[str, Any] = {}
    if action == "seed":
        body["count"] = max(0, int(count))
    elif action == "run":
        if not raid:
            raise ToolError("run needs a raid")
        body.update(raid=get_raid(raid, guild)["id"], dm_open=dm_open)
        if tempo not in (None, ""):
            body["tempo"] = tempo
        body.update({k: v for k, v in (("start_in", start_in), ("lock_in", lock_in), ("confirm_in", confirm_in), ("nudge_in", nudge_in)) if v is not None})
    elif action == "answer":
        body.update(run=run, join=join, bench=bench, out=out, yes=yes, no=no, member=member, status=status)
    elif action == "compose":
        body.update(add=add or [], remove=remove or [])
    elif action == "comp":
        if not raid:
            raise ToolError("comp needs a raid")
        body["raid"] = get_raid(raid, guild)["id"]
    return msg(api(guild).post(f"/api/test/{action}", body))


@mcp.tool()
def plain_change(text: str = "", apply: bool = False, token: str = "", guild: Guild = None) -> dict:
    """Configure in plain words, the way /gm change does ("lock Barrow Deeps 3 hours before", "make Kessa core",
    "set the signup channel to #raids"). Two calls, like Discord's diff + Apply: first `text` alone — Claude parses it
    into whitelisted ops, `describe` shows current → new per op, and a change comes back with a `token` (single use,
    ten minutes). Read the describe lines to the person; after they agree call `apply=true` with that `token` (no
    text): exactly the ops they saw are applied — never a new parse — and a board someone changed since the preview
    is refused. The apply result lists `applied` (what landed), `refused` (each op that didn't, with why — the rest
    of the request still applies, as on Discord) and `error` (the first failure, or null): never retry what is in
    `applied`. Questions from the parser come back in `questions` instead of ops. Everything plain text does works
    here (board moves, proposals — "another" / "use it" remember what this token was shown —, recruitment, the
    plain-text policy, test tempos) except re-posting a sheet "here": use repost_sheet."""
    if apply:
        if not token:
            raise ToolError("apply needs the token from the preview: call plain_change(text) first, then apply=true with its token")
        return api(guild).post("/api/ops/change", {"token": token, "apply": True})
    return api(guild).post("/api/ops/change", {"text": text})


@mcp.tool()
def plain_permissions(guild: Guild = None) -> dict:
    """Who may act through plain text, and where: capability groups (id, label, who, the ops in it, an example
    sentence), every channel's mode (act | self = own record only | answer = questions only | ignore), DMs, the
    default for unlisted channels, and the guild's roles (for role:<id> in `who`)."""
    d = api(guild).get("/api/plain-permissions")
    return {"groups": [{k: g[k] for k in ("id", "label", "who", "who_text", "ops", "example")} for g in d["groups"]],
            "channels": [{"id": c["id"], "name": c["name"], "mode": c["mode"]} for c in d["channels"]], "listed": d["listed"],
            "dm": d["dm"], "default": d["default"], "roles": d["guild_roles"]}


@mcp.tool()
def set_plain_permissions(groups: dict[str, list[str]] | None = None, channels: dict[str, str] | None = None, set_channels: dict[str, str | None] | None = None,
                          dm: str | None = None, default: str | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Change the plain-text permissions (owner). `groups`: {group id: [who…]} for the groups to change — who is
    everyone | registered | officers | owner | role:<role id> (the owner can always do everything; [] = owner only).
    `channels`: the WHOLE per-channel map {channel id: act|self|answer|ignore} (unlisted channels use `default`; the ops
    and analytics channels act unless listed); `set_channels`: some channels only ({id: mode | null = back to the
    default}). `dm` / `default`: act | self | answer | ignore. Read plain_permissions first; `preview=True` first."""
    fields: dict[str, Any] = {}
    if groups is not None:
        fields["groups"] = groups
    if channels is not None:
        fields["channels"] = channels
    if set_channels is not None:
        fields["set_channels"] = set_channels
    if dm:
        fields["dm"] = dm
    if default:
        fields["default"] = default
    return ops_change(guild, [{"kind": "plain", "fields": fields}], preview)


# ---------------------------------------------------------------- recruitment (applications happen in Discord)

@mcp.tool()
def recruitment(guild: Guild = None) -> dict:
    """The recruitment settings: applications open or closed, the note shown on the Apply screen, the homepage's
    Discord invite, the member roles (people holding one register without applying) and the guild's application
    questions (label ≤45, hint ≤100, required), plus the server's roles (for set_recruitment's member_roles)."""
    d = api(guild).get("/api/recruitment")
    names = {r["id"]: r["name"] for r in d.get("guild_roles", [])}
    return {"open": d["open"], "note": d["note"], "invite_url": d["invite_url"], "questions": d["questions"],
            "member_roles": [{"id": r, "name": names.get(r, f"role:{r}")} for r in d["member_role_ids"]], "roles": d.get("guild_roles", [])}


@mcp.tool()
def set_recruitment(open: bool | None = None, note: str | None = None, questions: list[dict] | None = None, invite_url: str | None = None,
                    member_roles: list[str] | None = None, preview: bool = False, guild: Guild = None) -> str | dict:
    """Change the recruitment settings (owner); anything left out keeps its value, and nothing is saved if any part is
    refused (one write). `note=""` / `invite_url=""` clear them (an invite looks like https://discord.gg/abc123).
    `questions` is the WHOLE ordered list [{label, hint?, required?}] (≤15; label ≤45 characters, hint ≤100; keep an
    existing `id` to keep it). `member_roles`: role ids or names the bot knows (the whole list; [] = none). Read
    recruitment() first; `preview=True` shows current → new."""
    fields: dict[str, Any] = {}
    if open is not None:
        fields["open"] = bool(open)
    if note is not None:
        fields["note"] = note
    if invite_url is not None:
        fields["invite_url"] = invite_url
    if questions is not None:
        fields["questions"] = questions
    if member_roles is not None:
        fields["member_role_ids"] = [str(r).strip().lstrip("@").strip("<&>") for r in member_roles]
    return ops_change(guild, [{"kind": "recruitment", "fields": fields}], preview)


@mcp.tool()
def create_invite(channel: str, save: bool = False, guild: Guild = None) -> dict:
    """Create a permanent, unlimited-use Discord invite to `channel` (a name like #welcome, or an id) — the homepage's
    Join our Discord button (owner). Needs the bot's Create Invite permission there. Returned, not saved, unless
    `save=true`: then it becomes the recruitment invite link (the homepage and registration card follow)."""
    data = api(guild).post("/api/admin/invite", {"channel": channel})
    url = data.get("invite_url")
    out = {"invite_url": url, "message": data.get("message")}
    if save and url:
        out["message"] = msg(api(guild).post("/api/admin/recruitment", {"invite_url": url}))
        out["saved"] = True
    return out


@mcp.tool()
def list_applications(guild: Guild = None) -> dict:
    """Applications made in Discord: the open ones (oldest first) and the last 20 decided, each with the applicant's
    character (label, class/spec), the raid schedules they can make, their answers to the guild's questions, when
    they applied and the decision (status, by, note)."""
    return api(guild).get("/api/applications")


@mcp.tool()
def decide_application(member: str, accept: bool, note: str | None = None, guild: Guild = None) -> str:
    """Accept or decline an OPEN application (officer verdict, same as the Discord card's buttons: the applicant is
    DMed and, on accept, their character is added). `member`: the applicant's Discord name, character name or id.
    `note` goes to the applicant with the verdict. Look at list_applications first."""
    opens = api(guild).get("/api/applications").get("open", [])
    raw = str(member).strip().strip("<@!>")
    n = raw.lower()
    hit = [a for a in opens if a["uid"] == raw] or [a for a in opens if n in (a["display_name"].lower(), a["label"].lower())]
    if not hit:
        hit = [a for a in opens if n and (n in a["display_name"].lower() or n in a["label"].lower())]
    if not hit:
        raise ToolError(f"no open application for {member}" + (" (open: " + ", ".join(a["label"] for a in opens) + ")" if opens else ""))
    if len(hit) > 1:
        raise ToolError(f"{member}: which one? " + ", ".join(f"{a['label']} ({a['display_name']})" for a in hit))
    body: dict[str, Any] = {"uid": hit[0]["uid"], "accept": bool(accept)}
    if note:
        body["note"] = note
    return msg(api(guild).post("/api/admin/application/decide", body))


# ---------------------------------------------------------------- news review (design §5.26)

@mcp.tool()
def list_news(since: str | None = None, guild: Guild = None) -> dict:
    """News items the bot kept from the #news channel (webhook embed text only: title, description, url, when posted,
    the keywords that matched), oldest first. `since` = an ISO date-time: only items recorded after it. The linked
    articles are never fetched — judge from this text alone."""
    return api(guild).get("/api/news", since=since)


@mcp.tool()
def get_profile(section: str | None = None, guild: Guild = None) -> dict:
    """The effective game profile the guild runs on: section = buffs | families | raids | comp_rules (none = all).
    Guild overrides are applied and marked (`overridden` fields, `default` = the game file's value); `setting_op`
    says which config op changes a section as a guild setting, `files` where the defaults live."""
    return api(guild).get("/api/agents/profile", section=section)


@mcp.tool()
def post_proposal(title: str, news: list[str], affects: str, kind: str, change: str, edit: dict, evidence: str, confidence: str = "medium", guild: Guild = None) -> dict:
    """Propose one correction from the news (officers approve or dismiss it on a card in the ops channel).
    title: one line naming the contradiction. news: the urls of the items it rests on (from list_news).
    affects: what it touches ('raid barrow_deeps lockout_days', 'profiles/forever/buffs.yaml: blood_pact.family').
    kind: guild_setting (edit = a config op: {op: raid_set|aura_set|family_set|comp_target|…, target, field, value})
    | profile (edit = {file: 'profiles/<version>/<file>.yaml', key: 'entry.field', value}) | needs_developer (code must change).
    change: the change in plain words. evidence: the news sentence(s), quoted. confidence: low | medium | high."""
    return api(guild).post("/api/proposals", {"title": title, "news": news, "affects": affects, "kind": kind, "change": change, "edit": edit or {}, "evidence": evidence, "confidence": confidence})


@mcp.tool()
def list_proposals(state: str | None = None, guild: Guild = None) -> dict:
    """Proposals, newest first; `state` narrows (proposed, approved, dismissed, applying, applied, failed, reverted;
    comma-separated for several). Check it before proposing so the same thing is not proposed twice."""
    return api(guild).get("/api/proposals", state=state)


@mcp.tool()
def get_proposal(id: str, guild: Guild = None) -> dict:
    """One proposal in full: the news it cites, the typed edit, its state history."""
    return api(guild).get(f"/api/proposals/{id}")


@mcp.tool()
def resolve_proposal(id: str, state: str, commit: str | None = None, note: str | None = None, guild: Guild = None) -> str:
    """Move a proposal: approved | dismissed (an officer's decision), or the apply job's steps applying | applied |
    failed | reverted, with the commit hash and a short note. The Discord card updates in place."""
    return msg(api(guild).post(f"/api/proposals/{id}/resolve", {"state": state, "commit": commit, "note": note}))


@mcp.tool()
def job_status(guild: Guild = None) -> dict:
    """The local agent jobs (news review, apply): state, last run and outcome, next run, whether launchd has them
    loaded, and the recent runs."""
    return api(guild).get("/api/agents")


@mcp.tool()
def get_job_run(run_id: str, guild: Guild = None) -> dict:
    """One job run in full (ids from job_status `runs` or a job's `run_id`): whether it is still running, its outcome,
    and every step (tool calls, results, the runner's own lines) with 12-hour times."""
    return api(guild).get(f"/api/agents/run/{run_id}")


@mcp.tool()
def run_job(job: str = "review", guild: Guild = None) -> str:
    """Start a local job now (owner): review (the news review) or apply (apply approved proposals). launchd starts it;
    refused when it is already running or not installed. Check job_status / get_job_run afterwards."""
    return msg(api(guild).post("/api/agents/run", {"job": job}))


# ---------------------------------------------------------------- the loot council (design §3.1; D8 for decisions)
# Reads, previews, open, tick, distribute, discard and end act as the owner's token. A decision about an award or a
# person — confirm, award (override), reason, correct, retire a precedent, tiers, someone's wishlist, a wishlist
# import — is made by a NAMED officer: these tools send X-Oibot-Actor = OIBOT_MCP_ACTOR, the bot checks that id is an
# officer of the chosen guild and writes "<name> via MCP"; without it the bot refuses the decision and says so.

Session = Annotated[str | None, Field(description="a split run's council: its id ('<run>-r2'), label ('Roster 2') or roster number; omit when the run has one")]


def _loot_body(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


@mcp.tool()
def get_loot(run: str | None = None, guild: Guild = None) -> dict:
    """The loot council. Without `run`: every locked run with its council(s) (or none yet), the awards still waiting
    for a reason and the in-game conflicts (live and ended councils), the companions feeding this guild (live / quiet /
    disconnected), drops held while no run is live, and where the public award log goes. With `run`: that run's
    councils — drops per boss with their state, every pending pick with its candidate table (scores, tier, attendance,
    wishlist), the bot's justification, alternates and warnings, pending overrides (who, reason), awards, reasons owed,
    the roster (who may be named in an award), `batch_rev` / `drops_rev` (pass them back on writes) and
    `end_consequence` (what end_loot would say). Officers' view: scores and reasons included."""
    return api(guild).get(f"/api/loot/run/{run}") if run else api(guild).get("/api/loot")


@mcp.tool()
def open_loot(run: str, guild: Guild = None) -> str:
    """Open the loot council on a LOCKED run (what /raid loot does): a thread in the officer channel (roster channel,
    else ops channel — refused when neither is set), one council per roster of a split run. Idempotent: an open council
    says so. Discord unreachable → the council opens with its thread pending."""
    return msg(api(guild).post(f"/api/loot/{run}/open"))


@mcp.tool()
def loot_tick(run: str, items: list[str] | None = None, boss: str | None = None, copies: int = 1, remove: list[str] | None = None,
              drops_rev: str | None = None, session: Session = None, guild: Guild = None) -> dict:
    """Tick what dropped: `items` = item names or ids (an item the tables don't know is recorded BY NAME and scored as
    untiered), `boss` = the boss, `copies` per item; `remove` = drop ids (d3) to un-tick (a recorded drop is refused —
    use loot_correct). A merge (a companion tick never conflicts); `drops_rev` only when you want a stale refusal."""
    add = [{**({"item_id": int(x)} if str(x).strip().isdigit() else {"name": str(x).strip()}), "boss": boss, "copies": max(1, int(copies))} for x in (items or []) if str(x).strip()]
    data = api(guild).post(f"/api/loot/{run}/tick", _loot_body(add=add, remove=remove or [], drops_rev=drops_rev, session=session))
    return {"message": data.get("message"), "drops_rev": data.get("drops_rev")}


@mcp.tool()
def loot_distribute(run: str, only: list[str] | None = None, drops_rev: str | None = None, session: Session = None, guild: Guild = None) -> dict:
    """Judge the ticked drops nobody proposed yet (`only` = some drop ids): code scores every candidate, the model judges
    the top ones against the loot policy. Adds a batch of PROPOSALS — nothing is recorded until loot_confirm. One judge
    at a time per council (a second call says who is distributing). Returns the line and the new `batch_rev`."""
    data = api(guild).post(f"/api/loot/{run}/distribute", _loot_body(only=only, drops_rev=drops_rev, session=session))
    return {"message": data.get("message"), "batch_rev": data.get("batch_rev"), "stale": data.get("stale")}


@mcp.tool()
def loot_discard(run: str, only: list[str] | None = None, batch_rev: str | None = None, preview: bool = False, session: Session = None, guild: Guild = None) -> str:
    """Drop pending proposals: every one (pass `batch_rev` from get_loot: a batch someone changed since is refused) or
    `only` some drop ids ("decide later": the drop stays ticked). Pending overrides on them go too; nothing was
    recorded, so nothing is undone. `preview=true`: the sentence only, nothing written."""
    return msg(api(guild).post(f"/api/loot/{run}/discard", _loot_body(only=only, batch_rev=batch_rev, preview=preview or None, session=session)))


@mcp.tool()
def end_loot(run: str, preview: bool = False, guild: Guild = None) -> str:
    """End the run and its loot council(s) — what /raid end does: the summary (awards, overrides, anything left
    undecided) is archived and posted, the run is marked done. `preview=true` returns the sentence ("End …: 6 awards,
    1 override, 2 drops never distributed") without ending; show it to the person first."""
    data = api(guild).post(f"/api/loot/{run}/end", {"preview": True} if preview else {})
    return str(data.get("preview") or msg(data))


@mcp.tool()
def loot_confirm(run: str, only: list[str] | None = None, expect_to: str | None = None, batch_rev: str | None = None, preview: bool = False,
                 session: Session = None, guild: Guild = None) -> str:
    """RECORD picks (a decision: needs OIBOT_MCP_ACTOR = the officer's Discord id). One drop: `only=[drop id]` +
    `expect_to` = the recipient you were shown; every pick still pending: `batch_rev` from get_loot (a batch someone
    changed since is refused with what changed). Writes the ledger and, for overrides, the precedents.
    `preview=true`: the sentence only (no actor needed)."""
    return msg(api(guild).post(f"/api/loot/{run}/confirm", _loot_body(only=only, expect_to=expect_to, batch_rev=batch_rev, preview=preview or None, session=session), actor=not preview))


@mcp.tool()
def loot_award(run: str, drop: str, to: str, reason: str, replace: bool = False, batch_rev: str | None = None, session: Session = None, guild: Guild = None) -> str:
    """Override the bot's pick (a decision: needs OIBOT_MCP_ACTOR): give drop `drop` (its id, d3) to `to` (a rostered
    character, or "disenchant") with a REQUIRED `reason` (it becomes the precedent the bot cites next time). Pending
    until loot_confirm. Another officer's pending override needs `replace=true`; a recorded drop needs loot_correct."""
    return msg(api(guild).post(f"/api/loot/{run}/override", _loot_body(drop_id=drop, to=to, reason=reason, replace=replace or None, batch_rev=batch_rev, session=session), actor=True))


@mcp.tool()
def loot_reason(run: str, drop: str, reason: str, replace: bool = False, session: Session = None, guild: Guild = None) -> str:
    """The reason for an award the master looter gave someone other than the bot's pick (a decision: needs
    OIBOT_MCP_ACTOR): it becomes the precedent. Works after the council ended (`session` = its id). A second reason
    needs `replace=true`. get_loot's reasons_pending lists what is owed."""
    return msg(api(guild).post(f"/api/loot/{run}/reason", _loot_body(drop_id=drop, reason=reason, replace=replace or None, session=session), actor=True))


@mcp.tool()
def loot_correct(run: str, drop: str, to: str, reason: str, session: Session = None, guild: Guild = None) -> str:
    """Fix a recorded award — a mistake, not a decision about merit (needs OIBOT_MCP_ACTOR): the old row is voided with
    `reason`, a new award to `to` (a character or "disenchant") is recorded, no precedent. Works after the council ended."""
    return msg(api(guild).post(f"/api/loot/{run}/correct", _loot_body(drop_id=drop, to=to, reason=reason, session=session), actor=True))


@mcp.tool()
def loot_history(council: str | None = None, guild: Guild = None) -> dict:
    """Ended councils, newest first (run, date, awards, overrides, what was left undecided, the end summary); with
    `council` (an id or file name from the list) that council in full: drops, awards with who recorded them, overrides."""
    return api(guild).get(f"/api/loot/history/{council}") if council else api(guild).get("/api/loot/history")


@mcp.tool()
def loot_log(member: str | None = None, limit: int = 100, scores: bool = True, guild: Guild = None) -> dict:
    """The guild's award log, newest first: item, character, spec, date, boss, MS/OS; with `scores` (officers) also tier,
    weight, source, the bot's pick, who recorded it and the precedent's reason. `member` narrows to one member."""
    return api(guild).get("/api/loot/log", member=member or None, limit=max(1, min(int(limit), 1000)), scores="true" if scores else None)


@mcp.tool()
def list_precedents(status: str | None = None, member: str | None = None, guild: Guild = None) -> dict:
    """Council overrides with their reasons (status active | retired | superseded), newest first, each with its `ref`
    (for retire_precedent), plus `needs_reason` = awards recorded against the bot's pick that still have no reason."""
    return api(guild).get("/api/loot/precedents", status=status or None, member=member or None)


@mcp.tool()
def retire_precedent(ref: str, reason: str, restore: bool = False, guild: Guild = None) -> str:
    """Retire a precedent (the bot stops citing it) or `restore` it (a decision: needs OIBOT_MCP_ACTOR). `ref` from
    list_precedents; `reason` is required and stays on the precedent."""
    return msg(api(guild).post("/api/loot/precedents/retire", _loot_body(ref=ref, reason=reason, restore=restore or None), actor=True))


@mcp.tool()
def list_items(item: str | None = None, guild: Guild = None) -> dict:
    """Loot › Items: the UNTIERED items (learned from drops by name, or on the tables without tiers — with when first
    seen, times dropped and awards so far), the tiered ones and the spec rows; with `item` (a name, id or key) that
    item's tier grid — every spec's tier (the guild's over the profile's), whether it can equip the item, awards so far."""
    if item:
        return api(guild).get(f"/api/loot/items/{item}")
    return api(guild).get("/api/loot/items")


@mcp.tool()
def tier_item(item: str, tiers: dict[str, str], reweight: bool | None = None, guild: Guild = None) -> dict:
    """Set tiers for an item (a decision: needs OIBOT_MCP_ACTOR): `tiers` = {spec key: tier}, a spec key 'Priest:Holy',
    a whole class 'Priest:*' or a role 'role:healer', a tier S+ | S | A | B, '-' clears a tier the guild set. When the
    item already has awards and `reweight` is not given, NOTHING is written and the reply asks (`ask`): "re-weight the
    past awards?" — ask the person, then call again with reweight=true (recent-loot scores follow the new tier; who got
    what never changes) or false (they keep their weight)."""
    from urllib.parse import quote

    data = api(guild).post(f"/api/loot/items/{quote(item, safe='')}/tiers", _loot_body(tiers=tiers, reweight=reweight), actor=True)
    return {k: data.get(k) for k in ("message", "ask", "awards", "reweighted") if data.get(k) is not None}


@mcp.tool()
def get_wishlists(member: str | None = None, guild: Guild = None) -> dict:
    """Wishlists: every member's characters with a list and, per item, who wants it most (MS before OS, by rank); with
    `member` that member's lists with each character's `rev` (pass it to set_wishlist) and what they already received."""
    if member:
        return api(guild).get("/api/members/wishlist", member=member)
    return api(guild).get("/api/loot/wishlists")


@mcp.tool()
def set_wishlist(member: str, rows: list[dict], character: str | None = None, rev: str | None = None, guild: Guild = None) -> str:
    """Replace one character's WHOLE wishlist (an officer acting for someone: needs OIBOT_MCP_ACTOR). `rows` in priority
    order: {name or item_id, offspec?: bool, note?}; up to 10, an item once. `character` = which of theirs (default
    their only character or main); `rev` from get_wishlists(member): a list changed since is refused."""
    return msg(api(guild).post("/api/members/wishlist", _loot_body(member=member, character=character, rows=rows, rev=rev), actor=True))


@mcp.tool()
def import_wishlists(csv: str, preview: bool = True, guild: Guild = None) -> dict:
    """A ThatsMyBIS wishlist export (the CSV text) into the native wishlists (needs OIBOT_MCP_ACTOR to write). Per
    character in TMB's order; an item already on a list is a duplicate (the native entry stays); a character's earlier
    TMB rows are replaced; an entry made on the site is never deleted; past 10 the rest is skipped. `preview=true`
    (default) counts only — show the person, then call with preview=false."""
    data = api(guild).post("/api/loot/wishlists/import", {"csv": csv, "preview": bool(preview)}, actor=not preview)
    return {"message": data.get("message"), "report": data.get("report")}


# ---------------------------------------------------------------- policy documents (loot, comp, persona)

@mcp.tool()
def policy_get(doc: str, guild: Guild = None) -> dict:
    """A policy document in full: doc = loot | comp | persona. Its text, whether a compiled reading is live (and its
    summary), and whether an edit is pending confirmation."""
    return api(guild).get(f"/api/policy/{doc}")


@mcp.tool()
def policy_edit(doc: str, text: str, guild: Guild = None) -> dict:
    """Replace a policy document's text (officers; the WHOLE text — read policy_get first and edit it). loot/comp: saved
    as a draft, the live reading is unchanged until policy_compile + policy_confirm; persona: live as written."""
    return api(guild).put(f"/api/policy/{doc}", {"text": text})


@mcp.tool()
def policy_compile(doc: str, guild: Guild = None) -> dict:
    """The bot's reading of a policy document as it is now (an LLM call; nothing goes live): `reading` = the numbered
    loot rules or comp constraints in words, plus questions it could not settle. Show it to the person, then
    policy_confirm (confirm=true makes it live, false discards the pending edit and restores the previous text)."""
    return api(guild).post(f"/api/policy/{doc}/compile")


@mcp.tool()
def policy_confirm(doc: str, confirm: bool = True, guild: Guild = None) -> str:
    """After policy_compile: confirm=true makes that reading the live compiled policy; confirm=false discards the
    pending edit (the text before it comes back; the live reading never changed)."""
    return msg(api(guild).post(f"/api/policy/{doc}/{'confirm' if confirm else 'discard'}"))


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="oibot-mcp", description="oibot_GM MCP server (stdio): tools that drive the running bot's JSON API as the owner of the chosen guild.")
    p.add_argument("--url", default=None, help=f"the bot's web bind (default ${URL_ENV} or {DEFAULT_URL})")
    p.add_argument("--guild", default=None, help=f"the default guild (key or name) for every tool: sets ${GUILD_ENV} for this process")
    p.add_argument("--list-tools", action="store_true", help="print the tool names and exit")
    args = p.parse_args(argv)
    load_dotenv(ROOT / ".env")  # OIBOT_MCP_TOKEN (never printed), OIBOT_MCP_URL, OIBOT_MCP_GUILD — never overriding the environment
    if args.url:
        os.environ[URL_ENV] = args.url
    if args.guild:
        os.environ[GUILD_ENV] = args.guild
        use(None)
    if args.list_tools:
        import asyncio

        for t in asyncio.run(mcp.list_tools()):
            print(t.name)
        return
    if not os.environ.get(TOKEN_ENV):
        print(f"oibot-mcp: {TOKEN_ENV} is not set in {ROOT / '.env'}; tools will refuse until it is", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
