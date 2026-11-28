"""The event page's Bot activity tab (design 2026-11-28-event-activity-tab): every DM the bot sent for one event and what
came back — what it still waits on first (soonest deadline first), then the history (newest first). ONE builder,
activity_json, over what the event already stores: the Confirm asks (each member's placement_asks on a raid's roster
key, the deadline the raid's confirm_by), the fill and pre-lock asks (RaidEvent.fill_asks: a role switch is an ask of a
swap kind), the callouts a member sent from a DM card (Callout.source "dm"), and RaidEvent.dms — the opening DMs, the
signup reminders and an officer's Remind, recorded by raidcycle.record_dms since this tab began (an older event says so in
one sentence). A DM that could not be delivered is marked on its ask (FillAsk.dm_failed, the placement ask's "dm_failed");
a Confirm ask is this occurrence's only (rc._event_asks, and nothing asked after a finished event closed).
Every word a row prints is here; times are Registry.local12's plus a relative time. Nothing here writes or sends: the
one action, Withdraw, is the existing route (POST /api/run/{key}/withdraw) named on the row. Officers only (the route,
web/api_events.py); nothing is shown that the event page did not already show its officers."""
from __future__ import annotations

from datetime import datetime

from . import raidcycle as rc
from .registry import Registry

CLOSED = ("done", "cancelled")

KIND_WORDS = {"confirm": "Confirm", "fill": "Open seat", "prelock": "Could you come?", "swap": "Role switch",
              "open": "Sheet opening DM", "reminder": "Signup reminder", "remind": "Remind the unanswered",
              "callout": "Can't make it"}
KIND_ORDER = tuple(KIND_WORDS)
WORDS = {
    "pending_title": "Waiting for an answer",
    "history_title": "Sent and answered",
    "empty_pending": "The bot is not waiting on anyone for this event.",
    "empty_history": "The bot has sent no DMs for this event yet.",
    "unrecorded": "Opening DMs, reminders, undelivered DMs and Can't make it answers from a DM card before this list began were not recorded for this event.",
    "all_kinds": "Every kind", "everyone": "Everyone",
    "sent": "Sent", "closes": "Closes", "answered": "Answered", "closed": "Closed", "said": "Said",
    "withdraw": "Withdraw",
    "not_delivered": "Not delivered, so it waits on their Me page.",
    "not_delivered_then": "Not delivered, so it waited on their Me page.",
    "on_site": "Sent to their Me page, not by DM.",
    "held": "Said yes. It waits for the other half of the swap.",
    "paused": "Automated Fill Requests are paused",
}
CONFIRM_OUTCOMES = {"yes": "said yes", "no": "can't make it", "expired": "no answer in time",
                    "cancelled": "closed when the event was cancelled", "withdrawn": "withdrawn by an officer",
                    "released": "closed when their seat was released",
                    "unrostered": "closed when the event locked again without them", "ended": "closed when the event ended"}
ASK_OUTCOMES = {"yes": "said yes", "no": "said no", "expired": "no answer in time", "released": "closed without an answer",
                "cancelled": "closed when the event was cancelled", "withdrawn": "withdrawn by {by}",
                "withdrawn_anon": "withdrawn by an officer"}
# why a released ask closed (FillAsk.closed_why, raidcycle.release_ask); a yes held for its tied half reads
# "said yes, then …"
RELEASED = {"lock": "closed by the lock",
            "partner_no": "closed because the other half of the swap said no",
            "partner_expired": "closed because the other half of the swap had no answer in time",
            "partner_withdrawn": "closed because the other half of the swap was withdrawn",
            "partner_out": "closed because the other half of the swap can't come",
            "partner": "closed because the other half of the swap fell through",
            "callout": "closed because they called out", "no_thanks": "closed because they answered No thanks",
            "away": "closed because they are away that day", "out": "closed because they can't come any more",
            "taken": "closed because someone else took the seat first",
            "changed": "closed because the event changed while it was being sent"}
# an officer's Confirm / Can't make it for them (the placement ask's "officer")
FOR_THEM = {"yes": "confirmed for them by {by}", "no": "can't make it, answered for them by {by}"}
TONE = {"yes": "yes", "no": "no", "expired": "no"}


