"""Auto-fill groups (the owner's tweaks C, 2026-11-24): the Events board before the lock is the OFFICERS' board, and
nothing is solved until an officer presses the button. One press = ONE run of the lock's own solve for one raid
(rc.autofill → rc._solve_run: the run's strategy, the lock's budget, `earlier` from rc.earlier_counts for a later
raid), written into that raid's board as placements (ev.auto_placed) — hand placements and kept-off pins kept. This
module only decides WHEN it runs and what the page is told meanwhile.

- `AutoFill` (one per bot: `autofill_of(bot)`), keyed (guild key, raid key). `start` refuses in one sentence (the raid
  is locked, nobody joined, a lock-side solve is running) or starts ONE background task for the raid (a second press
  while it works gets the same task) and answers at once; the page shows `state`'s working words until the board
  updates (the write publishes `run:<raid key>`; AUTOFILL_LISTENERS hear every end that writes nothing).
- The solve runs on a dedicated PREVIEW_WORKERS executor, never on the event loop, over a SNAPSHOT of the event's raids
  taken on the loop (`snapshot` + `frozen`): the thread never reads a run the loop is writing.
- The write: under the raid's board lock, only when the board is still the one it solved from (rc.board_rev) and the
  raid is still open — else nothing is written and the sentence says so (AUTOFILL_CHANGED). A failure keeps the
  solver's own sentence (rc.solver_error_text); both stay on the page while the board is unchanged.
- The lock comes first: RaidMixin.solving (every lock, Preview lock and split preview) calls `stop_all` once it holds
  the run, which abandons every running auto-fill (solver.Stop → CpSolver.stop_search): it writes nothing and says so
  (AUTOFILL_STOPPED). While such a solve runs no auto-fill starts (AUTOFILL_BUSY)."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from . import raidcycle as rc
from .constants import PAGE_WORDS
from .registry import Registry, RegistryError
from .roster import solver

log = logging.getLogger(__name__)

PREVIEW_WORKERS = 1  # auto-fill solves at once in the process: each is a lock-sized CP-SAT solve (8 search workers)
AUTOFILL_LISTENERS: list = []  # fn(guild key, raid key): an auto-fill started or ended (the web publishes `run:<raid key>`)


def snapshot(rs: "rc.RaidStore", ev: "rc.RaidEvent") -> list["rc.RaidEvent"]:
    """The event's raids, deep-copied ON THE LOOP (where run writes happen), in order: what one solve reads."""
    return [p.model_copy(deep=True) for p in rc.event_parts(rs, rc.first_part(rs, ev))]


def frozen(rs: "rc.RaidStore", parts: list["rc.RaidEvent"]) -> SimpleNamespace:
    """The run store as the solve reads it (seat_bonus's sat-out looks at the other runs; the comp policy is a file),
    taken on the loop: a copy of the run index with the event's own raids replaced by `parts` (the snapshot)."""
    return SimpleNamespace(events={**rs.events, **{p.key: p for p in parts}}, store=rs.store, key=rs.key)


