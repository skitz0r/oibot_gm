"""The member inbox and the officer's on-behalf union (dual interfaces phase 2 B, design §5.40) — shared by the Me page
(web/api_me.py), `/me view`, the SetWizard (`/raid set`), the site's row ⋮ menu and MCP.

- **Asks** (what someone must ACT on): every open Confirm / Can't ask (a placement ask on a locked run) and every open fill
  or pre-lock ask, wherever it waits (`channel` dm | web) — one shape (`asks_of`) for the Me page's AskCards and the
  officer's inbox view. Rendered from the record only; the card's answer goes to the verbs (`answer_placement_for`,
  `answer_fill_for`), never a second path.
- **Notices** (an outcome to KNOW, D26): Registry.notify writes them where the verbs used to send a courtesy DM (the DM
  rule); `notices_of` reads them for the Me page and `/me view`.
- **On behalf** (B5): rc.member_actions is the one list of what an officer may record for a member on a run; here
  `action_consequence` says what each does (truthfully: who is told, and how) and `do_member_action` calls the verb.

Never a DM from here. Nothing in this module talks to Discord except through the RaidMixin verbs it calls."""
from __future__ import annotations

from . import ops as ops_mod
from . import raidcycle as rc
from .registry import Member, Registry, RegistryError

# the one sentence for the DM toggle (design B2): the Me page's card, `/me view`, `/me dms`, the officers' DM column and the
# manual say exactly this — and it is true (no channel posts are promised)
DM_OFF_SENTENCE = "With DMs off, Confirm and fill asks wait on your Me page and the sheet; nothing is sent anywhere else."
DM_ON_SENTENCE = "With DMs on, Confirm and fill asks arrive as a DM with two buttons (they also show on your Me page)."
ASK_KIND_WORDS = {"confirm": "Confirm your seat", "fill": "A seat is open", "swap": "Could you switch?", "prelock": "Could you come?"}


def _run_of_roster(rs, team: str):
    """The live run a placement ask's roster key belongs to (soonest first), or None."""
    evs = sorted((e for e in rs.live() if e.team == team), key=lambda e: e.starts_at)
    return evs[0] if evs else None


def _seat_json(reg: Registry, p, me: bool) -> dict:
    return {"character": p.character or p.signup_name, "cls": p.cls, "spec": p.spec, "role": p.role, "me": me}


def group_preview(reg: Registry, ev, uid: int) -> dict | None:
    """The group `uid` is seated in, as the confirm DM shows it (one member per line, spec icon alone): {roster, group,
    members: [{character, cls, spec, role, me}]}; None when not seated."""
    hit = ev.seat_of(uid)
    if not hit:
        return None
    i, p = hit
    r = ev.all_rosters[i]
    by = {x.signup_name: x for x in r.selected}
    gi = next((k for k, g in enumerate(r.groups) if p.signup_name in g), None)
    names = r.groups[gi] if gi is not None else [p.signup_name]
    return {"roster": i + 1, "rosters": len(ev.all_rosters), "group": (gi or 0) + 1,
            "members": [_seat_json(reg, by[n], by[n] is p) for n in names if n in by]}


