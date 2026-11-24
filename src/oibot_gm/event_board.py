"""The Events page's board before the lock: every open raid of an event drawn as its lock would place people NOW
(phase 4, design §7a). The placement is the lock's own solve (rc.event_board_preview → rc._solve_run with the lock's
strategy and budget), so it is the roster lock_run then builds; this module only decides WHEN to solve and keeps the
answer.

- `fingerprint(reg, rs, parts)` = everything the solve reads, computed the way the solve computes it (players_for —
  answers, characters, ranks, attendance, the order people joined in —, seat_bonus — the weights, regulars, sat out —,
  the pins, the officers' board, the strategy, size, rosters, minimums, the comp policy, the run dict, the raid's config
  rev, the event's start and lineup) for each open raid, and each locked raid's roster for the raids after it. A change
  to any of them is a new fingerprint: a picture is never shown as current for inputs it wasn't solved from.
- `BoardCache` (one per bot: `cache_of(bot)`), keyed (guild key, event key = the first raid's run key), holding ONE
  entry per event: (fingerprint, previews, time). `get` answers at once: "ready" with the previews when the fingerprint
  matches; else it starts ONE background solve for the event (never two at once for an event, never twice for one
  fingerprint) and answers "working" with the previous picture (or None). The solves run on a dedicated
  PREVIEW_WORKERS-thread executor — never on the event loop, never in the request threads' pool — so six events opened
  in six tabs queue there one after another. An event nobody joined is answered inline (nothing to solve). A finished
  solve is kept; when the fingerprint still holds, BOARD_LISTENERS hear (guild key, event key) and the web publishes
  `run:<event key>` so the page reloads. A solve that went stale meanwhile is kept as the previous picture and solved
  again for the new fingerprint when a page asked for it meanwhile. An entry goes when its event has no open raid any
  more (looked at every `get` of the guild) and past MAX_BOARDS (least recently used).
- `shown_layout(reg, ev)` = the flat groups of the picture the server holds for that raid: what a drag on the page is
  applied to (rc.apply_move_shown, RaidMixin.board_move)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import raidcycle as rc
from .registry import Registry, RegistryError

log = logging.getLogger(__name__)

PREVIEW_WORKERS = 1  # board solves at once in the process: each is a lock-sized CP-SAT solve (8 search workers); one at a
#                     time keeps a lock solved meanwhile at full speed (its wall-clock safety net, solver.WALL_CEILING)
MAX_BOARDS = 32  # events whose picture is kept (least recently used goes first)
BOARD_LISTENERS: list = []  # fn(guild key, event key): a new current picture (the web publishes `run:<event key>`)


def snapshot(rs: "rc.RaidStore", first: "rc.RaidEvent") -> list["rc.RaidEvent"]:
    """The event's raids, deep-copied ON THE LOOP (where run writes happen), in order: what one solve and its
    fingerprint read, consistent with each other."""
    return [p.model_copy(deep=True) for p in rc.event_parts(rs, rc.first_part(rs, first))]


def needs_solve(parts: list["rc.RaidEvent"]) -> bool:
    return any(p.state == "open" and p.by_status("in") for p in parts)


def fingerprint(reg: Registry, rs: "rc.RaidStore", parts: list["rc.RaidEvent"]) -> str:
    """Everything the board's solves read (see the module doc), hashed. BLOCKING (players_for reads attendance): a
    thread. `parts` = snapshot()."""
    from .policy import PolicyStore

    rows: list = [PolicyStore(rs.store, rs.key).comp_constraints()]
    for p in parts:
        if p.state != "open":  # a locked raid's roster is what the raids after it prefer
            rows.append([p.key, p.state, [[list(g) for g in r.groups] for r in p.all_rosters],
                         sorted(str(rc.player_uid(p, x)) for x in p.seated())])
            continue
        players = rc.players_for(reg, p)
        size = rc.run_size(reg, p)
        strategy = rc.run_strategy(reg, p)
        try:
            raid_rev = reg.config_rev(f"raids:{p.instance}")
        except (RegistryError, KeyError):
            raid_rev = None
        rows.append([p.key, p.state, p.part, p.follows, rc.solve_inputs(p),
                     sorted((k, s.order_at, s.updated_at, s.source) for k, s in p.signups.items()),
                     [x.model_dump() for x in players], rc.seat_bonus(reg, rs, p, players), rc.run_weights(reg, p),
                     strategy, size, rc.run_rosters(reg, p), rc.run_kind(reg, p),
                     reg.minimums(p.instance, size), reg.role_bounds(p.instance, size),
                     sorted(rc.sat_out_last_window(reg, rs, p, first_only=True)) if strategy == "rotation" else None,
                     reg.config.team(p.team), raid_rev])
    rows.append(rc.event_rev(reg, rs, parts[0]) if parts else None)
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Entry:
    fp: str
    previews: dict
    at: float


class BoardCache:
    def __init__(self) -> None:
        self.entries: OrderedDict[tuple[str, str], Entry] = OrderedDict()
        self.running: dict[tuple[str, str], str] = {}  # event → the fingerprint being solved now
        self.asked: set[tuple[str, str]] = set()  # events a page asked for another fingerprint while one was solving
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.solves = 0  # solves started (tests, the Host's curiosity)
        self._pool: ThreadPoolExecutor | None = None

    def pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS, thread_name_prefix="board-preview")
        return self._pool

    def _keep(self, key: tuple[str, str], fp: str, previews: dict) -> None:
        self.entries[key] = Entry(fp, previews, time.time())
        self.entries.move_to_end(key)
        while len(self.entries) > MAX_BOARDS:
            self.entries.popitem(last=False)

    def prune(self, reg: Registry, rs: "rc.RaidStore") -> None:
        """Drop the guild's entries whose event has no open raid any more (locked, finished, cancelled or gone)."""
        for gk, fkey in [k for k in self.entries if k[0] == reg.key]:
            first = rs.events.get(fkey)
            if first is None or not any(p.state == "open" for p in rc.event_parts(rs, first)):
                self.entries.pop((gk, fkey), None)

    async def get(self, reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent") -> tuple[str, dict | None]:
        """The event's board pictures: ("ready", previews) for the current fingerprint; ("working", the previous
        previews | None) while a solve runs (one is started when none is); ("locked", None) when no raid is open."""
        first = rc.first_part(rs, ev)
        key = (reg.key, first.key)
        self.prune(reg, rs)
        parts = snapshot(rs, first)
        if not any(p.state == "open" for p in parts):
            return "locked", None
        fp = await asyncio.to_thread(fingerprint, reg, rs, parts)
        have = self.entries.get(key)
        if have is not None and have.fp == fp:
            self.entries.move_to_end(key)
            return "ready", have.previews
        if not needs_solve(parts):  # nobody joined: empty groups, no solve, nothing to wait for
            previews = await asyncio.to_thread(rc.event_board_preview, reg, rs, first, parts)  # board_rosters reads attendance
            self._keep(key, fp, previews)
            return "ready", previews
        self.start(reg, rs, first.key, fp, parts)
        return "working", (have.previews if have is not None else None)

    def start(self, reg: Registry, rs: "rc.RaidStore", fkey: str, fp: str, parts: list["rc.RaidEvent"]) -> None:
        """One background solve for the event; while one runs, a page asking for another fingerprint is noted and the
        running one solves again when it ends (never two at once, never twice for one fingerprint)."""
        key = (reg.key, fkey)
        if key in self.running:
            if self.running[key] != fp:
                self.asked.add(key)
            return
        self.running[key] = fp
        self.solves += 1
        self.tasks[key] = asyncio.get_running_loop().create_task(self._solve(reg, rs, fkey, fp, parts))

    async def _solve(self, reg: Registry, rs: "rc.RaidStore", fkey: str, fp: str, parts: list["rc.RaidEvent"]) -> None:
        key = (reg.key, fkey)
        previews = None
        try:
            previews = await asyncio.get_running_loop().run_in_executor(self.pool(), rc.event_board_preview, reg, rs, parts[0], parts)
        except Exception:  # noqa: BLE001 — a part's own failure is its sentence; this is the unexpected kind
            log.exception("board preview failed (%s %s)", reg.key, fkey)
        finally:
            self.running.pop(key, None)
            self.tasks.pop(key, None)
        first = rs.events.get(fkey)
        if first is None:
            return
        now = snapshot(rs, first)
        if not any(p.state == "open" for p in now):
            self.entries.pop(key, None)
            return
        if previews is not None:
            self._keep(key, fp, previews)
        fresh = await asyncio.to_thread(fingerprint, reg, rs, now)
        if fresh == fp:
            self.asked.discard(key)
            for fn in list(BOARD_LISTENERS):
                try:
                    fn(reg.key, fkey)
                except Exception:  # noqa: BLE001
                    log.exception("board listener failed")
        elif key in self.asked:  # changed while solving and a page is waiting on it: solve the board as it is now
            self.asked.discard(key)
            self.start(reg, rs, fkey, fresh, now)

    def shown_layout(self, reg: Registry, ev: "rc.RaidEvent") -> list[list[str]] | None:
        """The flat groups (padded to whole rosters) of the picture the server holds for raid `ev` — the latest one,
        current or being placed again: what the page drew and a drag is applied to. None when there is none (the
        page drew the officers' layout) or that raid's solve failed."""
        e = self.entries.get((reg.key, rc.event_id(ev) or ev.key))
        rosters = e.previews.get(ev.key) if e is not None else None
        if not isinstance(rosters, list):
            return None
        k = rc.groups_per_roster(reg, rc.run_size(reg, ev))
        return [list(g) for r in rosters for g in (list(r.groups) + [[] for _ in range(k - len(r.groups))])]


def cache_of(bot) -> BoardCache:
    """The bot's one board cache (made on first use; test bots get theirs the same way)."""
    c = bot.__dict__.get("event_boards")
    if c is None:
        c = bot.__dict__["event_boards"] = BoardCache()
    return c