class AutoFill:
    def __init__(self, bot=None) -> None:
        self.bot = bot  # whose RaidMixin.solving marks a lock-side solve (bot._solver_locks), and whose board lock the write takes
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.stops: dict[tuple[str, str], solver.Stop] = {}
        self.said: dict[tuple[str, str], tuple[str, str]] = {}  # raid → (the outcome sentence, the board it was about)
        self.solves = 0  # solves started (tests)
        self._pool: ThreadPoolExecutor | None = None

    def pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=PREVIEW_WORKERS, thread_name_prefix="autofill")
        return self._pool

    def busy(self) -> bool:
        """A lock-side solve holds a run somewhere in the process (RaidMixin.solving's locks)."""
        locks = (self.bot.__dict__.get("_solver_locks") or {}) if self.bot is not None else {}
        return any(lk.locked() for lk in list(locks.values()))

    def stop_all(self) -> None:
        """A lock-side solve begins: every running auto-fill is abandoned (it writes nothing)."""
        for st in list(self.stops.values()):
            st.stop()

    def state(self, reg: Registry, ev: "rc.RaidEvent") -> dict:
        """What the page shows beside the button for raid `ev`: {working, words, failed} — the working words while it
        runs; afterwards the sentence of an auto-fill that wrote nothing, while the board is still the one it was about."""
        key = (reg.key, ev.key)
        if key in self.tasks:
            return {"working": True, "words": PAGE_WORDS["autofill_working"], "failed": False}
        said = self.said.get(key)
        if said is not None and said[1] == rc.board_rev(reg, ev):
            return {"working": False, "words": said[0], "failed": True}
        return {"working": False, "words": None, "failed": False}

    def refusal(self, reg: Registry, ev: "rc.RaidEvent") -> str | None:
        if ev.state != "open":
            return PAGE_WORDS["autofill_locked"]
        if not ev.by_status("in"):
            return PAGE_WORDS["autofill_nobody"]
        if self.busy():
            return PAGE_WORDS["autofill_busy"]
        return None

    def start(self, reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent", *, by: str, by_id: int | None, via: str) -> asyncio.Task:
        """Press Auto-fill groups on raid `ev` (on the loop): RegistryError with the refusal's sentence, else the task
        (already running → that task). The task's result is the outcome sentence, None when the board was filled."""
        key = (reg.key, ev.key)
        if key in self.tasks:
            return self.tasks[key]
        if (why := self.refusal(reg, ev)):
            raise RegistryError(why)
        self.said.pop(key, None)
        self.stops[key] = stop = solver.Stop()
        self.solves += 1
        task = asyncio.get_running_loop().create_task(self._run(reg, rs, ev.key, stop, by, by_id, via))
        self.tasks[key] = task
        self._tell(reg.key, ev.key)
        return task

    def _solve(self, reg: Registry, view, part: "rc.RaidEvent", stop: solver.Stop) -> tuple[list[list[str]], list[str]]:
        """BLOCKING (the executor): who the raids before it will roster, then this raid's own lock solve."""
        earlier = rc.earlier_counts(reg, view, part, stop=stop)
        return rc.autofill(reg, view, part, earlier=earlier, stop=stop)

    async def _run(self, reg: Registry, rs: "rc.RaidStore", key: str, stop: solver.Stop, by: str, by_id: int | None, via: str) -> str | None:
        k = (reg.key, key)
        said: str | None = None
        answer = None
        try:
            live = rs.events[key]
            rev = rc.board_rev(reg, live)
            parts = snapshot(rs, live)
            part = next(p for p in parts if p.key == key)
            try:
                answer = await asyncio.get_running_loop().run_in_executor(self.pool(), self._solve, reg, frozen(rs, parts), part, stop)
            except solver.Stopped:
                said = PAGE_WORDS["autofill_stopped"]
            except Exception as e:  # noqa: BLE001 — the solver's own failure is its sentence; anything else is logged too
                if not isinstance(e, (RuntimeError, solver.Conflict)):
                    log.exception("auto-fill failed (%s %s)", reg.key, key)
                said = rc.solver_error_text(e)
        finally:
            self.tasks.pop(k, None)
            self.stops.pop(k, None)
        if answer is not None:
            layout, auto = answer
            said = await self.write(reg, rs, key, rev, layout, auto, stop, by, by_id, via)
        live = rs.events.get(key)
        if said is not None and live is not None:
            self.said[k] = (said, rc.board_rev(reg, live))
        self._tell(reg.key, key)
        return said

    async def write(self, reg: Registry, rs: "rc.RaidStore", key: str, rev: str, layout: list[list[str]], auto: list[str],
                    stop: solver.Stop, by: str, by_id: int | None, via: str) -> str | None:
        """The answer written as the raid's board, under its board lock — only on the board it was solved from, while
        the raid is open and no lock stopped it. → None when written, else the sentence."""
        from . import ops as ops_mod
        from .raid_views import run_title

        bot = self.bot
        async with bot.board_lock(reg, key):
            live = rs.events.get(key)
            if stop.stopped:
                return PAGE_WORDS["autofill_stopped"]
            if live is None or live.state != "open" or rc.board_rev(reg, live) != rev:
                return PAGE_WORDS["autofill_changed"]
            live.log.append(f"{by}: auto-filled the board")
            await bot._write_board(reg, rs, live, layout, by, via=via, auto=auto)
        try:
            await ops_mod.audited(bot.ops, reg, by, f"auto-filled the board of {run_title(reg, live)}", via=via, by_id=by_id)
        except Exception:  # noqa: BLE001 — the board is written; an audit line that fails never undoes it
            log.exception("auto-fill audit line failed (%s %s)", reg.key, key)
        return None

    def _tell(self, gk: str, key: str) -> None:
        for fn in list(AUTOFILL_LISTENERS):
            try:
                fn(gk, key)
            except Exception:  # noqa: BLE001
                log.exception("auto-fill listener failed")


def autofill_of(bot) -> AutoFill:
    """The bot's one AutoFill (made on first use; test bots get theirs the same way)."""
    c = bot.__dict__.get("event_autofill")
    if c is None:
        c = bot.__dict__["event_autofill"] = AutoFill(bot)
    return c