def asks_of(reg: Registry, rs, uid: int) -> list[dict]:
    """Every open ask `uid` holds on a live run, soonest run first — the Me page's AskCards (and the officers' inbox view):
    {id, kind confirm|fill|swap|prelock, title, run, roster, raid, when, character, cls, spec, role, as_label, group (the
    seat's group preview; confirm only), deadline (12-hour words or None), channel dm|web, held, waiting_for, reason}.
    `id` is what POST /api/me/ask/{id} (confirm) or /api/me/fill/{run} (the others) takes."""
    m = reg.members.get(int(uid))
    if m is None:
        return []
    out: list[dict] = []
    for a in reg.open_placement_asks(m.discord_id):
        ev = _run_of_roster(rs, a["roster"])
        if ev is None or ev.state != "locked":
            continue
        hit = ev.seat_of(m.discord_id)
        p = hit[1] if hit else None
        c = reg.find(a["character"])
        cls = p.cls if p else (c[1].cls if c else None)
        spec = p.spec if p else (c[1].spec if c else None)
        out.append({"id": ev.key, "kind": "confirm", "title": ASK_KIND_WORDS["confirm"], "run": ev.key, "roster": a["roster"],
                    "raid": reg.raid_def(ev.instance).get("name", ev.instance), "when": reg.local12(ev.start), "starts_at": ev.starts_at,
                    "character": a["character"], "cls": cls, "spec": spec, "role": p.role if p else None, "as_label": f"as {spec}" if spec else "",
                    "group": group_preview(reg, ev, m.discord_id), "deadline": reg.local12(ev.confirm_by) if ev.confirm_by else None,
                    "channel": a.get("channel", "dm"), "held": False, "waiting_for": None, "reason": None})
    for ev in rs.live():
        if ev.state not in ("open", "locked"):
            continue
        for a in ev.fill_asks:
            if a.discord_id != m.discord_id or not a.pending:
                continue
            kind = "prelock" if a.phase == "prelock" else ("swap" if a.swap else "fill")
            partner = rc.partner_of(ev, a) if a.held else None
            c = reg.find(a.character)
            out.append({"id": ev.key, "kind": kind, "title": ASK_KIND_WORDS[kind], "run": ev.key, "roster": ev.team,
                        "raid": reg.raid_def(ev.instance).get("name", ev.instance), "when": reg.local12(ev.start), "starts_at": ev.starts_at,
                        "character": a.character, "cls": c[1].cls if c else None, "spec": a.spec, "role": a.role, "as_label": f"as {a.spec}",
                        "group": None, "deadline": reg.local12(a.expires_at) if a.expires_at else None, "channel": a.channel,
                        "held": bool(a.held), "waiting_for": partner.display_name if partner else None, "reason": a.reason})
    return sorted(out, key=lambda x: x["starts_at"])


def notices_of(reg: Registry, uid: int, *, unread: bool = False) -> list[dict]:
    """The member's notices for a person to read: {id, kind, text, run, when (12-hour words), read}."""
    return [{"id": n["id"], "kind": n["kind"], "text": n["text"], "run": n.get("run"), "when": reg.local12(n["at"]) if n.get("at") else "",
             "read": bool(n.get("read_at"))} for n in reg.notices(uid, unread=unread)]


def inbox_lines(reg: Registry, rs, uid: int, limit: int = 6) -> list[str]:
    """`/me view`'s inbox: open asks first (what to press, where), then the unread notices — plain lines."""
    lines = []
    for a in asks_of(reg, rs, uid):
        where = "your DM or the Me page" if a["channel"] == "dm" else "the Me page"
        if a["held"]:
            lines.append(f"⏳ {a['raid']} {a['when']}: you said yes — waiting for {a['waiting_for'] or 'the other half'}")
        else:
            lines.append(f"❓ {a['title']}: {a['raid']} {a['when']} as {a['character']}" + (f" — by {a['deadline']}" if a["deadline"] else "") + f" ({where})")
    for n in notices_of(reg, uid, unread=True):
        lines.append(f"• {n['text']}")
    return lines[:limit] + ([f"-# +{len(lines) - limit} more on the Me page"] if len(lines) > limit else [])


# ---------------------------------------------------------------- on behalf (B5)

def _told(reg: Registry, ev, m: Member, card: bool) -> str:
    """How the member hears of an officer's write — truthfully (the DM rule: a notice, plus their open DM card edited)."""
    return f"{m.display_name} sees a notice on their Me page" + (" and their open DM card is edited" if card else "") + " — no new DM."


