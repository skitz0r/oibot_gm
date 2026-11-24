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


# the confirmation box's fixed words (design §7b, D18): the site's ConfirmBox and the leave guard print these, never their own
CONFIRM_WORDS = {"back": "Back", "keep": "Keep editing", "discard": "Discard edits", "leave_title": "Leave without saving?",
                 "changed": "This changed while the box was open. Read it again before you press."}

# a priority weight as a short phrase (rc.priority_words: "Regulars first, then rank")
PRIORITY_SHORT = {"regular": "Regulars", "rank": "rank", "main": "main characters", "sat_out": "who sat out last time",
                  "signup_order": "who signed up first"}

# the value words of the Events, New event, Schedules and Raids pages (phase 4, the approved mockups), ONCE here: the read
# models (event_view.py …) print them and /api/meta.page_words carries them, so a page file never holds a value word.
# `{…}` are filled by whoever prints them (the server, or the page with a count it already has).
PAGE_WORDS = {
    "one_off": "One-off, no schedule", "first_to_join": "First to join", "not_asked": "Not asked",
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
    "board_footer": "Placed as the lock would place them now. Drag a name to move it.",
    "show_switches": "Show {n} possible switches", "hide_switches": "Hide the {n} switches", "raid_short": "{raid} is short",
    # Role switches: each row's button (rc.event_switches `button`), and why a short raid has no switch to show (`why`)
    "switch_button": "Switch", "answer_button": "Answer for them", "ask_button": "Ask",
    "short_cover": "The only switches here need someone to cover the role they leave, so {screen} shows them.",
    "short_asked": "The asks already out cover it until they are answered.",
    "short_counted": "Regulars who haven't answered are counted as coming, so nothing is suggested until they answer.",
    "short_nobody": "Nobody free to come can play this role. A member who adds a character that can will show here.",
    # the Signup sheet fact while the sheet is not in Discord: no signup channel to post to, or not posted yet
    "sheet_waits": "Posts once a signup channel is set on the Config page's Channels tab", "sheet_not_posted": "Not in Discord yet",
    # the Board before the lock: an empty seat a minimum wants (beside its role icon), and the line over a picture that is
    # being placed again after a change (event_board: board_state "working")
    "open_seat": "open seat",
    # the Board's Not placed list before the lock: a name an officer drops there is kept off the roster (a pin), and a
    # drag back into a group lifts it (raidcycle.apply_move_shown)
    "not_placed_off": "A name dropped here stays off the roster when the sheet locks. Drag it into a group to put it back.",
    "board_working": "Placing everyone again after the last change. This is the board from before it.",
    # a board the bot could not work out at all (event_board: an unexpected failure, kept for those answers)
    "board_failed": "The bot couldn't work out where the lock would place everyone. The board shows the officers' moves.",
    # Schedules (schedule_view): a pickup's Repeats, a schedule with no run times, no reminder, the regulars' words and
    # the next four weeks, and the empty Next events of a paused schedule
    "by_an_officer": "Opened by an officer from the Events page", "no_times": "No run times yet. Edit the schedule to add them.",
    "no_reminder": "No reminder", "for_seats": "{n} for {seats} seats", "short_every_week": "Short every week",
    "ready_every_week": "Ready every week", "short_weeks": "Short {n} of the next four weeks",
    "paused_next": "Paused, so no event opens. Resume it to open events again.",
    # Raids (raid_page): the First opens value once the raid is open, its make-up with no minimum, a raid no schedule plays
    "open_now": "Open now", "no_minimums": "No minimums set. Edit the raid to add one.",
    "played_nowhere": "No schedule plays this raid yet. Add one on the Schedules page.",
    # Readiness' Standing when it is not "ready": the printed row that makes it so (raid_page.readiness_view)
    "standing_mains": "Fewer mains than seats.", "standing_under": "{slot} are under the minimum.",
}
