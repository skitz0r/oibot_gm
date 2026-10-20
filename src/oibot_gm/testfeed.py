"""The test bench's event feed (dual interfaces phase 2, B9 / owner decision D20): every card, puppet DM, scheduler
step and answer of a TEST run as one stream, so a rehearsal can be followed on the site (the Members page's test card,
the Rosters page's Test tab) while its cards post in the channel the command ran in and the puppets' DMs reach the
tester as one digest per batch.

Nothing here is written to the data repo: a ring of events per (guild key, run key) in this process, gone at a
restart (a rehearsal lasts minutes). Fed from the hooks the real traffic already goes through:

- `observe(reg, ev)` on every RaidStore.save of a test run (rc.RUN_LISTENERS, `install`): the new `ev.log` lines are
  the steps (opened, locked, confirmations sent, cancelled — whoever did it: the scheduler, a press, the site), a new
  officer card or a (re)posted sheet is a card, a changed answer on the sheet is an answer;
- `note(reg, ev, kind, text)` from the two places that carry words the run file doesn't keep: the run's updates
  thread (RaidMixin.post_run_update — "🧩 short 1 healer", "⌛ no confirmation from …") and a puppet's DM
  (SetupMixin.puppet_dm — what the puppet would have got).

Each new event calls LISTENERS (the web's `publish("test:<run>", guild)`), so `useLive(["test:"])` reloads the feed.
Text is made readable for the site here, once: Discord emoji, stamps and mentions become words (`plain`).

Also the test bench's tempo resolver (`tempo_of`): a TEMPOS name or a number of minutes → the four minute marks."""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime

from . import raidcycle as rc
from .presets import DEFAULT_TEMPO, TEMPOS

log = logging.getLogger(__name__)

KINDS = ("card", "dm", "step", "answer")
RING = 300  # events kept per test run
RUNS_KEPT = 12  # test runs kept per guild (the oldest ring goes)
FIRST_LOOK = 8  # a run first seen with a long log (a restart mid-rehearsal): only its newest lines
LISTENERS: list = []  # fn(guild_key, run_key) after every new event — the web's publish("test:<run key>", guild)

_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_STAMP = re.compile(r"<t:(\d+)(?::[tTdDfFR])?>")
_USER = re.compile(r"<@!?(\d+)>")
_CHAN = re.compile(r"<#(\d+)>")
_ROLE = re.compile(r"<@&(\d+)>")
_MD = re.compile(r"(\*\*|__|~~|`)")