def _parse(at) -> datetime | None:
    try:
        return datetime.fromisoformat(str(at)) if at else None
    except ValueError:
        return None


def relative(reg: Registry, at, now: datetime | None = None) -> str:
    """'in 3 h', '20 min ago', 'just now', '4 days ago' — the relative time beside a Registry.local12 stamp."""
    t = _parse(at)
    if t is None:
        return ""
    now = now or reg.now_local()
    if t.tzinfo is None:
        t = t.replace(tzinfo=now.tzinfo)
    secs = (t - now).total_seconds()
    ahead, secs = secs > 0, abs(secs)
    if secs < 60:
        return "in under a minute" if ahead else "just now"
    if secs < 3600:
        n, unit = int(secs // 60), "min"
    elif secs < 48 * 3600:
        n, unit = int(secs // 3600), "h"
    else:
        n = int(secs // 86400)
        unit = "day" if n == 1 else "days"
    return f"in {n} {unit}" if ahead else f"{n} {unit} ago"


def _time(reg: Registry, label: str, at, now) -> dict:
    return {"label": label, "when": reg.local12(at), "rel": relative(reg, at, now)}


def _person(reg: Registry, uid, *, name: str | None = None, character: str | None = None, spec: str | None = None) -> dict:
    """A person as a row draws them: the character and the spec icon (class + spec), the name for the filter."""
    m = reg.members.get(int(uid)) if uid is not None else None
    char = None
    if m is not None:
        char = next((c for c in m.characters if character and c.matches(character)), None) or m.main
    cls, sp = (char.cls if char else ""), spec or (char.spec if char else "")
    return {"uid": str(uid) if uid is not None else None, "display_name": name or (m.display_name if m else str(uid)),
            "character": character or (char.label if char else (m.display_name if m else str(uid))),
            "cls": cls, "spec": sp, "icon_title": f"{sp} {cls}".strip()}


def _people_words(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def _occurrence_asks(m, p: "rc.RaidEvent") -> list[dict]:
    """`m`'s Confirm asks on THIS occurrence of `p`: rc._event_asks (asked since it opened — an older event's roster key is
    reused week to week) and, once it closed, nothing asked after."""
    end = p.closed_at if p.state in CLOSED and p.closed_at else None
    return [a for a in rc._event_asks(m, p) if end is None or (a.get("asked_at") or "") <= end]


def _row(kind: str, rid: str, where: str | None, **kw) -> dict:
    row = {"id": rid, "kind": kind, "kind_label": KIND_WORDS[kind], "where": where, "person": None, "people": [],
           "failed_people": [], "summary": None, "failed_summary": None, "times": [], "outcome": None, "tone": "waiting",
           "detail": None, "delivery": None, "withdraw": None, "sort": ""}
    row.update(kw)
    people = ([row["person"]] if row["person"] else []) + row["people"] + row["failed_people"]
    row["uids"] = sorted({p["uid"] for p in people if p.get("uid")})
    return row


def _confirm_rows(reg: Registry, p: "rc.RaidEvent", where, live: bool, now) -> tuple[list, list]:
    pending, history = [], []
    several = len(p.all_rosters) > 1
    for m in reg.members.values():
        for a in _occurrence_asks(m, p):
            i = next(k for k, x in enumerate(m.placement_asks) if x is a)
            seat = p.seat_of(m.discord_id)
            here = where
            if several and seat is not None:
                here = ", ".join(x for x in (where, f"roster {seat[0] + 1}") if x)
            person = _person(reg, m.discord_id, character=a.get("character"))
            not_sent = bool(a.get("dm_failed"))
            answer = a.get("answer")
            rid = f"confirm:{p.key}:{m.discord_id}:{i}"
            sent = _time(reg, WORDS["sent"], a.get("asked_at"), now) if a.get("asked_at") else None
            if answer is None and live:  # an unlock keeps a Confirm ask open: it still waits
                times = [t for t in (sent, _time(reg, WORDS["closes"], p.confirm_by, now) if p.confirm_by else None) if t]
                pending.append(_row("confirm", rid, here, person=person, times=times,
                                    delivery=WORDS["not_delivered"] if not_sent else WORDS["on_site"] if a.get("channel") == "web" else None,
                                    sort=p.confirm_by or "~"))
                continue
            if answer is None:  # an ask the event's end left open in a file from before ends closed them
                answer = "cancelled" if p.state == "cancelled" else "ended"
            key = "ended" if answer == "expired" and a.get("answered_by") == "end" else answer
            ended = a.get("answered_at")
            times = [t for t in (sent, _time(reg, WORDS["answered"] if answer in ("yes", "no") else WORDS["closed"], ended, now) if ended else None) if t]
            outcome = FOR_THEM[answer].format(by=a["officer"]) if answer in FOR_THEM and a.get("officer") else CONFIRM_OUTCOMES.get(key, key)
            history.append(_row("confirm", rid, here, person=person, times=times, outcome=outcome,
                                tone=TONE.get(answer, "closed"),
                                delivery=WORDS["not_delivered_then"] if not_sent else None,
                                sort=ended or a.get("asked_at") or ""))
    return pending, history


def _ask_kind(a: "rc.FillAsk") -> str:
    return "swap" if a.swap else "prelock" if a.phase == "prelock" else "fill"


def _ask_rows(reg: Registry, p: "rc.RaidEvent", where, live: bool, now) -> tuple[list, list]:
    pending, history = [], []
    may_withdraw = live and p.state in ("open", "locked")
    for i, a in enumerate(p.fill_asks):
        kind = _ask_kind(a)
        person = _person(reg, a.discord_id, name=a.display_name, character=a.character, spec=a.spec)
        not_sent = a.dm_failed
        rid = f"ask:{p.key}:{a.discord_id}:{i}"
        sent = _time(reg, WORDS["sent"], a.asked_at, now) if a.asked_at else None
        if a.pending and live:
            times = [t for t in (sent, _time(reg, WORDS["closes"], a.expires_at, now) if a.expires_at else None) if t]
            pending.append(_row(kind, rid, where, person=person, times=times, detail=a.reason or None,
                                outcome=WORDS["held"] if a.held else None, tone="yes" if a.held else "waiting",
                                delivery=WORDS["not_delivered"] if not_sent else WORDS["on_site"] if a.channel == "web" else None,
                                withdraw={"path": f"/api/run/{p.key}/withdraw", "uid": str(a.discord_id), "asked_at": a.asked_at,
                                          "label": WORDS["withdraw"]} if may_withdraw else None,
                                sort=a.expires_at or "~"))
            continue
        answer = a.answer or ("cancelled" if p.state == "cancelled" else "released")
        if answer == "withdrawn":
            outcome = ASK_OUTCOMES["withdrawn"].format(by=a.answered_by) if a.answered_by else ASK_OUTCOMES["withdrawn_anon"]
        elif answer == "released" and a.closed_why:
            held, why = a.closed_why.startswith("held_"), a.closed_why.removeprefix("held_")
            words = RELEASED.get(why, ASK_OUTCOMES["released"])
            outcome = f"said yes, then {words}" if held else words
        else:
            outcome = ASK_OUTCOMES.get(answer, answer)
            if answer in ("yes", "no") and a.answered_by:
                outcome += f", answered by {a.answered_by}"
        ended = a.answered_at
        times = [t for t in (sent, _time(reg, WORDS["answered"] if answer in ("yes", "no") else WORDS["closed"], ended, now) if ended else None) if t]
        history.append(_row(kind, rid, where, person=person, times=times, detail=a.reason or None, outcome=outcome,
                            tone=TONE.get(answer, "closed"), delivery=WORDS["not_delivered_then"] if not_sent else None,
                            sort=ended or a.asked_at or ""))
    return pending, history


def _batch_rows(reg: Registry, p: "rc.RaidEvent", where, now) -> list:
    out = []
    for i, d in enumerate(p.dms):
        kind = d.get("kind")
        if kind not in ("open", "reminder", "remind"):
            continue
        sent, failed = list(d.get("sent") or []), list(d.get("failed") or [])
        n = _people_words(len(sent))
        summary = f"{d['by']} sent it to {n}." if kind == "remind" and d.get("by") else f"Sent to {n}."
        out.append(_row(kind, f"dm:{p.key}:{i}", where, people=[_person(reg, u) for u in sent],
                        failed_people=[_person(reg, u) for u in failed], summary=summary,
                        failed_summary=f"Not delivered to {_people_words(len(failed))}." if failed else None,
                        times=[_time(reg, WORDS["sent"], d.get("at"), now)], tone="closed", sort=d.get("at") or ""))
    return out


def _callout_rows(reg: Registry, p: "rc.RaidEvent", where, now) -> list:
    out = []
    for i, c in enumerate(p.callouts):
        if c.source != "dm":  # the tab is the bot's DMs: a Can't make it pressed on a DM card; the rest are on Signups and the log
            continue
        h = round(float(c.hours_before))
        when = f"{h} h before the start" if h >= 0 else "after the start"
        after = bool(p.locked_at) and (c.at or "") >= (p.locked_at or "")
        out.append(_row("callout", f"callout:{p.key}:{c.discord_id}:{i}", where,
                        person=_person(reg, c.discord_id, name=c.display_name, character=c.character),
                        times=[_time(reg, WORDS["said"], c.at, now)], outcome=when + (", after the lock" if after else ""),
                        tone="no", detail=c.note or None, sort=c.at or ""))
    return out


def activity_json(reg: Registry, rs: "rc.RaidStore", ev: "rc.RaidEvent", now: datetime | None = None) -> dict:
    """BotActivity: {key (the first raid's), pending_count, paused [sentence], pending [row], history [row],
    pending_title, history_title, empty_pending, empty_history, unrecorded (a sentence | null), filters {kinds, people,
    all_kinds, everyone}, read_only}. A row: {id, kind, kind_label, where (the raid, and the roster — only on an event
    of several raids or rosters), person {uid, display_name, character, cls, spec, icon_title} | null, people / failed_people (a send to many, unfolded), summary,
    failed_summary, times [{label, when, rel}], outcome, tone (yes | no | waiting | closed), detail (the ask's reason,
    the callout's note), delivery, withdraw {path, uid, asked_at, label} | null, uids}. Pending: soonest deadline first;
    history: newest first. Any part's key answers as its event."""
    now = now or reg.now_local()
    first = rc.first_part(rs, ev)
    parts = rc.event_parts(rs, first)
    live = any(p.state not in CLOSED for p in parts)
    several = len(parts) > 1
    pending, history, paused = [], [], []
    for p in parts:
        name = reg.raid_def(p.instance).get("name", p.instance or p.team)  # the reader raid_views.raid_name uses
        where = name if several else None
        p_live = p.state not in CLOSED
        cp, ch = _confirm_rows(reg, p, where, p_live, now)
        ap, ah = _ask_rows(reg, p, where, p_live, now)
        pending += cp + ap
        history += ch + ah + _batch_rows(reg, p, where, now) + _callout_rows(reg, p, where, now)
        if p_live and p.state == "locked" and p.autofill_paused:
            line = WORDS["paused"] + (f" by {p.autofill_paused_by}" if p.autofill_paused_by else "") + "."
            paused.append(f"{name}: {line}" if several else line)
    pending.sort(key=lambda r: r["sort"])
    history.sort(key=lambda r: r["sort"], reverse=True)
    rows = pending + history
    kinds = [k for k in KIND_ORDER if any(r["kind"] == k for r in rows)]
    people: dict[str, str] = {}
    for r in rows:
        for x in ([r["person"]] if r["person"] else []) + r["people"] + r["failed_people"]:
            if x.get("uid"):
                people.setdefault(x["uid"], x["display_name"])
    for r in rows:
        r.pop("sort", None)
    return {"key": first.key, "pending_count": len(pending), "paused": paused, "pending": pending, "history": history,
            "pending_title": WORDS["pending_title"], "history_title": WORDS["history_title"],
            "empty_pending": WORDS["empty_pending"], "empty_history": WORDS["empty_history"],
            "unrecorded": None if all(p.dms_whole for p in parts) else WORDS["unrecorded"],
            "filters": {"kinds": [{"value": k, "label": KIND_WORDS[k]} for k in kinds],
                        "people": [{"value": u, "label": n} for u, n in sorted(people.items(), key=lambda x: x[1].lower())],
                        "all_kinds": WORDS["all_kinds"], "everyone": WORDS["everyone"]},
            "read_only": not live}