def _has_card(reg: Registry, ev, m: Member, key: str) -> bool:
    if key in ("confirm_yes", "confirm_no", "no_thanks", "callout"):
        ask = next((a for a in m.placement_asks if a["roster"] == ev.team and a.get("answer") is None), None)
        if ask and ask.get("dm"):
            return True
    if key in ("fill_yes", "fill_no", "no_thanks", "callout"):
        a = next((a for a in ev.fill_asks if a.discord_id == m.discord_id and a.open), None)
        if a and a.dm_message_id:
            return True
    return False


def action_consequence(reg: Registry, ev, m: Member, key: str, character: str | None = None, *, by_id: int | None = None) -> str:
    """What one on-behalf action does, from code (design §2.3) — the SetWizard's decide screen and the site's confirm print
    exactly this. Says who is told and how (a notice; their open DM card edited) — never more than happens. `by_id` = the
    officer recording it: on their OWN record the verbs treat it as their own answer and write no notice, so the
    sentence promises none."""
    run = rc.run_words(reg, ev)
    locked = ev.state == "locked"
    seated = bool(ev.seat_of(m)) if locked else False
    s = ev.signups.get(str(m.discord_id))
    char = character or (s.character if s else (m.main.label if m.main else "their main"))
    fill = rc.team_setting(rc.run_team(reg, ev), "autofill")
    own = by_id is not None and int(by_id) == m.discord_id
    tail = " Your own record: no notice is written." if own else " " + _told(reg, ev, m, _has_card(reg, ev, m, key))
    if key == "callout":
        if locked and seated:
            return f"{m.display_name} is recorded as a callout on {run} (hours before counted) and their seat is freed" + ("; the bench is asked." if fill else ".") + tail
        return f"{m.display_name} goes to No thanks on {run} and a callout is recorded (hours before counted; your note, if any, is for officers)." + tail
    if key == "no_thanks":
        if locked and seated:
            return f"{m.display_name} goes to No thanks on {run} — their seat is freed" + (" and the bench is asked" if fill else "") + "; no callout is recorded." + tail
        return f"{m.display_name} goes to No thanks on {run}; no callout is recorded." + tail
    if key == "join":
        if not locked:
            return f"{m.display_name} joins {run} as {char}; the lock decides the roster." + tail
        if seated:
            return f"{m.display_name} plays {char} on {run} and keeps the seat." + tail
        conf = " and asked to confirm" if rc.run_confirms(reg, ev) else ""
        return f"{m.display_name} joins {run} as {char}: rostered if a seat is free{conf}, else a Join the board can move in." + tail
    if key == "bench":
        return f"{m.display_name} goes to Bench on {run}" + (" — still rostered until set to No thanks." if seated else ".") + tail
    if key == "confirm_yes":
        return f"{m.display_name}'s seat on {run} is confirmed for them." + tail
    if key == "confirm_no":
        return f"{m.display_name} can't make {run}: their seat is freed" + (" and the bench is asked." if fill else ".") + tail
    if key == "fill_yes":
        return f"{m.display_name} takes the fill ask on {run} (seated the way they were asked, if the seat is still free)." + tail
    if key == "fill_no":
        return f"{m.display_name} declines the fill ask on {run}; the next person is asked." + tail
    if key in ("pin_in", "pin_out", "unpin"):
        what = {"pin_in": "is pinned to the roster", "pin_out": "is kept on the bench", "unpin": "is unpinned"}[key]
        return f"{m.display_name} {what} for the lock of {run}. Nobody is told (a pin is officer planning)."
    raise RegistryError(f"unknown action {key}")


ON_BEHALF_WORDS = {"join": "set you to Join", "bench": "set you to Bench", "no_thanks": "set you to No thanks", "callout": "recorded that you can't make it",
                   "confirm_yes": "confirmed your seat", "confirm_no": "answered Can't make it for you", "fill_yes": "said yes to your fill ask for you",
                   "fill_no": "said no to your fill ask for you"}