def plain(reg, text: str, name_of=None) -> str:
    """A Discord line as the site reads it: custom emoji dropped (the words around them carry the meaning), `<t:…>`
    stamps in the guild's 12-hour clock (Registry.local12), member mentions by display name, channels by name
    (`name_of(channel id)`), markdown markers and `-# ` subtext prefixes gone. One line per Discord line."""
    s = str(text or "")
    s = _EMOJI.sub("", s)

    def stamp(m):
        try:
            return reg.local12(datetime.fromtimestamp(int(m[1]), tz=reg.tz))
        except Exception:  # noqa: BLE001
            return "?"
    s = _STAMP.sub(stamp, s)

    def user(m):
        mm = getattr(reg, "members", {}).get(int(m[1]))
        return f"@{mm.display_name}" if mm else "@someone"
    s = _USER.sub(user, s)
    s = _CHAN.sub(lambda m: f"#{(name_of(int(m[1])) if name_of else None) or 'channel'}", s)
    s = _ROLE.sub("@role", s)
    s = _MD.sub("", s)
    lines = [re.sub(r"^(-#|#{1,3})\s+", "", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def view_text(view) -> str:
    """The words of a layout card (a LayoutView's TextDisplays, in order) — a puppet's Confirm / Can't card as text."""
    out = []
    try:
        for it in view.walk_children():
            content = getattr(it, "content", None)
            if isinstance(content, str) and content.strip():
                out.append(content)
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out)


def dm_kind(view) -> str:
    """What a puppet DM is, from its buttons: confirm (PlaceButton: Confirm / Can't make it), fill (FillButton: a fill
    or pre-lock ask), sheet (SignupButton: the open / nudge DM with Join · Bench · No thanks), else note."""
    from .raid_buttons import FillButton, PlaceButton, SignupButton

    found = set()
    try:
        for it in (view.walk_children() if view is not None else ()):
            found.add(type(it))
    except Exception:  # noqa: BLE001
        pass
    if PlaceButton in found:
        return "confirm"
    if FillButton in found:
        return "fill"
    if SignupButton in found:
        return "sheet"
    return "note"


DM_WORDS = {"confirm": ("Confirm ask", "Confirm asks"), "fill": ("fill ask", "fill asks"), "sheet": ("sheet DM", "sheet DMs"), "note": ("message", "messages")}


def digest_line(counts: dict[str, int]) -> str:
    """'12 Confirm asks sent to puppets · 3 fill asks' — the digest's first line, kinds in a fixed order."""
    parts = [f"{n} {DM_WORDS[k][0 if n == 1 else 1]}" for k in DM_WORDS if (n := int(counts.get(k) or 0))]
    if not parts:
        return "Nothing sent to puppets"
    return " · ".join([parts[0] + " sent to puppets", *parts[1:]])


def tempo_of(tempo=None, *, start_in=None, nudge_in=None, lock_in=None, confirm_in=None) -> tuple[str, int, int, int, int]:
    """(label, start_in, nudge_in, lock_in, confirm_in) in minutes for a test run, from a presets.TEMPOS name
    ('quick' | 'standard' | 'slow'), or a number of minutes to the start (the marks keep the standard tempo's
    proportions, strictly ordered), or — no tempo — the four explicit marks (the old API), defaulting to the
    standard tempo. ValueError naming the options when it can't be made."""
    if tempo not in (None, ""):
        key = str(tempo).strip().lower()
        if key in TEMPOS:
            return TEMPOS[key]
        try:
            start = int(float(key))
        except ValueError:
            raise ValueError(f"tempo is one of {', '.join(TEMPOS)}, or minutes to the start") from None
        if start < 4:
            raise ValueError("a test run needs at least 4 minutes to its start (nudge, lock and the confirmation deadline come before it)")
        _, s0, n0, l0, c0 = TEMPOS[DEFAULT_TEMPO]
        # each mark clamped below the one before it (start → nudge → lock → confirm), so every start ≥ 4 has a
        # strictly ordered set: 4 → 3, 2, 1 (clamping upward from confirm made nudge = lock at 4 minutes)
        nudge = min(start - 1, round(start * n0 / s0))
        lock = min(nudge - 1, round(start * l0 / s0))
        confirm = max(0, min(lock - 1, round(start * c0 / s0)))
        if not start > nudge > lock > confirm >= 0:
            raise ValueError("a test run needs at least 4 minutes to its start")
        return (f"{start} min", start, nudge, lock, confirm)
    _, s0, n0, l0, c0 = TEMPOS[DEFAULT_TEMPO]
    vals = [int(v) if v is not None else d for v, d in ((start_in, s0), (nudge_in, n0), (lock_in, l0), (confirm_in, c0))]
    start, nudge, lock, confirm = vals
    if not start > nudge > lock > confirm >= 0:
        raise ValueError("need start_in > nudge_in > lock_in > confirm_in ≥ 0 (all minutes)")
    return (f"{start} min", start, nudge, lock, confirm)


class TestFeed:
    """Per (guild key, run key): a ring of events {seq, at, at_label, kind, text, actor} and what was last seen of the
    run (log length, cards, sheet message, answers, state) so `observe` reports only what changed. Thread-safe:
    run saves come from the loop and from worker threads (the web)."""

    __test__ = False  # not a pytest class

    def __init__(self):
        self._lock = threading.Lock()
        self._rings: dict[tuple[str, str], deque] = {}
        self._seen: dict[tuple[str, str], dict] = {}
        self._seq = 0

    # ---- writing
    def record(self, gk: str, run: str, kind: str, text: str, actor: str | None = None, at: datetime | None = None, *, label: str | None = None) -> dict | None:
        text = str(text or "").strip()
        if not text or kind not in KINDS:
            return None
        with self._lock:
            self._seq += 1
            at = at or datetime.now().astimezone()
            e = {"seq": self._seq, "at": at.isoformat(), "at_label": label or "", "kind": kind, "text": text[:600], "actor": actor}
            key = (gk, run)
            ring = self._rings.get(key)
            if ring is None:
                ring = self._rings[key] = deque(maxlen=RING)
                mine = [k for k in self._rings if k[0] == gk]
                for old in mine[:-RUNS_KEPT]:
                    self._rings.pop(old, None)
                    self._seen.pop(old, None)
            ring.append(e)
        for fn in list(LISTENERS):
            try:
                fn(gk, run)
            except Exception:  # noqa: BLE001 — a listener never breaks the feed
                log.exception("test feed listener failed")
        return e

    def observe(self, reg, ev, name_of=None) -> list[dict]:
        """Diff a test run against what was last seen and record what changed. Called after every save of the run."""
        if not rc.is_test_run(reg, ev):
            return []
        key = (reg.key, ev.key)
        log_lines = list(ev.log or [])
        answers = {uid: (sg.status, sg.character) for uid, sg in list(ev.signups.items())}
        cards = set((ev.cards or {}).keys())
        with self._lock:
            seen = self._seen.get(key)
            if seen is None:
                seen = {"log": max(0, len(log_lines) - FIRST_LOOK), "answers": {}, "cards": set(), "message": None, "state": None}
            self._seen[key] = {"log": len(log_lines), "answers": answers, "cards": cards, "message": ev.message_id, "state": ev.state}
        label = reg.local12(datetime.now(reg.tz))
        out: list[dict] = []

        def add(kind, text, actor=None):
            e = self.record(reg.key, ev.key, kind, text, actor, label=label)
            if e:
                out.append(e)

        if ev.message_id and ev.message_id != seen["message"]:
            where = name_of(ev.channel_id) if name_of and ev.channel_id else None
            add("card", ("Sheet re-posted" if seen["message"] else "Sheet posted") + (f" in #{where}" if where else ""))
        for k in sorted(cards - seen["cards"]):
            where = name_of(ev.cards_channel_id) if name_of and ev.cards_channel_id else None
            what = "Health card" if k == "health" else (f"Lock card · roster {int(k[5:]) + 1}" if k.startswith("lock:") and k[5:].isdigit() else f"{k} card")
            add("card", f"{what} posted" + (f" in #{where}" if where else ""))
        changed = [(uid, status, char) for uid, (status, char) in answers.items() if seen["answers"].get(uid) != (status, char) and uid in ev.signups]
        # a changed answer is ONE event (kind answer, in the sheet's words): set_signup's own log line for it
        # ('Kessa in as Kessa (test)') is not repeated as a step
        said = {f"{ev.signups[uid].display_name} {status} as " for uid, status, _c in changed}
        new_lines = log_lines[seen["log"]:]
        for line in new_lines:
            if not any(line.startswith(p) for p in said):
                add("step", plain(reg, line, name_of))
        if seen["state"] is not None and ev.state != seen["state"] and not any(ev.state in ln for ln in new_lines):
            add("step", f"Run is now {ev.state}")
        for uid, status, char in changed:
            sg = ev.signups[uid]
            was = seen["answers"].get(uid)
            word = rc.LABELS.get(status, status)
            text = f"{sg.display_name}: {word}" + (f" as {char}" if char and (was is None or was[1] != char) else "")
            add("answer", text, sg.source)
        for uid in set(seen["answers"]) - set(answers):
            m = getattr(reg, "members", {}).get(int(uid)) if str(uid).isdigit() else None
            add("answer", f"{m.display_name if m else uid}: answer removed")
        return out

    # ---- reading
    def events(self, gk: str, run: str | None = None, after: int = 0, limit: int = RING) -> list[dict]:
        """A guild's events (one run's, or every test run's interleaved), oldest first, `seq > after`, the last `limit`."""
        with self._lock:
            rings = [(k[1], list(r)) for k, r in self._rings.items() if k[0] == gk and (run is None or k[1] == run)]
        rows = [{**e, "run": rk} for rk, ring in rings for e in ring if e["seq"] > after]
        rows.sort(key=lambda e: e["seq"])
        return rows[-limit:]

    def runs(self, gk: str) -> list[str]:
        with self._lock:
            return [k[1] for k in self._rings if k[0] == gk]

    def forget(self, gk: str, run: str | None = None) -> None:
        with self._lock:
            for k in [k for k in self._rings if k[0] == gk and (run is None or k[1] == run)]:
                self._rings.pop(k, None)
                self._seen.pop(k, None)


FEED = TestFeed()


def note(reg, ev, kind: str, text: str, actor: str | None = None, name_of=None) -> dict | None:
    """Record one event for a TEST run (anything else is ignored): a thread line, a puppet's DM. Never raises."""
    try:
        if ev is None or not rc.is_test_run(reg, ev):
            return None
        return FEED.record(reg.key, ev.key, kind, plain(reg, text, name_of), actor, label=reg.local12(datetime.now(reg.tz)))
    except Exception:  # noqa: BLE001 — the feed is a mirror; it never breaks the step it mirrors
        log.exception("test feed note failed")
        return None


def install(lookup, name_of=None) -> None:
    """Listen to every run save: `lookup(guild key, run key)` → (reg, ev) or None; a test run is observed. One
    listener per process (a newer install replaces it — the web app is created once, tests create many)."""

    def test_run_saved(gk: str, run_key: str) -> None:
        try:
            got = lookup(gk, run_key)
            if got is not None:
                FEED.observe(got[0], got[1], name_of)
        except Exception:  # noqa: BLE001
            log.exception("test feed observe failed")

    rc.RUN_LISTENERS[:] = [f for f in rc.RUN_LISTENERS if getattr(f, "__name__", "") != test_run_saved.__name__] + [test_run_saved]
