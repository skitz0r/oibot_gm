"""oibot-mcp: a stdio MCP server that lets a Claude Code session act as a guild's owner through the RUNNING bot.

Thin by design (design.md §5.24): every tool is one HTTP call to the bot's JSON API (`OIBOT_MCP_URL`, default
http://127.0.0.1:8788) with `Authorization: Bearer <OIBOT_MCP_TOKEN>` (read from .env at start; never logged) and
the site's CSRF marker. So a write here is the same verb the site and the Discord commands run — sheets, cards,
DMs and ops lines follow — and the bot's request log shows `via=mcp`. If the bot is down every tool says so.

Several guilds (design §5.32 "MCP"): every tool takes an optional `guild` (key or name). Omitted → `OIBOT_MCP_GUILD`
(from the environment or .env, non-overriding, so a job's pinned value wins), else the only guild; with several and no
default the bot answers with the list. The header always carries the KEY (a name is resolved here through
/api/guilds: names may be non-ASCII and headers are Latin-1). `list_guilds` lists them. Each call acts as the OWNER OF
THE CHOSEN GUILD.

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
GUILD_HEADER = "X-Oibot-Guild"
# every tool's last parameter, documented once
Guild = Annotated[str | None, Field(description="guild: key or name; omit for OIBOT_MCP_GUILD or the only guild (list_guilds lists them)")]
HEADERS = {"X-Requested-With": "oibot", "Accept": "application/json"}
STATUSES = ("in", "sub", "out")
POLICY_TEXT_MAX = 1500  # guild_overview's excerpt of each policy document (policy_get has the whole text)
RAID_FIELDS = ("slots", "signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours", "lockout_days", "duration_hours",
               "notes", "split_policy", "nudge", "autofill", "open_dm", "confirm", "first_open")


class Api:
    """The bot's JSON API as the owner of the chosen guild. `transport` is for tests (httpx.MockTransport). `guild`: the
    default guild (key or name) for every call — `OIBOT_MCP_GUILD` when not given; the one place that default lives
    (scripts/agents reuse this class). A call's own `guild=` wins over it."""

    def __init__(self, url: str | None = None, token: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 60.0,
                 guild: str | None = None):
        self.url = (url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else (os.environ.get(TOKEN_ENV) or "")
        self.guild = guild if guild is not None else (os.environ.get(GUILD_ENV) or "").strip() or None
        self._client = httpx.Client(base_url=self.url, headers=HEADERS, transport=transport, timeout=timeout)
        self._guilds: list[dict] | None = None  # GET /api/guilds, cached for the process (name → key)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ToolError(f"{TOKEN_ENV} is not set: add it to {ROOT / '.env'} (the same value the bot reads) and restart the MCP server")
        return {"Authorization": f"Bearer {self.token}"}

    def _send(self, method: str, path: str, guild: str | None = None, scoped: bool = True, **kw) -> Any:
        """`scoped=False`: no guild header at all (GET /api/guilds, which lists every guild for the bearer)."""
        headers = self._headers()
        key = self.key_for(guild or self.guild) if scoped else None
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
        if r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            e = ToolError(str(err or data or f"HTTP {r.status_code}"))
            e.status = r.status_code
            raise e
        return data

    # ---- guilds
    def guilds(self) -> list[dict]:
        """Every guild the bot serves ({key, name, discord_guild_id, owner_discord_id, game_profile}); fetched once.
        A bot from before several guilds (no /api/guilds: 404) serves exactly one: one row with an empty key, which
        sends no header — the jobs run from this checkout and may meet the running bot before it restarts."""
        if self._guilds is None:
            try:
                self._guilds = list(self._send("GET", "/api/guilds", scoped=False).get("guilds", []))
            except ToolError as e:
                if getattr(e, "status", None) != 404:
                    raise
                self._guilds = [{"key": "", "name": ""}]
        return self._guilds

    def key_for(self, ref: str | None) -> str | None:
        """A guild reference (key or name, case-insensitive) → its key; None/'' → None (the bot then uses the only
        guild, or answers with the list). Unknown or ambiguous → ToolError listing the guilds."""
        ref = (ref or "").strip()
        if not ref:
            return None
        rows = self.guilds()
        if any(g["key"] == ref for g in rows):
            return ref
        named = [g for g in rows if str(g.get("name", "")).casefold() == ref.casefold()] or [g for g in rows if g["key"].casefold() == ref.casefold()]
        if len(named) == 1:
            return named[0]["key"]
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

    def post(self, path: str, body: dict | None = None, guild: str | None = None) -> Any:
        return self._send("POST", path, guild=guild, json=body or {})

    def put(self, path: str, body: dict | None = None, guild: str | None = None) -> Any:
        return self._send("PUT", path, guild=guild, json=body or {})


class Scoped:
    """`api(guild)`: the same client, every call for that guild."""

    def __init__(self, base: Api, guild: str):
        self.base, self.guild = base, guild

    def get(self, path: str, **params) -> Any:
        return self.base.get(path, guild=self.guild, **params)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.base.post(path, body, guild=self.guild)

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
    rows = base.guilds()
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
    loot precedents (council overrides of the bot's pick, up to 50) and ledger rows (awards, up to 100), newest first.
    `member` (name, character or id) narrows precedents and ledger to that member's characters."""
    data = api(guild).get("/api/ops", precedents_limit=max(0, min(int(precedents_limit), 50)), ledger_limit=max(0, min(int(ledger_limit), 100)),
                     member=member or None, changes_limit=max(0, min(int(changes_limit), 200)))
    n = max(1, min(int(limit), 500))
    return {"head": data.get("head"), "push": data.get("push"), "llm": data.get("llm"), "feed": data.get("feed"), "up_seconds": data.get("up"),
            "rows": data.get("rows", [])[:n], "changes": data.get("changes", []), "precedents": data.get("precedents", []), "ledger": data.get("ledger", []),
            "member": data.get("member")}


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
def set_answer(run: str, member: str, status: str, character: str | None = None, note: str | None = None, guild: Guild = None) -> str:
    """Answer a sheet for someone: status in (Join) / sub (Bench) / out (No thanks); `character` swaps which character
    they play. After lock, `in` seats them and asks them to confirm, `out` frees the seat and starts filling it.
    `out` is always a callout, like /raid out (hours before, late after the lock cutoff, the updates-thread line);
    `note` (with out only) is its reason: officers see it (get_run callouts, the run's updates thread), the public
    sheet never does."""
    if status not in STATUSES:
        raise ToolError(f"status must be one of {', '.join(STATUSES)} (in = Join, sub = Bench, out = No thanks)")
    body: dict[str, Any] = {"member": member, "status": status, "character": character}
    if note:
        body["note"] = note
    if status == "out":
        body["callout"] = True  # /raid out's verb (RaidMixin.call_out), note or not
    return msg(api(guild).post(f"/api/run/{run}/set", body))


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
def cancel_run(run: str, reason: str | None = None, guild: Guild = None) -> str:
    """Cancel a live run the way /raid cancel does: the sheet and officer cards show it cancelled, open confirmations
    are withdrawn, a line goes to the run's thread and the ops feed. Rostered members are NOT told automatically —
    say so in the channel (the returned line reminds you)."""
    return msg(api(guild).post(f"/api/run/{run}/cancel", {"reason": reason or ""}))


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
def raid_set(raid: str, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, guild: Guild = None) -> str:
    """Override raid settings for the guild (owner): one `field` + `value`, or several at once as `fields`
    {field: value} — ONE validated write, so settings that are only legal together (signup lead and nudge raised
    together) go through, and a refusal leaves the raid as it was. field = slots ('Tue 19:30, Thu 19:30'),
    signup_lead_hours, nudge_hours_before, lock_hours_before, confirm_hours_before, fill_ask_hours, lockout_days,
    duration_hours, notes, split_policy, nudge/autofill/open_dm/confirm (true|false; confirm = the lock asks the
    rostered to Confirm), first_open ('YYYY-MM-DDTHH:MM' guild time), weight_<key> (see list_raids weight_keys), or
    tank_min/tank_max/healer_min/healer_max/dps_min/dps_max."""
    rid = get_raid(raid, guild)["id"]
    want = dict(fields or {})
    if field:
        want[field] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    body: dict[str, Any] = {"instance": rid}
    for raw, v in want.items():
        f = str(raw).strip()
        if f.startswith("weight_"):
            body.setdefault("weights", {})[f[7:]] = v
        elif f.split("_")[0] in ("tank", "healer", "dps") and f.split("_")[-1] in ("min", "max"):
            role, bound = f.split("_", 1)
            body.setdefault("comp", {}).setdefault(role, {})[bound] = v
        elif f in ("nudge", "autofill", "open_dm", "confirm"):
            body[f] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif f in RAID_FIELDS:
            body[f] = v
        else:
            raise ToolError(f"unknown raid field {raw}; one of " + ", ".join(RAID_FIELDS) + ", weight_<key>, <role>_min/<role>_max")
    return msg(api(guild).post("/api/admin/raid", body))


@mcp.tool()
def schedule_set(raid: str, schedule: str, field: str | None = None, value: Any = None, fields: dict[str, Any] | None = None, guild: Guild = None) -> str:
    """Change settings of a raid's schedule (owner) — or create the schedule. One `field` + `value`, or several at
    once as `fields` {field: value} in ONE validated write (a new schedule's kind and run times together). Schedules are
    listed per raid by list_raids / get_raid (`schedules`: id, name, kind, plain-words label, next runs); `default` is
    the raid's own weekly slots. field = kind (weekly|lockout|pickup) | name | active (true|false) | rosters (1-4) |
    slots ('Sat 20:00, Sun 20:00') | days ('1, 3' = days of each lockout, 1 = reset day) | time ('20:00') |
    signup_lead_hours | nudge_hours_before | lock_hours_before | confirm_hours_before | fill_ask_hours | nudge |
    autofill | open_dm | confirm (true|false: the lock asks the rostered to Confirm) | split_policy | weights (the
    schedule's own seat weights: {rank, main, sat_out, signup_order, regular} or 'regular 6, rank 0'; 'inherit' drops
    them). A cadence field set to 'inherit' goes back to the raid's value. A new schedule starts with its kind, slots,
    or days/time."""
    rid = get_raid(raid, guild)["id"]
    want = {str(k).strip(): v for k, v in (fields or {}).items()}
    if field:
        want[field.strip()] = value
    if not want:
        raise ToolError("give a field and value, or fields = {field: value, …}")
    return msg(api(guild).post("/api/admin/raid/schedule", {"instance": rid, "id": schedule, "fields": want}))


@mcp.tool()
def schedule_remove(raid: str, schedule: str, guild: Guild = None) -> str:
    """Remove a raid's schedule (owner). Removing `default` clears the raid's weekly slots; runs already open keep
    their times."""
    rid = get_raid(raid, guild)["id"]
    return msg(api(guild).post("/api/admin/raid/schedule/remove", {"instance": rid, "id": schedule}))


@mcp.tool()
def raid_reset(raid: str, guild: Guild = None) -> str:
    """Drop every guild override on a raid (owner): back to the game profile's cadence."""
    rid = get_raid(raid, guild)["id"]
    return msg(api(guild).post("/api/admin/raid/reset", {"instance": rid}))


@mcp.tool()
def aura_set(buff: str, field: str, value: str, guild: Guild = None) -> str:
    """What the guild learns about a buff (owner): field = scope (party|raid), family (a family id or another buff's id;
    'own' = stands alone), strength (number, 1 = full), status (confirmed|reported|assumed), note. `buff` = buff id
    from list_auras (e.g. fortitude, windfury_totem)."""
    if field not in ("scope", "family", "strength", "status", "note"):
        raise ToolError("field must be scope, family, strength, status or note")
    return msg(api(guild).post("/api/admin/aura", {"id": buff, field: value}))


@mcp.tool()
def family_set(family: str, field: str, value: str, guild: Guild = None) -> str:
    """A stacking family (owner): field = name, status, note, or value = the whole beneficiary map as 'all: 3, mana: 2'
    (keys: all, physical, spell, mana, melee, ranged, healer, tank, spec:<Name>). New family ids are created."""
    if field not in ("name", "status", "note", "value"):
        raise ToolError("field must be name, status, note or value")
    if field == "value":
        vm: dict[str, float] = {}
        for part in str(value).replace(";", ",").split(","):
            if not part.strip():
                continue
            k, _, x = part.partition(":")
            try:
                vm[k.strip()] = float(x)
            except ValueError:
                raise ToolError(f"value entries look like 'all: 3, mana: 2' (bad: {part.strip()})") from None
        return msg(api(guild).post("/api/admin/family", {"id": family, "value": vm}))
    return msg(api(guild).post("/api/admin/family", {"id": family, field: value}))


@mcp.tool()
def aura_reset(id: str | None = None, guild: Guild = None) -> str:
    """Back to the game defaults (owner): one buff or family id, or everything when `id` is empty."""
    return msg(api(guild).post("/api/admin/aura/reset", {"id": id or None}))


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
    existing character (label from get_member) or a new one (cls + spec, name optional = planned), or {label, delete: true}.
    Only the rows you pass change."""
    deletes = [str(r.get("label")) for r in rows if isinstance(r, dict) and r.get("delete")]
    chars = [r for r in rows if isinstance(r, dict) and not r.get("delete")]
    return msg(api(guild).post("/api/members/save", {"rows": [{"member": member, "deletes": deletes, "characters": chars}]}))


@mcp.tool()
def set_member(member: str, character: str | None = None, rank: str | None = None, confirm: bool | None = None, main: str | None = None, guild: Guild = None) -> str:
    """Officer edits of one member's record: `rank` (trial|raider|core|alt|social, on `character` or their main),
    `confirm=true` (verify the character), `main` (character name that becomes their main). Any subset."""
    body = {"member": member, "character": character, "rank": rank, "confirm": bool(confirm) if confirm else None, "main": main}
    return msg(api(guild).post("/api/members/set", {k: v for k, v in body.items() if v is not None}))


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
def set_channel(kind: str, channel: str | None = None, guild: Guild = None) -> str:
    """Point a bot channel at a server channel (owner): kind = ops | applications | signup | roster | registration |
    analytics | absences | news; `channel` = a channel name (#general), an id, or empty to unset. Cards are (re)posted."""
    return msg(api(guild).post("/api/admin/config", {"field": f"channel:{kind}", "value": channel or None}))


@mcp.tool()
def set_config(field: str, value: Any, guild: Guild = None) -> str:
    """One guild setting (owner): timezone (IANA name), ask_audience, about (text), news_keywords (a list of words or
    phrases that make a news item relevant), or officer_roles (a list of role ids
    — the whole list; role names are NOT resolved here, use plain_change for that)."""
    return msg(api(guild).post("/api/admin/config", {"field": field, "value": value}))


@mcp.tool()
def test_bench(action: str, count: int = 20, raid: str | None = None, start_in: int = 40, lock_in: int = 25, confirm_in: int = 15, nudge_in: int = 32, dm_open: bool = False,
               run: str | None = None, join: int = 0, bench: int = 0, out: int = 0, member: str | None = None, status: str | None = None,
               add: list[dict] | None = None, remove: list[str] | None = None, guild: Guild = None) -> str:
    """The test bench (/gm test): action = seed (`count` puppet members from the built-in test roster — existing ones
    are kept, a count above the roster's size is capped to it, count=0 = the whole roster), run (`raid`, minute cadence start_in > nudge_in
    > lock_in > confirm_in), answer (`run` + a random mix join/bench/out, or one `member` with `status`), clear (cancel
    test runs, delete every puppet), compose (`add` = [{cls, spec, offspec?, count}] puppets of exactly that mix, `remove`
    = puppet ids from get_member/list_members), comp (`raid`: every puppet joined to a sandbox run two weeks out, not
    posted — then propose_split on its key builds the best comp). Puppets' DMs land in the tester's DMs; real members
    are never touched."""
    if action not in ("seed", "run", "answer", "clear", "compose", "comp"):
        raise ToolError("action must be seed, run, answer, clear, compose or comp")
    body: dict[str, Any] = {"action": action}
    if action == "seed":
        body["count"] = max(0, int(count))
    elif action == "run":
        if not raid:
            raise ToolError("run needs a raid")
        body.update(raid=get_raid(raid, guild)["id"], start_in=start_in, lock_in=lock_in, confirm_in=confirm_in, nudge_in=nudge_in, dm_open=dm_open)
    elif action == "answer":
        body.update(run=run, join=join, bench=bench, out=out, member=member, status=status)
    elif action == "compose":
        body.update(add=add or [], remove=remove or [])
    elif action == "comp":
        if not raid:
            raise ToolError("comp needs a raid")
        body["raid"] = get_raid(raid, guild)["id"]
    return msg(api(guild).post("/api/admin/test", body))


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
def set_plain_permissions(groups: dict[str, list[str]] | None = None, channels: dict[str, str] | None = None, dm: str | None = None, default: str | None = None, guild: Guild = None) -> str:
    """Change the plain-text permissions (owner). `groups`: {group id: [who…]} for the groups to change — who is
    everyone | registered | officers | owner | role:<role id> (the owner can always do everything; [] = owner only).
    `channels`: the WHOLE per-channel map {channel id: act|self|answer|ignore} (unlisted channels use `default`; the ops
    and analytics channels act unless listed). `dm` / `default`: act | self | answer | ignore. Read plain_permissions first."""
    body: dict[str, Any] = {}
    if groups is not None:
        body["groups"] = groups
    if channels is not None:
        body["channels"] = channels
    if dm:
        body["dm"] = dm
    if default:
        body["default"] = default
    return msg(api(guild).post("/api/admin/plain-permissions", body))


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
                    member_roles: list[str] | None = None, guild: Guild = None) -> str:
    """Change the recruitment settings (owner); anything left out keeps its value, and nothing is saved if any part is
    refused. `note=""` / `invite_url=""` clear them (an invite looks like https://discord.gg/abc123). `questions` is the
    WHOLE ordered list [{label, hint?, required?}] (≤15; label ≤45 characters, hint ≤100; keep an existing `id` to keep
    it). `member_roles`: role names or ids (the whole list; [] = none). Read recruitment() first."""
    body: dict[str, Any] = {}
    if open is not None:
        body["open"] = bool(open)
    if note is not None:
        body["note"] = note
    if invite_url is not None:
        body["invite_url"] = invite_url
    if questions is not None:
        body["questions"] = questions
    if member_roles is not None:
        roles = api(guild).get("/api/recruitment").get("guild_roles", [])
        ids = []
        for ref in member_roles:
            raw = str(ref).strip().lstrip("@").strip("<&>")
            hit = [r for r in roles if r["id"] == raw or r["name"].lower() == raw.lower()]
            if len(hit) != 1:
                raise ToolError(f"no role named {ref}" if not hit else f"{ref}: several roles match")
            ids.append(hit[0]["id"])
        body["member_role_ids"] = ids
    return msg(api(guild).post("/api/admin/recruitment", body))


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
