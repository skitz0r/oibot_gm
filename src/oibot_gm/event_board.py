"""The Events page's board before the lock: every open raid of an event drawn as its lock would place people NOW
(phase 4, design §7a). The placement is the lock's own solve (rc.event_board_preview → rc._solve_run with the lock's
strategy and budget), so it is the roster lock_run then builds; this module only decides WHEN to solve and keeps the
answer.

- `fingerprint(reg, view, parts)` = only what the solve reads, computed the way the solve computes it (players_for —
  answers, characters, ranks, attendance, the order people joined in —, seat_bonus — the weights, regulars, sat out —,
  the pins, the officers' board, the strategy, size, rosters, minimums, the comp policy) for each open raid, and each
  locked raid's roster for the raids after it. A change to any of them is a new fingerprint: a picture is never shown as
  current for inputs it wasn't solved from; a change to none of them (a re-press with the same answer, a card id written
  to the run) is the same fingerprint and no new solve. It reads the SNAPSHOT taken on the loop (`snapshot` +
  `frozen`), never the live store from its thread.
- `BoardCache` (one per bot: `cache_of(bot)`), keyed (guild key, event key = the first raid's run key), holding ONE
  entry per event: (fingerprint, previews, time). `get` answers at once: "ready" with the previews when the fingerprint
  matches; else it starts ONE background solve for the event (never two at once for an event, never twice for one
  fingerprint) and answers "working" with the previous picture (or None). The solves run on a dedicated
  PREVIEW_WORKERS-thread executor — never on the event loop, never in the request threads' pool — so six events opened
  in six tabs queue there one after another. An event nobody joined is answered inline (nothing to solve). A finished
  solve is kept; when the fingerprint still holds, BOARD_LISTENERS hear (guild key, event key) and the web publishes
  `run:<event key>` so the page reloads. A solve that went stale meanwhile is kept as the previous picture and solved
  again for the new fingerprint when a page asked for it meanwhile. A solve that fails unexpectedly is kept too, as
  every open raid's BOARD_FAILED sentence under that fingerprint: logged once, never solved again for it. An entry
  goes when its event has no open raid any more (looked at every `get` of the guild) and past MAX_BOARDS (least
  recently used).
- The lock comes first: the pictures share the CPU with the lock's own CP-SAT solve, and a lock slowed to the solver's
  wall-clock net (solver.WALL_CEILING) is no longer repeatable. So while any solve holds a run (RaidMixin.solving: a
  lock, Preview lock, an auto-fill, a split preview) no picture starts — it waits — and a picture solving when one
  begins is abandoned at once (solver.Stop → CpSolver.stop_search; the lock never waits on it) and solved again when
  the last such solve ends (`pause` / `resume`, called by RaidMixin.solving).
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
from types import SimpleNamespace

from . import raidcycle as rc
from .constants import PAGE_WORDS
from .registry import Registry
from .roster import solver

log = logging.getLogger(__name__)

PREVIEW_WORKERS = 1  # board solves at once in the process: each is a lock-sized CP-SAT solve (8 search workers)
MAX_BOARDS = 32  # events whose picture is kept (least recently used goes first)
BOARD_LISTENERS: list = []  # fn(guild key, event key): a new current picture (the web publishes `run:<event key>`)


def snapshot(rs: "rc.RaidStore", first: "rc.RaidEvent") -> list["rc.RaidEvent"]:
    """The event's raids, deep-copied ON THE LOOP (where run writes happen), in order: what one solve and its
    fingerprint read, consistent with each other."""
    return [p.model_copy(deep=True) for p in rc.event_parts(rs, rc.first_part(rs, first))]


def frozen(rs: "rc.RaidStore") -> SimpleNamespace:
    """The run store as the solve reads it (seat_bonus's sat-out looks at the other runs; the comp policy is a file),
    taken on the loop: a copy of the run index, so a thread never iterates the live one while a run is added."""
    return SimpleNamespace(events=dict(rs.events), store=rs.store, key=rs.key)


def needs_solve(parts: list["rc.RaidEvent"]) -> bool:
    return any(p.state == "open" and p.by_status("in") for p in parts)


def fingerprint(reg: Registry, view, parts: list["rc.RaidEvent"]) -> str:
    """What the board's solves read (see the module doc), hashed. BLOCKING (players_for reads attendance): a thread.
    `view` = frozen(rs), `parts` = snapshot()."""
    from .policy import PolicyStore

    rows: list = [PolicyStore(view.store, view.key).comp_constraints()]
    for p in parts:
        if p.state != "open":  # a locked raid's roster is what the raids after it prefer
            rows.append([p.key, p.state, [[list(g) for g in r.groups] for r in p.all_rosters],
                         sorted(str(rc.player_uid(p, x)) for x in p.seated())])
            continue
        players = rc.players_for(reg, p)
        size = rc.run_size(reg, p)
        strategy = rc.run_strategy(reg, p)
        raid_id = p.instance if p.instance in reg.profile.raids else next(iter(reg.profile.raids))
        rows.append([p.key, p.state, p.part, p.follows, p.starts_at,
                     # the answers as the solve reads them: status, character, class, spec, offspec, role; the pins, the
                     # officers' board, the run's own split — never the time of a re-press
                     [(k, s.display_name, s.character, s.cls, s.spec, s.offspec, s.role, s.status, s.source)
                      for k, s in sorted(p.signups.items())],
                     sorted(p.pins.items()), p.layout, p.split_strategy,
                     [x.model_dump() for x in players],  # the join order (pos), rank, attendance, tank alts
                     rc.seat_bonus(reg, view, p, players), strategy, size, rc.run_rosters(reg, p), rc.run_kind(reg, p),
                     reg.minimums(raid_id, size), reg.role_bounds(raid_id, size),
                     (int(reg.raid_def(raid_id)["weights"].get("sat_out", 2)),
                      sorted(rc.sat_out_last_window(reg, view, p, first_only=True))) if strategy == "rotation" else None])
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Entry:
    fp: str
    previews: dict
    at: float


class BoardCache:
    def __init__(self, bot=None) -> None:
        self.bot = bot  # whose RaidMixin.solving marks a lock-side solve (bot._solver_locks)
        self.entries: OrderedDict[tuple[str, str], Entry] = OrderedDict()
        self.running: dict[tuple[str, str], str] = {}  # event → the fingerprint being solved now
        self.asked: set[tuple[str, str]] = set()  # events a page asked for another fingerprint while one was solving
        self.waiting: dict[tuple[str, str], tuple] = {}  # events whose picture waits for a lock-side solve to end
        self.stops: dict[tuple[str, str], solver.Stop] = {}
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.solves = 0  # solves started (tests, the Host's curiosity)
        self._pool: ThreadPoolExecutor | None = None

    def pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS, thread_name_prefix="board-preview")
        return self._pool

    def busy(self) -> bool:
        """A lock-side solve holds a run somewhere in the process (RaidMixin.solving's locks)."""
        locks = (self.bot.__dict__.get("_solver_locks") or {}) if self.bot is not None else {}
        return any(lk.locked() for lk in list(locks.values()))

    def pause(self) -> None:
        """A lock-side solve begins: every picture solving now is abandoned (it is solved again at `resume`)."""
        for st in list(self.stops.values()):
            st.stop()

    def resume(self) -> None:
        """A lock-side solve ended: once none holds a run, the pictures that waited start again (each re-reads its
        event: the fingerprint may have moved meanwhile)."""
        if self.busy() or not self.waiting:
            return
        waiting, self.waiting = self.waiting, {}
        loop = asyncio.get_running_loop()
        for key, (reg, rs) in waiting.items():
            if key not in self.tasks:  # a task already working on the event re-reads it itself
                self.tasks[key] = loop.create_task(self._again(reg, rs, key[1]))

    async def _again(self, reg: Registry, rs: "rc.RaidStore", fkey: str) -> None:
        key = (reg.key, fkey)
        try:
            first = rs.events.get(fkey)
            if first is None:
                return
            parts, view = snapshot(rs, first), frozen(rs)
            if not needs_solve(parts):
                return
            fp = await asyncio.to_thread(fingerprint, reg, view, parts)
            have = self.entries.get(key)
            if have is None or have.fp != fp:
                self.tasks.pop(key, None)  # this task hands over to the solve it starts
                self.start(reg, rs, fkey, fp, parts, view)
        finally:
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)

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
        previews | None) while a solve runs or waits (one is started when none is); ("locked", None) when no raid is
        open."""
        first = rc.first_part(rs, ev)
        key = (reg.key, first.key)
        self.prune(reg, rs)
        parts, view = snapshot(rs, first), frozen(rs)
        if not any(p.state == "open" for p in parts):
            return "locked", None
        fp = await asyncio.to_thread(fingerprint, reg, view, parts)
        have = self.entries.get(key)
        if have is not None and have.fp == fp:
            self.entries.move_to_end(key)
            return "ready", have.previews
        if not needs_solve(parts):  # nobody joined: empty groups, no solve, nothing to wait for
            previews = await asyncio.to_thread(rc.event_board_preview, reg, view, first, parts)  # board_rosters reads attendance
            self._keep(key, fp, previews)
            return "ready", previews
        self.start(reg, rs, first.key, fp, parts, view)
        return "working", (have.previews if have is not None else None)

    def start(self, reg: Registry, rs: "rc.RaidStore", fkey: str, fp: str, parts: list["rc.RaidEvent"], view=None) -> None:
        """One background solve for the event; while one runs, a page asking for another fingerprint is noted and the
        running one solves again when it ends (never two at once, never twice for one fingerprint). While a lock-side
        solve holds a run, nothing starts: the event waits for `resume`."""
        key = (reg.key, fkey)
        if key in self.running:
            if self.running[key] != fp:
                self.asked.add(key)
            return
        if self.busy():
            self.waiting[key] = (reg, rs)
            return
        self.running[key] = fp
        self.stops[key] = solver.Stop()
        self.solves += 1
        self.tasks[key] = asyncio.get_running_loop().create_task(self._solve(reg, rs, fkey, fp, parts, view or frozen(rs)))

    async def _solve(self, reg: Registry, rs: "rc.RaidStore", fkey: str, fp: str, parts: list["rc.RaidEvent"], view) -> None:
        key = (reg.key, fkey)
        previews, stop = None, self.stops[key]
        try:
            previews = await asyncio.get_running_loop().run_in_executor(self.pool(), rc.event_board_preview, reg, view, parts[0], parts, stop)
        except solver.Stopped:  # a lock-side solve began: this picture waits for it and is solved again after
            self.waiting[key] = (reg, rs)
        except Exception:  # noqa: BLE001 — a raid's own failure is its sentence; this is the unexpected kind
            log.exception("board preview failed (%s %s); kept as failed for this fingerprint", reg.key, fkey)
            previews = {p.key: PAGE_WORDS["board_failed"] for p in parts if p.state == "open"}
        finally:
            self.running.pop(key, None)
            self.stops.pop(key, None)
            self.tasks.pop(key, None)
        if previews is None:
            self.resume()  # the lock that stopped it may already be over
            return
        first = rs.events.get(fkey)
        if first is None:
            return
        now = snapshot(rs, first)
        if not any(p.state == "open" for p in now):
            self.entries.pop(key, None)
            return
        self._keep(key, fp, previews)
        fresh = await asyncio.to_thread(fingerprint, reg, frozen(rs), now)
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
        current or being placed again, always with the officers' current moves on top (rc.overlay_board, as the page drew it):
        what a drag is applied to. None when there is none (the page drew the officers' layout) or that raid's solve
        failed. `ev` = the live raid."""
        key = (reg.key, rc.event_id(ev) or ev.key)
        e = self.entries.get(key)
        rosters = e.previews.get(ev.key) if e is not None else None
        if not isinstance(rosters, list):
            return None
        k = rc.groups_per_roster(reg, rc.run_size(reg, ev))

        def flat(rr: list) -> list[list[str]]:
            return [list(g) for r in rr for g in (list(r.groups) + [[] for _ in range(k - len(r.groups))])]

        raw, over = flat(rosters), flat(rc.overlay_board(reg, ev, rosters))
        # always overlaid (the picture may predate the last drag); on a current picture the overlay moves nobody — the
        # officers' placements are hard pins there — so the picture is returned in its own order, as the page drew it
        if len(raw) == len(over) and all(set(a) == set(b) for a, b in zip(raw, over)):
            return raw
        return over


def cache_of(bot) -> BoardCache:
    """The bot's one board cache (made on first use; test bots get theirs the same way)."""
    c = bot.__dict__.get("event_boards")
    if c is None:
        c = bot.__dict__["event_boards"] = BoardCache(bot)
    return c
