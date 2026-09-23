"""oibot-mcp: a stdio MCP server that lets a Claude Code session act as the guild owner through the RUNNING bot.

Thin by design (design.md §5.24): every tool is one HTTP call to the bot's JSON API (`OIBOT_MCP_URL`, default
http://127.0.0.1:8788) with `Authorization: Bearer <OIBOT_MCP_TOKEN>` (read from .env at start; never logged) and
the site's CSRF marker. So a write here is the same verb the site and the Discord commands run — sheets, cards,
DMs and ops lines follow — and the bot's request log shows `via=mcp`. If the bot is down every tool says so.

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
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # OIBOT_MCP_TOKEN (never printed), OIBOT_MCP_URL — at import so tools work when called directly too
DEFAULT_URL = "http://127.0.0.1:8788"
TOKEN_ENV, URL_ENV = "OIBOT_MCP_TOKEN", "OIBOT_MCP_URL"
HEADERS = {"X-Requested-With": "oibot", "Accept": "application/json"}
STATUSES = ("in", "sub", "out")
RAID_FIELDS = ("slots", "signup_lead_hours", "nudge_hours_before", "lock_hours_before", "confirm_hours_before", "fill_ask_hours", "lockout_days", "duration_hours",
               "notes", "split_policy", "nudge", "autofill", "open_dm", "first_open")


class Api:
    """The bot's JSON API as the owner. `transport` is for tests (httpx.MockTransport)."""

    def __init__(self, url: str | None = None, token: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.url = (url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        self.token = token if token is not None else (os.environ.get(TOKEN_ENV) or "")
        self._client = httpx.Client(base_url=self.url, headers=HEADERS, transport=transport, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ToolError(f"{TOKEN_ENV} is not set: add it to {ROOT / '.env'} (the same value the bot reads) and restart the MCP server")
        return {"Authorization": f"Bearer {self.token}"}

    def _send(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._client.request(method, path, headers=self._headers(), **kw)
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
            raise ToolError(str(err or data or f"HTTP {r.status_code}"))
        return data

    def get(self, path: str, **params) -> Any:
        return self._send("GET", path, params={k: v for k, v in params.items() if v is not None} or None)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._send("POST", path, json=body or {})


_api: Api | None = None


def api() -> Api:
    global _api
    if _api is None:
        _api = Api()
    return _api


def use(a: Api | None) -> None:
    """Swap the client (tests)."""
    global _api
    _api = a


mcp = FastMCP(
    "oibot_gm",
    instructions="Tools for the oibot_GM guild bot (WoW raiding guild). They act as the guild OWNER through the running bot: "
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


def run_summary(ev: dict) -> dict:
    return {"key": ev["key"], "raid": ev.get("raid"), "run": ev.get("run"), "when": ev.get("when"), "rel": ev.get("rel"), "state": ev.get("state"), "fill_state": ev.get("fill_state"),
            "size": ev.get("size"), "counts": ev.get("counts"), "rostered": ev.get("rostered"), "n_rosters": ev.get("n_rosters")}


# ---------------------------------------------------------------- reads

@mcp.tool()
def guild_overview() -> dict:
    """The guild at a glance: name, timezone, owner, officer roles, the channels the bot uses, each raid's cadence and
    live-run count, the live runs (key, when, state, counts), and the test bench (puppets, test runs). Start here."""
    cfg = api().get("/api/config")
    raids = api().get("/api/raids")
    rosters = api().get("/api/rosters")
    live = [run_summary(e) for r in rosters.get("raids", []) for e in r.get("open", []) + r.get("locked", [])] + [run_summary(e) for e in rosters.get("orphans", [])]
    return {
        "guild": {"timezone": cfg["settings"].get("timezone"), "owner_id": cfg["settings"].get("owner_id"), "about": cfg["settings"].get("about"), "ask_audience": cfg["settings"].get("ask_audience"),
                  "officer_roles": [r["name"] for r in cfg["settings"].get("officer_roles", [])], "officer_roles_pending": cfg["settings"].get("officer_roles_pending")},
        "channels": {k: v.get("name") for k, v in cfg.get("channels", {}).items()},
        "policy_docs": {k: {"compiled": v.get("compiled"), "summary": v.get("summary")} for k, v in cfg.get("docs", {}).items()},
        "raids": [{"id": r["id"], "name": r["name"], "size": r["size"], "slots": r["slots"], "lockout_days": r["lockout_days"], "signup_lead_hours": r["signup_lead_hours"],
                   "lock_hours_before": r["lock_hours_before"], "confirm_hours_before": r["confirm_hours_before"], "split_policy": r["split_policy"], "opened": r["opened"], "live": r["live"],
                   "schedules": [{"id": s["id"], "name": s["name"], "label": s["label"], "next": s.get("next", [])} for s in r.get("schedules", [])]} for r in raids.get("raids", [])],
        "live_runs": live,
        "test_bench": cfg.get("test_bench"),
    }


@mcp.tool()
def list_runs() -> dict:
    """Live sheets per raid (open / locked, with counts and the roster state) plus the upcoming slots the scheduler
    will open and the last closed runs. Use the `key` of a run with get_run and the write tools."""
    data = api().get("/api/rosters")
    return {"tz": data.get("tz"), "raids": [{"id": r["id"], "name": r["name"], "size": r["size"], "opened": r["opened"], "first_open": r.get("first_open"),
                                            "open": [run_summary(e) for e in r.get("open", [])], "locked": [run_summary(e) for e in r.get("locked", [])],
                                            "upcoming": r.get("upcoming", []), "past": [run_summary(e) for e in r.get("past", [])]} for r in data.get("raids", [])],
            "orphans": [run_summary(e) for e in data.get("orphans", [])]}


@mcp.tool()
def get_run(run: str) -> dict:
    """One run in full: timeline (nudge / lock / confirm), counts, every signup with character/spec/role/status/pin,
    who has not answered, absences that day, double-booked members, what the roster still needs, the board (bank and
    rosters with their groups; after lock the actual rosters), confirmations, fill asks, callouts, split settings and
    the run's log. `run` = run key, roster key, or the raid's name when it has exactly one live run."""
    return api().get(f"/api/run/{run}")


@mcp.tool()
def list_raids() -> dict:
    """Every raid of the game profile with the guild's effective cadence: size, slots, lockout, signup lead, nudge/lock/
    confirm hours, fill-ask hours, weights, split policy, comp bounds, which fields the guild overrides, live-run count,
    and its schedules (id, name, kind, a plain-words label, own cadence overrides vs the effective values, the next runs)."""
    return api().get("/api/raids")


@mcp.tool()
def get_raid(raid: str) -> dict:
    """One raid's effective settings (see list_raids). `raid` = the raid id (e.g. barrow_deeps) or its name."""
    data = api().get("/api/raids")
    n = raid.strip().lower()
    hits = [r for r in data.get("raids", []) if r["id"].lower() == n or r["name"].lower() == n] or [r for r in data.get("raids", []) if n in r["id"].lower() or n in r["name"].lower()]
    if len(hits) != 1:
        raise ToolError(f"unknown raid {raid}; options: " + ", ".join(f"{r['id']} ({r['name']})" for r in data.get("raids", [])) if not hits else f"{raid}: which one? " + ", ".join(r["id"] for r in hits))
    return {**hits[0], "weight_keys": data.get("weight_keys"), "split_policies": data.get("split_policies")}


@mcp.tool()
def list_members(filter: str | None = None) -> dict:
    """Every registered member with their characters (class/spec/offspec/role/rank/confirmed/rosters), upcoming absences,
    open placement asks, and privilege (owner/officer/member/outside/test). `filter` narrows by display name, character
    name, class, spec, role or rank (case-insensitive substring)."""
    data = api().get("/api/members")
    rows = data.get("rows", [])
    if filter:
        f = filter.strip().lower()
        rows = [r for r in rows if f in r["display_name"].lower() or any(f in " ".join(str(x) for x in (c.get("name"), c.get("cls"), c.get("spec"), c.get("role"), c.get("rank"))).lower() for c in r.get("characters", []))]
    return {"members": len(data.get("members", rows)) if isinstance(data.get("members"), int) else len(rows), "shown": len(rows), "rows": rows}


@mcp.tool()
def get_member(member: str) -> dict:
    """One member's record: characters, every absence on file, placement asks, live sheets they answered, DM setting.
    `member` = display name, character name or Discord id (ambiguous names come back with the candidates)."""
    return api().get("/api/member", name=member)


@mcp.tool()
def list_absences(include_past: bool = False) -> dict:
    """Upcoming absences (and current ones) across the guild, earliest first, with who recorded them."""
    return api().get("/api/absences", all="true" if include_past else None)


@mcp.tool()
def list_auras() -> dict:
    """The buff matrix as the guild has it: every aura (providers, scope, family, strength, status, note, overrides)
    and the stacking families with their beneficiary map. Owner edits go through aura_set / family_set / aura_reset."""
    return api().get("/api/auras")


@mcp.tool()
def ops_log(limit: int = 40) -> dict:
    """The bot's status (data-repo head, push, LLM usage, loot feed, uptime) and the last `limit` ops-feed lines
    (newest first), plus recent precedents and ledger rows."""
    data = api().get("/api/ops")
    n = max(1, min(int(limit), 500))
    return {"head": data.get("head"), "push": data.get("push"), "llm": data.get("llm"), "feed": data.get("feed"), "up_seconds": data.get("up"),
            "rows": data.get("rows", [])[:n], "precedents": data.get("precedents", [])[:10], "ledger": data.get("ledger", [])[:10]}


# ---------------------------------------------------------------- writes: runs

@mcp.tool()
def open_run(raid: str, when: str | None = None, schedule: str | None = None) -> str:
    """Open a signup sheet now for `raid` (id or name): its next scheduled run, or a one-off start `when` as
    'YYYY-MM-DD HH:MM' in guild time. `schedule` (an id from get_raid's schedules) picks that schedule's next run, or
    with `when` its settings — a pickup template always needs `when`. Posts the sheet in the signup channel (and DMs
    members if the raid says so)."""
    rid = get_raid(raid)["id"]
    return msg(api().post(f"/api/raid/{rid}/open", {"when": when or "", "schedule": schedule or ""}))


@mcp.tool()
def set_answer(run: str, member: str, status: str, character: str | None = None) -> str:
    """Answer a sheet for someone: status in (Join) / sub (Bench) / out (No thanks); `character` swaps which character
    they play. After lock, `in` seats them and asks them to confirm, `out` frees the seat and starts filling it."""
    if status not in STATUSES:
        raise ToolError(f"status must be one of {', '.join(STATUSES)} (in = Join, sub = Bench, out = No thanks)")
    return msg(api().post(f"/api/run/{run}/set", {"member": member, "status": status, "character": character}))


@mcp.tool()
def set_layout(run: str, groups: list[list[str]]) -> dict:
    """Set the board: `groups` = lists of display names per group (only members who answered Join count). Before lock it
    is the layout the lock will use; after lock it edits the roster (someone added from the bench is asked to confirm,
    someone removed is freed). Returns the board and the roster's needs."""
    data = api().post(f"/api/run/{run}/layout", {"groups": groups})
    return {"message": data.get("message", "layout saved"), "needs": data.get("needs"), "board": data.get("board")}


@mcp.tool()
def propose_split(run: str, strategy: str | None = None, avoid: list[list[list[str]]] | None = None) -> dict:
    """PREVIEW only (nothing saved): a fresh layout for an open run — one roster, or several under `strategy`
    (balanced | stacked | …; see list_raids split_policies). `avoid` = layouts already shown, to get a different one.
    Apply the layout you like with set_layout, or remember the strategy with set_strategy. When the tanks/healers
    allow fewer runs than the headcount, `why_not_more` says what another run is short, `leftovers` lists the joiners
    the roster left out and `another_run` says whether leftovers + bench + pool could make one (offspecs/alts)."""
    data = api().post(f"/api/run/{run}/split", {"strategy": strategy, "avoid": avoid})
    board = data.get("board") or {}
    return {"strategy": data.get("strategy"), "layout": data.get("layout"), "synergy": data.get("synergy"), "total": data.get("total"), "gap": data.get("gap"), "board": board,
            "why_not_more": data.get("reason_text"), "leftovers": board.get("leftovers"), "another_run": board.get("another")}


@mcp.tool()
def set_strategy(run: str, strategy: str) -> str:
    """Remember the split philosophy on a run (Auto-fill and the scheduled lock use it)."""
    return msg(api().post(f"/api/run/{run}/strategy", {"strategy": strategy}))


@mcp.tool()
def autofill(run: str) -> dict:
    """Let the solver shape the board of an open run (the roster's split strategy, pins, role bounds). Saves the layout;
    the lock will use it. Returns the board and what the roster still needs."""
    data = api().post(f"/api/run/{run}/autofill")
    return {"message": "board auto-filled", "needs": data.get("needs"), "board": data.get("board")}


@mcp.tool()
def pin(run: str, member: str, pin: str | None = None) -> str:
    """Pin someone for the lock: 'in' = must be rostered, 'out' = stays on the bench, None/'clear' = unpinned."""
    p = None if pin in (None, "", "clear", "none") else pin
    if p not in ("in", "out", None):
        raise ToolError("pin must be in, out or clear")
    return msg(api().post(f"/api/run/{run}/pin", {"member": member, "pin": p}))


@mcp.tool()
def lock_run(run: str) -> str:
    """Lock an open run now: the roster is built from the board (or solved), roster cards are posted and everyone seated
    gets a Confirm / Can't make it DM. What the scheduled lock does, early."""
    return msg(api().post(f"/api/run/{run}/lock"))


@mcp.tool()
def fill_seats(run: str, send: bool = False) -> dict:
    """After lock: what the fill engine would ask (shortfall, who is still being waited on, the next batch). With
    `send=true` the asks are actually sent (DMs + a line under the sheet)."""
    if not send:
        return api().post(f"/api/run/{run}/fill/preview")
    return {"message": msg(api().post(f"/api/run/{run}/fill"))}


@mcp.tool()
def cancel_run(run: str, reason: str | None = None) -> str:
    """Cancel a live run the way /raid cancel does: sheet closed, everyone on it told, open confirmations withdrawn."""
    return msg(api().post(f"/api/run/{run}/cancel", {"reason": reason or ""}))


@mcp.tool()
def confirm_for(run: str, member: str, yes: bool = True) -> str:
    """Answer a member's Confirm / Can't make it ask on their behalf (yes = confirmed, no = can't make it, which frees
    the seat). Same ripple as the member pressing the button; use it when they told you out of band."""
    return msg(api().post("/api/admin/placement", {"member": member, "run": run, "answer": "yes" if yes else "no"}))


# ---------------------------------------------------------------- writes: raids and auras (owner)

@mcp.tool()
def raid_set(raid: str, field: str, value: str) -> str:
    """Override one raid setting for the guild (owner): field = slots ('Tue 19:30, Thu 19:30'), signup_lead_hours,
    nudge_hours_before, lock_hours_before, confirm_hours_before, fill_ask_hours, lockout_days, duration_hours, notes,
    split_policy, nudge/autofill/open_dm (true|false), first_open ('YYYY-MM-DDTHH:MM' guild time),
    weight_<key> (see list_raids weight_keys), or tank_min/tank_max/healer_min/healer_max/dps_min/dps_max."""
    rid = get_raid(raid)["id"]
    f, v = field.strip(), value
    body: dict[str, Any] = {"instance": rid}
    if f.startswith("weight_"):
        body["weights"] = {f[7:]: v}
    elif f.split("_")[0] in ("tank", "healer", "dps") and f.split("_")[-1] in ("min", "max"):
        role, bound = f.split("_", 1)
        body["comp"] = {role: {bound: v}}
    elif f in ("nudge", "autofill", "open_dm"):
        body[f] = str(v).strip().lower() in ("1", "true", "yes", "on")
    elif f in RAID_FIELDS:
        body[f] = v
    else:
        raise ToolError(f"unknown raid field {field}; one of " + ", ".join(RAID_FIELDS) + ", weight_<key>, <role>_min/<role>_max")
    return msg(api().post("/api/admin/raid", body))


@mcp.tool()
def schedule_set(raid: str, schedule: str, field: str, value: Any) -> str:
    """Change one setting of a raid's schedule (owner) — or create the schedule with its first setting. Schedules are
    listed per raid by list_raids / get_raid (`schedules`: id, name, kind, plain-words label, next runs); `default` is
    the raid's own weekly slots. field = kind (weekly|lockout|pickup) | name | active (true|false) | rosters (1-4) |
    slots ('Sat 20:00, Sun 20:00') | days ('1, 3' = days of each lockout, 1 = reset day) | time ('20:00') |
    signup_lead_hours | nudge_hours_before | lock_hours_before | confirm_hours_before | fill_ask_hours | nudge |
    autofill | open_dm | split_policy. A cadence field set to 'inherit' goes back to the raid's value. A new schedule
    starts with its kind, slots, or days/time."""
    rid = get_raid(raid)["id"]
    return msg(api().post("/api/admin/raid/schedule", {"instance": rid, "id": schedule, "fields": {field.strip(): value}}))


@mcp.tool()
def schedule_remove(raid: str, schedule: str) -> str:
    """Remove a raid's schedule (owner). Removing `default` clears the raid's weekly slots; runs already open keep
    their times."""
    rid = get_raid(raid)["id"]
    return msg(api().post("/api/admin/raid/schedule/remove", {"instance": rid, "id": schedule}))


@mcp.tool()
def raid_reset(raid: str) -> str:
    """Drop every guild override on a raid (owner): back to the game profile's cadence."""
    rid = get_raid(raid)["id"]
    return msg(api().post("/api/admin/raid/reset", {"instance": rid}))


@mcp.tool()
def aura_set(buff: str, field: str, value: str) -> str:
    """What the guild learns about a buff (owner): field = scope (party|raid), family (a family id or another buff's id;
    'own' = stands alone), strength (number, 1 = full), status (confirmed|reported|assumed), note. `buff` = buff id
    from list_auras (e.g. fortitude, windfury_totem)."""
    if field not in ("scope", "family", "strength", "status", "note"):
        raise ToolError("field must be scope, family, strength, status or note")
    return msg(api().post("/api/admin/aura", {"id": buff, field: value}))


@mcp.tool()
def family_set(family: str, field: str, value: str) -> str:
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
        return msg(api().post("/api/admin/family", {"id": family, "value": vm}))
    return msg(api().post("/api/admin/family", {"id": family, field: value}))


@mcp.tool()
def aura_reset(id: str | None = None) -> str:
    """Back to the game defaults (owner): one buff or family id, or everything when `id` is empty."""
    return msg(api().post("/api/admin/aura/reset", {"id": id or None}))


# ---------------------------------------------------------------- writes: members

@mcp.tool()
def add_absence(member: str, start: str, end: str | None = None, reason: str | None = None) -> str:
    """Record that someone is away (dates YYYY-MM-DD, `end` defaults to `start`): the sheets in that window get their
    No thanks and the absences card updates — what the member's own 'I'll be away' does."""
    return msg(api().post("/api/members/absence", {"member": member, "start": start, "end": end, "reason": reason}))


@mcp.tool()
def clear_absence(member: str, start: str) -> str:
    """Someone is back early: the absence starting on `start` (YYYY-MM-DD) goes and the sheets it had answered for
    them re-open; the lines say which."""
    return msg(api().post("/api/members/absence/clear", {"member": member, "start": start}))


@mcp.tool()
def save_characters(member: str, rows: list[dict]) -> str:
    """Edit someone's character table the way the Members page saves it. Each row: {label, name?, surname?, cls, spec,
    offspec?, main: bool} for an existing character (label from get_member) or a new one (cls + spec, name optional
    = planned), or {label, delete: true}. Only the rows you pass change."""
    deletes = [str(r.get("label")) for r in rows if isinstance(r, dict) and r.get("delete")]
    chars = [r for r in rows if isinstance(r, dict) and not r.get("delete")]
    return msg(api().post("/api/members/save", {"rows": [{"member": member, "deletes": deletes, "characters": chars}]}))


@mcp.tool()
def set_member(member: str, character: str | None = None, rank: str | None = None, confirm: bool | None = None, main: str | None = None) -> str:
    """Officer edits of one member's record: `rank` (trial|raider|core|alt|social, on `character` or their main),
    `confirm=true` (verify the character), `main` (character name that becomes their main). Any subset."""
    body = {"member": member, "character": character, "rank": rank, "confirm": bool(confirm) if confirm else None, "main": main}
    return msg(api().post("/api/members/set", {k: v for k, v in body.items() if v is not None}))


@mcp.tool()
def set_dm(member: str, on: bool) -> str:
    """Switch a member's DMs from the bot on or off (off = they answer asks on the site only)."""
    return msg(api().post("/api/members/dm", {"member": member, "on": bool(on)}))


# ---------------------------------------------------------------- writes: config (owner) and the test bench

@mcp.tool()
def set_channel(kind: str, channel: str | None = None) -> str:
    """Point a bot channel at a server channel (owner): kind = ops | applications | signup | roster | registration |
    analytics | absences | news; `channel` = a channel name (#general), an id, or empty to unset. Cards are (re)posted."""
    return msg(api().post("/api/admin/config", {"field": f"channel:{kind}", "value": channel or None}))


@mcp.tool()
def set_config(field: str, value: Any) -> str:
    """One guild setting (owner): timezone (IANA name), ask_audience, about (text), news_keywords (a list of words or
    phrases that make a news item relevant), or officer_roles (a list of role ids
    — the whole list; role names are NOT resolved here, use plain_change for that)."""
    return msg(api().post("/api/admin/config", {"field": field, "value": value}))


@mcp.tool()
def test_bench(action: str, count: int = 20, raid: str | None = None, start_in: int = 40, lock_in: int = 25, confirm_in: int = 15, nudge_in: int = 32, dm_open: bool = False,
               run: str | None = None, join: int = 0, bench: int = 0, out: int = 0, member: str | None = None, status: str | None = None,
               add: list[dict] | None = None, remove: list[str] | None = None) -> str:
    """The test bench (/gm test): action = seed (`count` puppet members), run (`raid`, minute cadence start_in > nudge_in
    > lock_in > confirm_in), answer (`run` + a random mix join/bench/out, or one `member` with `status`), clear (cancel
    test runs, delete every puppet), compose (`add` = [{cls, spec, offspec?, count}] puppets of exactly that mix, `remove`
    = puppet ids from get_member/list_members), comp (`raid`: every puppet joined to a sandbox run two weeks out, not
    posted — then propose_split on its key builds the best comp). Puppets' DMs land in the tester's DMs; real members
    are never touched."""
    if action not in ("seed", "run", "answer", "clear", "compose", "comp"):
        raise ToolError("action must be seed, run, answer, clear, compose or comp")
    body: dict[str, Any] = {"action": action}
    if action == "seed":
        body["count"] = count
    elif action == "run":
        if not raid:
            raise ToolError("run needs a raid")
        body.update(raid=get_raid(raid)["id"], start_in=start_in, lock_in=lock_in, confirm_in=confirm_in, nudge_in=nudge_in, dm_open=dm_open)
    elif action == "answer":
        body.update(run=run, join=join, bench=bench, out=out, member=member, status=status)
    elif action == "compose":
        body.update(add=add or [], remove=remove or [])
    elif action == "comp":
        if not raid:
            raise ToolError("comp needs a raid")
        body["raid"] = get_raid(raid)["id"]
    return msg(api().post("/api/admin/test", body))


@mcp.tool()
def plain_change(text: str, apply: bool = False) -> dict:
    """Configure in plain words, the way /gm change does ("lock Barrow Deeps 3 hours before", "make Kessa core",
    "set the signup channel to #raids"). Claude parses it into whitelisted ops; `describe` shows current → new per op.
    Nothing is applied unless `apply=true` — call once to show the person, again with apply after they agree.
    Questions from the parser come back in `questions` instead of ops."""
    return api().post("/api/ops/change", {"text": text, "apply": bool(apply)})


@mcp.tool()
def plain_permissions() -> dict:
    """Who may act through plain text, and where: capability groups (id, label, who, the ops in it, an example
    sentence), every channel's mode (act | self = own record only | answer = questions only | ignore), DMs, the
    default for unlisted channels, and the guild's roles (for role:<id> in `who`)."""
    d = api().get("/api/plain-permissions")
    return {"groups": [{k: g[k] for k in ("id", "label", "who", "who_text", "ops", "example")} for g in d["groups"]],
            "channels": [{"id": c["id"], "name": c["name"], "mode": c["mode"]} for c in d["channels"]], "listed": d["listed"],
            "dm": d["dm"], "default": d["default"], "roles": d["guild_roles"]}


@mcp.tool()
def set_plain_permissions(groups: dict[str, list[str]] | None = None, channels: dict[str, str] | None = None, dm: str | None = None, default: str | None = None) -> str:
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
    return msg(api().post("/api/admin/plain-permissions", body))


# ---------------------------------------------------------------- news review (design §5.26)

@mcp.tool()
def list_news(since: str | None = None) -> dict:
    """News items the bot kept from the #news channel (webhook embed text only: title, description, url, when posted,
    the keywords that matched), oldest first. `since` = an ISO date-time: only items recorded after it. The linked
    articles are never fetched — judge from this text alone."""
    return api().get("/api/news", since=since)


@mcp.tool()
def get_profile(section: str | None = None) -> dict:
    """The effective game profile the guild runs on: section = buffs | families | raids | comp_rules (none = all).
    Guild overrides are applied and marked (`overridden` fields, `default` = the game file's value); `setting_op`
    says which config op changes a section as a guild setting, `files` where the defaults live."""
    return api().get("/api/agents/profile", section=section)


@mcp.tool()
def post_proposal(title: str, news: list[str], affects: str, kind: str, change: str, edit: dict, evidence: str, confidence: str = "medium") -> dict:
    """Propose one correction from the news (officers approve or dismiss it on a card in the ops channel).
    title: one line naming the contradiction. news: the urls of the items it rests on (from list_news).
    affects: what it touches ('raid barrow_deeps lockout_days', 'profiles/forever/buffs.yaml: blood_pact.family').
    kind: guild_setting (edit = a config op: {op: raid_set|aura_set|family_set|comp_target|…, target, field, value})
    | profile (edit = {file: 'profiles/<version>/<file>.yaml', key: 'entry.field', value}) | needs_developer (code must change).
    change: the change in plain words. evidence: the news sentence(s), quoted. confidence: low | medium | high."""
    return api().post("/api/proposals", {"title": title, "news": news, "affects": affects, "kind": kind, "change": change, "edit": edit or {}, "evidence": evidence, "confidence": confidence})


@mcp.tool()
def list_proposals(state: str | None = None) -> dict:
    """Proposals, newest first; `state` narrows (proposed, approved, dismissed, applying, applied, failed, reverted;
    comma-separated for several). Check it before proposing so the same thing is not proposed twice."""
    return api().get("/api/proposals", state=state)


@mcp.tool()
def get_proposal(id: str) -> dict:
    """One proposal in full: the news it cites, the typed edit, its state history."""
    return api().get(f"/api/proposals/{id}")


@mcp.tool()
def resolve_proposal(id: str, state: str, commit: str | None = None, note: str | None = None) -> str:
    """Move a proposal: approved | dismissed (an officer's decision), or the apply job's steps applying | applied |
    failed | reverted, with the commit hash and a short note. The Discord card updates in place."""
    return msg(api().post(f"/api/proposals/{id}/resolve", {"state": state, "commit": commit, "note": note}))


@mcp.tool()
def job_status() -> dict:
    """The local agent jobs (news review, apply): state, last run and outcome, next run, whether launchd has them
    loaded, and the recent runs."""
    return api().get("/api/agents")


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="oibot-mcp", description="oibot_GM MCP server (stdio): tools that drive the running bot's JSON API as the guild owner.")
    p.add_argument("--url", default=None, help=f"the bot's web bind (default ${URL_ENV} or {DEFAULT_URL})")
    p.add_argument("--list-tools", action="store_true", help="print the tool names and exit")
    args = p.parse_args(argv)
    load_dotenv(ROOT / ".env")  # OIBOT_MCP_TOKEN (never printed), OIBOT_MCP_URL
    if args.url:
        os.environ[URL_ENV] = args.url
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
