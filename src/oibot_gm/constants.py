"""Shared literal values: one place for the role tuple, answer marks, level colours and the brand colour.
Values are copied from where they used to live (raidcycle / discord_raid / discord_pool / comp); don't change them
here without checking every site that renders them."""
from __future__ import annotations

ROLES = ("tank", "healer", "melee", "ranged")
RANK_ORDER = {"core": 0, "raider": 1, "trial": 2, "social": 3, "alt": 4}  # lower = seated first

# confirmation / fill-ask answer marks (None = still waiting)
MARK = {"yes": "✅", "no": "❌", "expired": "⌛", None: "⏳"}
# what each mark means, in the glossary's words — the site's tooltips and the confirmation tally (design §5.56 phase 2);
# /api/meta carries both maps ("waiting" = the None key), so the site draws the Discord card's marks, never its own
MARK_WORDS = {"yes": "confirmed", "no": "can't make it", "expired": "no answer in time", None: "waiting"}

# a raid's (or a schedule's regulars') coverage level as a word about the guild, never a colour alone: the Readiness and
# Rosters chips (meta.level_words)
LEVEL_WORDS = {"green": "ready", "amber": "thin", "red": "short"}

# health levels → embed / container accent colours and the dot used in text
LEVEL_COLOUR = {"green": 0x2E9E6B, "amber": 0xE0A448, "red": 0xC0392B}
LEVEL_DOT = {"green": "🟢", "amber": "🟡", "red": "🔴"}

TEAL = 0x2B7A78  # the bot's embed colour

# the one human-facing clock (the clock rule): 12-hour, no leading zero. Registry.local12 converts to the guild zone and
# formats with this; `fmt12` is the same formatting for a datetime already in the zone it should read in.
CLOCK12 = "%a %d %b %I:%M %p"


def fmt12(dt, fmt: str = CLOCK12) -> str:
    """'Tue 22 Sep 7:30 PM' / 'Wed 9 Dec 3:00 PM' for a datetime in its own zone: the hour's AND the day's leading zero
    stripped (a person says "the 9th", never "oh-nine"). The single implementation behind wizard.fmt12,
    raid_views.clock12, flows_config.fmt12, agents.clock12 and Registry.day_label; never `%H:%M` where a person reads."""
    import re

    return re.sub(r"\b0(\d) ([A-Z][a-z]{2})\b", r"\1 \2", re.sub(r"\b0(\d:\d\d [AP]M)", r"\1", dt.strftime(fmt)))


def hours_words(h) -> str:
    """48 → '2 days', 168 → 'a week', 24 → '24 hours', 1 → '1 hour' (flows_config re-exports it; registry's lift words)."""
    h = float(h)
    if h == 168:
        return "a week"
    if h >= 48 and h % 24 == 0:
        return f"{int(h // 24)} days"
    return f"{h:g} hour{'' if h == 1 else 's'}"


# the one line every prompt a person's words reach carries (phase 6, event words): people still say "run", "ad hoc" and
# "follow-on"; the model understands them and answers in the glossary's words (configops.SCHEMA_TEXT, help.SYSTEM, the
# listening gate's state in discord_bot)
OLD_WORDS_PROMPT = "run, ad hoc and follow-on are older words for event, one-off and a later raid; answer in the new words."

# the confirmation box's fixed words (design §7b, D18): the site's ConfirmBox and the leave guard print these, never their own
CONFIRM_WORDS = {"back": "Back", "keep": "Keep editing", "discard": "Discard edits", "leave_title": "Leave without saving?",
                 "changed": "This changed while the box was open. Read it again before you press."}

# a priority weight as a short phrase (rc.priority_words: "Regulars first, then rank")
PRIORITY_SHORT = {"regular": "Regulars", "rank": "rank", "main": "main characters", "sat_out": "who sat out last time",
                  "signup_order": "early signups"}  # never "who signed up first first"