async def do_member_action(bot, reg: Registry, rs, ev, m: Member, key: str, *, character: str | None = None, note: str | None = None,
                           by: str, by_id: int | None, via: str = "discord") -> str:
    """Record one on-behalf action — the SetWizard's Confirm and POST /api/admin/action — through the SAME verbs every
    surface uses: set_answer (Join / Bench / No thanks: an answer change), call_out (Record a callout: the record + an
    optional note, before and after lock), answer_placement_for, answer_fill_for, pin_member. Refuses an action that no
    longer applies (rc.member_actions re-read). The verbs write the notice, edit cards and write the audit line."""
    if not rc.on_bench_side(reg, ev, m):  # a puppet on a real run, a real member on a test run: never
        raise RegistryError(f"{m.display_name} isn't on this run's side of the test bench — a test run takes its puppets and its tester only.")
    allowed = {a["key"] for a in rc.member_actions(reg, ev, m)}
    if key not in allowed:
        raise RegistryError(f"That no longer applies to {m.display_name} on {rc.run_words(reg, ev)} — look again.")
    if key in ("join", "bench", "no_thanks"):
        status = {"join": "in", "bench": "sub", "no_thanks": "out"}[key]
        line = await bot.set_answer(reg, rs, ev, m, status, character or None, by, by_id=by_id)
    elif key == "callout":
        line = await bot.call_out(reg, rs, ev, m, (note or "").strip()[:300] or None, by, by_id=by_id)
    elif key in ("confirm_yes", "confirm_no"):
        line = await bot.answer_placement_for(reg, m.discord_id, ev.team, key == "confirm_yes", by, by_id=by_id)
    elif key in ("fill_yes", "fill_no"):
        line = await bot.answer_fill_for(reg, rs, ev, m, key == "fill_yes", by, by_id=by_id)
    else:
        line = await pin_member(bot, reg, rs, ev, m, {"pin_in": "in", "pin_out": "out", "unpin": None}[key], by=by, by_id=by_id, via=via)
    return str(line).replace("**", "").removeprefix("✅ ")


# ---------------------------------------------------------------- pins (D16): ONE write for every surface

def pin_write(reg: Registry, rs, ev, m: Member, pin: str | None, by: str) -> str:
    """THE pin write: rc.set_pin (the log line) + one save. `pin` = "in" | "out" | None (clear). Refuses someone who
    isn't on the sheet. Offline callers (plain text's `apply` with no bot) stop here; everyone else calls `pin_member`,
    which also re-renders the sheet (its footer counts pins). Returns '<name>: pinned in' / '<name>: unpinned'."""
    s = ev.signups.get(str(m.discord_id))
    if s is None:
        raise RegistryError(f"{m.display_name} isn't on this sheet")
    try:
        rc.set_pin(ev, s.display_name, pin)
    except ValueError as e:
        raise RegistryError(str(e))
    rs.save(ev, f"pin {s.display_name} {pin or 'clear'} by {by}")
    return f"{s.display_name}: {'pinned ' + pin if pin else 'unpinned'}"


async def pin_member(bot, reg: Registry, rs, ev, m: Member, pin: str | None, *, by: str, by_id: int | None = None, via: str = "discord",
                     audit: bool = True) -> str:
    """Pin someone to the roster / keep them on the bench for the lock / clear it — the one path behind the SetWizard,
    the site's row menu (POST /api/admin/action), POST /api/run/{key}/pin (MCP `pin`) and plain text's `pin` op:
    pin_write, the audit line (`audit=False` when the caller writes it: configops.apply_async audits what it returns),
    then the Discord sheet re-rendered so its footer's 'Pinned: N · Bench-pinned: N' is current. Nobody is told."""
    line = pin_write(reg, rs, ev, m, pin, by)
    if audit and getattr(bot, "ops", None) is not None:
        await ops_mod.audited(bot.ops, reg, by, f"{ev.key} {line.replace(': ', ' ', 1)}", via=via, by_id=by_id)  # 'bd-… Kestrel pinned in'
    refresh = getattr(bot, "refresh_sheet", None)
    if refresh is not None:
        await refresh(reg, ev)
    return line