# the value words of the Events, New event, Schedules and Raids pages (phase 4, the approved mockups), ONCE here: the read
# models (event_view.py …) print them and /api/meta.page_words carries them, so a page file never holds a value word.
# `{…}` are filled by whoever prints them (the server, or the page with a count it already has).
PAGE_WORDS = {
    "one_off": "One-off, no schedule", "first_to_join": "First to join", "not_asked": "Not asked",
    # an event's raid-night facts (event_view._night_facts) and the page's time zone line (event_list `tz_words`)
    "paused": "Paused", "paused_by": "Paused by {who}", "reminded_by": "{when}, by {who}", "away_reason": "{name} ({reason})",
    "confirm_pending": "Not sent yet. The bot sends them within a minute.", "times_in": "Times in {zone}.", "test_run": "test event",
    "asked_at_lock": "Asked at the lock", "no_sheet_yet": "no sheet yet", "from": "From {when}",
    "open_in_discord": "Open in Discord", "no_answer_yet": "no answer yet", "answers_one": "One for the whole evening",
    "bot_asks": "The bot asks people", "officer_asks": "An officer asks", "every_main": "Every main",
    "used_when": "More people join than there are seats", "one_off_rule": "First to join, whatever the priority",
    # the Priority bars, one per seat weight (the weights' keys)
    "priority": {"regular": "Regular", "rank": "Rank", "main": "Main character", "sat_out": "Sat out last time",
                 "signup_order": "Signed up early"},
    "rosters_of": "{n} of {size}", "placed_of": "{n} of {m} joined",
    # a locked or finished raid's board counts who holds a seat, not who joined (the lock may seat someone from the Bench)
    "seats_of": "{n} of {size} seats",
    # Undo my moves greyed out: the officer has moved nobody on this board (also the route's refusal)
    "no_moves": "You have no moves of your own on this board, so there is nothing to undo.",
    # the Board's Not placed / Not rostered list when it is empty (the Events page)
    "bank_empty": {"nobody": "Nobody has joined yet.", "placed": "Everyone who joined is placed.",
                   "rostered": "Everyone who joined is rostered."},
    # under the Board before the lock (the owner's tweaks C): the board is what the officers placed, the lock places the rest
    "board_footer": "The lock keeps these groups and fills any open seat from Not placed. Auto-fill groups places them now the way the lock would.",
    "show_switches": "Show {n} possible switches", "hide_switches": "Hide the {n} switches", "raid_short": "{raid} is short",
    # Role switches: each row's button (rc.event_switches `button`), and why a short raid has no switch to show (`why`)
    "switch_button": "Switch", "answer_button": "Answer for them", "ask_button": "Ask",
    # a Signups row's ⋮ item for that member's Role switches row: the row's own button word and what they would play
    "switch_item": "{button}: {to}",
    "short_cover": "The only switches here need someone to cover the role they leave, so {screen} shows them.",
    "short_asked": "The asks already out cover it until they are answered.",
    "short_counted": "Regulars who haven't answered are counted as coming, so nothing is suggested until they answer.",
    "short_nobody": "Nobody free to come can play this role. A member who adds a character that can will show here.",
    # the Signup sheet fact while the sheet is not in Discord: no signup channel to post to, or not posted yet
    "sheet_waits": "Posts once a signup channel is set on the Config page's Channels tab", "sheet_not_posted": "Not in Discord yet",
    # a locked Board's empty seat a minimum wants (beside its role icon)
    "open_seat": "open seat",
    # the Board's Not placed list before the lock (the owner's tweaks C): a name there is only not placed yet — the lock
    # places them — and one kept off through the ⋮ menu (an "out" pin) carries the word `kept_off` on its line
    "not_placed_note": "Nobody here is placed yet. The lock seats them where a seat is open, unless they are kept off.",
    "kept_off": "kept off",
    # Auto-fill groups (event_board.AutoFill): the button beside Not placed, its working line, what an MCP press that
    # waited is told, and each outcome that writes nothing (shown beside the button while the board is unchanged)
    "autofill_button": "Auto-fill groups",
    "autofill_working": "Filling the groups the way the lock would. The board updates when it is done.",
    "autofill_done": "The groups are filled the way the lock would fill them now.",
    "autofill_changed": "The board or the answers changed while the groups were being filled, so the board stays as it was.",
    "autofill_stopped": "A lock started while the groups were being filled, so the board stays as it was.",
    "autofill_stopped_preview": "A lock preview started on this event while the groups were being filled, so the board stays as it was.",
    "autofill_same": "The groups are already filled the way the lock would fill them, so nothing changed.",
    "autofill_busy": "The bot is building a roster right now, so the groups can't be filled until it is done.",
    "autofill_locked": "The roster is locked, so the board is the roster now.",
    "autofill_nobody": "Nobody has joined yet, so there is nobody to place.",
    # Schedules (schedule_view): a pickup's Repeats, a schedule with no run times, no reminder, the regulars' words and
    # the next four weeks, and the empty Next events of a paused schedule
    # what a schedule repeats (the schedule editor's Repeats choice on the site, flows_schedule's select in Discord)
    "kinds": {"weekly": "Weekly", "lockout": "Day N of each lockout", "pickup": "Opened by an officer"},
    # the New event page's and Edit lineup's section title, the one-off's confirmation heading in /raid open
    "what_this_makes": "What this makes",
    "by_an_officer": "An officer opens each event with /raid open in Discord", "no_times": "No event times yet. Edit the schedule to add them.",
    "no_reminder": "No reminder", "for_seats": "{n} for {seats} seats", "short_every_week": "Short every week",
    "ready_every_week": "Ready every week", "short_weeks": "Short {n} of the next four weeks",
    "paused_next": "Paused, so no event opens. Resume it to open events again.",
    # Raids (raid_page): the First opens value once the raid is open, its make-up with no minimum, a raid no schedule plays
    "open_now": "Open now", "no_minimums": "No minimums set. Edit the raid to add one.",
    "played_nowhere": "No schedule plays this raid yet. Add one on the Schedules page.",
    # Readiness' Standing when it is not "ready": the printed row that makes it so (raid_page.readiness_view)
    "standing_mains": "Fewer mains than seats.", "standing_under": "{slot} are under the minimum.",
    # the sentences the pages wrote themselves until the final review (the Copy rule: an empty list names what is
    # missing and how to fill it) — the Events, Schedules and Raids lists, New event and the schedule editor's hints,
    # an event's empty Activity, Re-post the sheet, a member menu with nothing to do, a priority left to the raid
    "events_empty": "No event yet. Make one with New event, or add a schedule on the Schedules page.",
    "schedules_empty": "No schedule yet. New schedule makes one, with what it repeats, its lineup and its sheet timing.",
    "raids_empty": "This game has no raids yet, so there is nothing to set up here.",
    "raid_missing": "That raid isn't in this game. Pick one from the list.",
    "new_event_no_lineup": "Add a raid to the lineup to see what this event makes.",
    "new_event_no_time": "Pick the day and the start time to see what this event makes.",
    "schedule_no_lineup": "Add a raid to the lineup to make the schedule.",
    "activity_empty": "Nothing has happened on this event yet. A line appears here each time someone answers or an officer acts.",
    "repost_sheet": "The live sheet moves to this channel. Its buttons and later updates follow it, and the old message says where it went.",
    "nothing_applies": "Nothing can be done for them on this event.",
    "weight_same": "Same as the raid ({n})",
    # a schedule's settings by name: the Schedules page's editor labels AND the Save box's "label: old → new" lines
    # (configops._schedule_part) — one word per setting, so the two never drift
    "schedule_fields": {"name": "Name", "kind": "Repeats", "slots": "Event times", "days": "Days of each lockout", "time": "Starts",
                        "lineup": "Lineup", "rosters": "Rosters", "active": "Active", "signup_lead_hours": "Sheet opens",
                        # the four switches read as what they do (the owner, 2026-11-24); each select beside one never
                        # repeats the switch's word ("Signup Reminder" · "Goes out")
                        "nudge": "Signup Reminder", "nudge_hours_before": "Goes out", "lock_hours_before": "Locks",
                        "confirm": "Lock Confirmation", "confirm_hours_before": "Confirm by",
                        "autofill": "Automated Fill Requests", "fill_ask_hours": "An ask waits",
                        "open_dm": "Sheet Opening DM", "split_policy": "When one night fills two rosters",
                        "weights": "Priority",
                        # an event's own (Edit event, the owner's tweaks B): first to join or by priority
                        "first_come": "Seat order"},
    # the (?) beside each switch of Sheet timing (the schedule editor and Edit event): exactly who gets what, when and
    # where — read from discord_raid.post_health / dm_on_open / lock_run / confirm_one / run_fill, raidcycle.fill_batch
    # (FILL_OVERASK one more than the shortfall, FILL_MAX_OPEN asks out at once) and expire_confirmation_asks
    "schedule_field_help": {
        "nudge": "When the reminder goes out, each regular with no answer gets a DM with Join, Bench and No thanks. "
                 "With no regulars yet, every member with no answer gets it, and nobody with DMs off is sent one. "
                 "One-off events remind nobody, and the officers' health card goes out then either way.",
        "confirm": "At the lock, everyone rostered on the event's first raid gets a DM to Confirm or say Can't make it. "
                   "With DMs off the ask waits on their Me page, and a seat unanswered by Confirm by is freed. "
                   "Someone who takes a seat by confirming a fill request isn't asked again. "
                   "With this off, the roster stands as locked and nobody is asked.",
        "autofill": "After the lock, the bot DMs players of each open seat's role, bench first, then anyone with no answer. "
                    "It asks one more person than there are open seats, up to three at once. "
                    "The first Confirm takes the seat, and Can't make it or no answer within An ask waits asks the next. "
                    "A role switch waits for an officer, and with this off officers fill seats from More › Fill seats.",
        "open_dm": "When the sheet opens, each regular gets a DM with their answer so far and the sheet's buttons. "
                   "The DM also says when the roster locks. "
                   "With no regulars yet, every member gets it, and nobody with DMs off is sent one.",
    },
    # the (?)'s name for a screen reader: "What Signup Reminder does"
    "help_about": "What {name} does",
    # Edit event's seating choice beside PAGE_WORDS["first_to_join"], and its one-line states: after the lock, and once the
    # event has started or is over (the page shows nothing to edit)
    "by_priority": "By priority",
    "edit_locked": "The roster is locked, so only the automatic asks and how long an ask waits can change.",
    "edit_over": "This event has started or is over, so its settings can't change.",
    # a raid's settings by name: the Raids page's Details labels and its Save box's lines
    "raid_fields": {"lockout_days": "Lockout", "duration_hours": "Length", "first_open": "First opens"},
    # how one night's joiners are split into two rosters (registry.SPLIT_POLICIES)
    "split": {"balanced": "Balanced", "first": "Raid one first", "rotation": "Rotation"},
    "on": "on", "off": "off", "raids_own": "the raid's", "suggested": "the game's suggestion",
    # a schedule's Regulars when its raid left the game (no seats to count against), and a night's absences
    "regulars_one": "1 regular", "regulars_n": "{n} regulars", "away_n": "{n} away",
    # the Me page reached from a finished or cancelled event's sheet title (its run hash matches no row)
    "me_event_over": "That event has finished or was cancelled, so My sheets has no row for it.",
    "me_event_over_tail": "Your loot and attendance are further down this page.",
    # the members' Events page (member_event_view.py, web/api_member_events.py): a key it doesn't list, an empty list,
    # the No thanks mark's tooltip (the sheet's ⚑), and the answer box for someone with no character yet
    "event_over_member": "That event has finished or was cancelled, so it is not on the Events list.",
    "member_events_empty": "No event is open for signup. An event shows here once its sheet opens in Discord.",
    "called_out": "Called out after answering",
    "answer_needs_character": "Register a character on the Me page to answer.",
    # the Signups tabs (components/events/SignupTabs.tsx: the officers' Events page and the members' event page), each
    # tab's label and the sentence an empty tab says; "none" is the officers' alone, "away" the members' alone
    "signup_tabs": {"in": {"label": "Joined", "empty": "Nobody has joined yet."},
                    "sub": {"label": "Bench", "empty": "Nobody is on the bench."},
                    "out": {"label": "No thanks", "empty": "Nobody said No thanks."},
                    "none": {"label": "No answer yet", "empty": "Everyone has answered."},
                    "away": {"label": "Away", "empty": "Nobody is away that day."}},
}


def schedule_line_label(setting: str) -> str | None:
    """A schedule setting's name on a Save / Edit box's 'label: old → new' line: the editor's label, except a select
    whose label leans on the switch beside it — 'Goes out' alone says nothing, so its line reads 'Signup Reminder goes
    out'. configops (the schedule's Save) and consequences.event_edit_lines (Edit event) both read it."""
    F = PAGE_WORDS["schedule_fields"]
    if setting == "nudge_hours_before":
        return f"{F['nudge']} {F['nudge_hours_before'].lower()}"
    return F.get(setting)
