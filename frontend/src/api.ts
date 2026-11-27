// Thin JSON client. The session cookie carries identity; the header marks requests as ours (CSRF guard on the server).
// Several guilds (design §5.32 "Web"): once the app has rendered a guild, every call names it (X-Oibot-Guild), so this
// tab stays on its guild even when another tab switches the browser's selection (the oibot_guild cookie).
let guild: string | null = null;
let guildName: string | null = null;
const PREV_KEY = "oibot_prev_guild";

/** The guild this tab shows (from /api/meta), or null before meta has loaded. */
export function setGuild(key: string | null, name: string | null = null) { guild = key; guildName = name; }
/** "?g=<key>" for URLs the browser fetches on its own (images, EventSource): they can't carry the header. */
export function guildQuery(sep: "?" | "&" = "?"): string { return guild ? `${sep}g=${encodeURIComponent(guild)}` : ""; }
/** The guild a tab lost access to mid-session (set just before the reload that lands on another), read once. */
export function takePrevGuild(): { key: string; name: string } | null {
  try { const raw = sessionStorage.getItem(PREV_KEY); sessionStorage.removeItem(PREV_KEY); return raw ? JSON.parse(raw) : null; } catch { return null; }
}
export class ApiError extends Error {
  status: number;
  body?: unknown;  // the server's JSON on a refusal (a stale board write carries the current board)
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "oibot", ...(guild ? { "X-Oibot-Guild": guild } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (r.status === 401) {
    window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
    throw new ApiError(401, "login required");
  }
  const data = await r.json().catch(() => ({}));
  const nm = data as { not_member?: boolean; guilds?: string[] };
  if (r.status === 403 && nm.not_member && guild && (nm.guilds || []).length) {
    // this tab's guild is no longer one this person may open, but another is: drop the header and reload onto the
    // browser's selection (or the first guild they may open); the app then says which guild it left (K16)
    try { sessionStorage.setItem(PREV_KEY, JSON.stringify({ key: guild, name: guildName || guild })); } catch { /* private window: no notice */ }
    setGuild(null);
    window.location.reload();
    throw new ApiError(403, "switching server");
  }
  if (!r.ok) { const err = new ApiError(r.status, (data as { error?: string }).error || r.statusText); err.body = data; throw err; }
  return data as T;
}

/** POST a JSON body and save the file the server answers (the leveling guide's re-issued import string): the same
 *  headers as every call; a refusal reads the server's JSON error like `call`. Nothing is kept: the blob is handed to the
 *  browser's download and its URL revoked. */
async function download(path: string, body: unknown, fallbackName: string): Promise<string> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "oibot", ...(guild ? { "X-Oibot-Guild": guild } : {}) },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new ApiError(r.status, (data as { error?: string }).error || r.statusText);
  }
  const cd = r.headers.get("Content-Disposition") || "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const plain = /filename="([^"]+)"/i.exec(cd);
  const name = star ? decodeURIComponent(star[1]) : plain ? plain[1] : fallbackName;
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return name;
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => call<T>("PUT", path, body),  // the Policy page's Save draft (PUT /api/policy/{doc})
  download: (path: string, body: unknown, fallbackName: string) => download(path, body, fallbackName),
};

// ---- shapes (mirror src/oibot_gm/web/api.py)
/** A guild this person may open: the server selector's rows (role = theirs there). */
export interface GuildChoice { key: string; name: string; icon: string | null; role: "owner" | "officer" | "member" }
export interface Meta {
  guild: string; tz: string;
  /** the selected guild's key; every guild this person may open; `moved` = the guild they had selected and lost; the game version's label */
  guild_key: string; guilds: GuildChoice[]; moved: string | null; profile: string; game: string; classes: Record<string, Record<string, string>>; icons: { classes: Record<string, string>; roles: Record<string, string>; specs: Record<string, string> };
  raids: { id: string; name: string; size: number; lockout_days: number }[];
  viewer: { uid: string; name: string; officer: boolean; owner: boolean };
  labels: Record<string, string>;
  /** answer statuses in order (in, sub, out), split philosophies, role order, per-group role caps, class colours, bot start (epoch s) */
  statuses: string[]; split_policies: string[]; roles: string[]; group_caps: Record<string, number>; class_colours: Record<string, string>; started_at: number;
  /** absences: the Discord wizard's For how long list (presets.DURATIONS) and the verb's caps; the member ranks (D25: per member, on the main) */
  durations?: { days: number; label: string }[]; max_absence_days?: number; absence_reason_max?: number; ranks?: string[];
  /** the DM toggle's one sentence each way (inbox.DM_ON_SENTENCE / DM_OFF_SENTENCE) */
  dm_sentences?: { on: string; off: string };
  /** Settings edit mode: every timezone grouped by region (Mantine grouped Select data — picked, never typed) and the about text's cap */
  timezones?: { group: string; items: { value: string; label: string }[] }[]; about_max?: number;
  /** the raid wizard's cadence presets (presets.PRESETS): the schedule editor's quick picks */
  cadence_presets?: { key: string; name: string; signup_lead_hours: number; nudge_hours_before: number; lock_hours_before: number; confirm_hours_before: number }[];
  /** §2.9: Discord reachable for this guild (channel / role editing waits while not); `stale` = this viewer's membership is the
   *  last-known "yes" (reads go on, officer writes refuse) */
  discord?: { ready: boolean; stale: boolean };
  /** §5.55: the game version has the leveling-guide tool's tables (profiles/<version>/guides.yaml) — the Me page shows its Leveling guide tab */
  guides?: boolean;
  /** armory link §5.1: the game version has an armory and the host its Blizzard keys — Config › Settings shows the guild card */
  armory?: boolean;
  /** §5.60: the version's professions (empty list = none: render nothing), the skill cap and how many trades (crafting +
   *  gathering) a character may hold */
  professions?: { list: { id: string; name: string; group: string; icon: string; makes: boolean }[]; skill_cap: number; trade_slots: number;
    /** §5.64: recipes are picked from the game's list (the editor searches it; `makes` then comes from the list — R7) */
    recipe_list?: boolean };
  /** auction prices (web/api_prices.prices_meta): `on` = an auction house is picked; `state` off | waiting | fresh | stale |
   *  old; `words` = how recent the prices are for this reader (the owner, while off: how to turn them on; others: ""),
   *  `market` = the version has auction houses and the host its keys (Config shows the auction house card) */
  prices?: PricesMeta;
  /** the item page template with `{id}` (the profile's item link template); null = no item links (components/ItemName) */
  item_url?: string | null;
  /** The run's vocabulary, defined once in Python (design §5.56 phase 2) — a page never keeps its own copy: the state words
   *  (open · locked · done · cancelled), the run verbs' labels (raid_buttons.RUN_WORDS — the Discord buttons' own), the
   *  confirmation marks with their words (constants.MARK / MARK_WORDS; "waiting" = no answer yet), how an ask reaches
   *  someone (rc.ASK_HOW) and its answers, the coverage levels' words, and the run's sentences. */
  run_states: Record<string, string>;
  run_labels: Record<string, string>;
  marks: Record<string, { mark: string; words: string }>;
  ask_kinds: Record<string, string>;
  ask_answers: Record<string, string>;
  level_words: Record<string, string>;
  run_words: { no_confirmations: string; first_come: string; bank: Record<string, string>; not_answered: Record<string, string>; auto_asks_paused: string; pins: Record<string, string>; leftovers: string };
  /** phase 4's pages: their value words (constants.PAGE_WORDS), the confirmation box's fixed words (CONFIRM_WORDS) and the
   *  leave guard's sentence templates ({name}, {n}) — a page prints these, never its own copy */
  page_words: PageWords;
  /** `refused` = the title of a refusal the server sent no box for (consequences.REFUSAL_TITLES["other"]) */
  confirm_words: { back: string; keep: string; discard: string; leave_title: string; changed: string; refused: string };
  leave: { one: string; many: string; page: string };
  /** registry.RESERVED_SCHEDULE_IDS: the ids no new schedule takes (a schedule editor never holds its own copy) */
  reserved_schedule_ids?: string[];
}
/** constants.PAGE_WORDS: `{…}` are filled by whoever prints them. */
export interface PageWords {
  schedule_label: string; one_off: string; first_to_join: string; not_asked: string; asked_at_lock: string; not_asked_at_lock: string; no_sheet_yet: string; from: string;
  open_in_discord: string; no_answer_yet: string; answers_one: string; bot_asks: string; officer_asks: string; every_main: string;
  used_when: string; one_off_rule: string; priority: Record<"regular" | "rank" | "main" | "sat_out" | "signup_order", string>;
  rosters_of: string; placed_of: string; placed_of_bench: string; board_footer: string; show_switches: string; hide_switches: string; raid_short: string; switch_item: string;
  sheet_waits: string; sheet_not_posted: string; open_seat: string; kept_off: string; seats_of: string; no_moves: string;
  bank_empty: Record<"nobody" | "placed" | "rostered", string>;
  /** Auto-fill groups (event_board.AutoFill): the button, its working line and its outcomes */
  autofill_button: string; autofill_working: string; autofill_done: string; autofill_changed: string; autofill_stopped: string;
  autofill_busy: string; autofill_locked: string; autofill_nobody: string; autofill_stopped_preview: string; autofill_same: string;
  /** a schedule's Repeats choice (the site's segmented control, Discord's select) and the lineup pages' section title */
  kinds: Record<"weekly" | "lockout" | "pickup", string>; what_this_makes: string;
  by_an_officer: string; no_times: string; no_reminder: string; for_seats: string; short_every_week: string;
  ready_every_week: string; short_weeks: string; paused_next: string; open_now: string; no_minimums: string; played_nowhere: string;
  /** a setting's name: the schedule editor's labels and the Save box's lines (configops) say the same */
  schedule_fields: Record<"name" | "kind" | "slots" | "days" | "time" | "lineup" | "rosters" | "active" | "signup_lead_hours" | "nudge"
    | "nudge_hours_before" | "lock_hours_before" | "confirm" | "confirm_hours_before" | "autofill" | "fill_ask_hours" | "open_dm"
    | "split_policy" | "weights" | "first_come" | "auto_lock" | "auto_lock_fill", string>;
  /** the (?) beside each Sheet timing switch: exactly who gets what, when and where */
  schedule_field_help: Record<"nudge" | "confirm" | "autofill" | "open_dm" | "auto_lock" | "auto_lock_fill", string>; help_about: string;
  /** Auto Lock's On / Off, and the Details Locks row when Auto Lock is off (issue 14) */
  switch_on: string; switch_off: string; officer_locks: string;
  raid_fields: Record<string, string>; split: Record<string, string>; on: string; off: string; raids_own: string; suggested: string;
  regulars_one: string; regulars_n: string; away_n: string;
  me_event_over: string; me_event_over_tail: string;
  /** the empty lists, hints and lines the pages wrote themselves until the final review */
  events_empty: string; schedules_empty: string; raids_empty: string; raid_missing: string; new_event_no_lineup: string;
  new_event_no_time: string; schedule_no_lineup: string; activity_empty: string; repost_sheet: string; nothing_applies: string;
  weight_same: string;
  /** Edit event (the owner's tweaks, B): the seating choice beside first_to_join, and the editor's one-line states */
  by_priority: string; edit_locked: string; edit_over: string;
  /** the members' Events page (member_event_view.py): a key it doesn't list, an empty list, the ⚑ tooltip, no character yet */
  event_over_member: string; member_events_empty: string; called_out: string; answer_needs_character: string;
  /** the Signups tabs (SignupTabs): each tab's label and the sentence its empty list says */
  signup_tabs: Record<SignupTab, { label: string; empty: string }>;
}
/** A Signups tab: Joined, Bench, No thanks, No answer yet (officers), Away (members). */
export type SignupTab = "in" | "sub" | "out" | "none" | "away";
/** One member on the members' event page's signup tabs (member_event_view): who plays what, and the ⚑ on No thanks. */
export interface MemberPerson { display_name: string; character: string; cls: string; spec: string; callout?: boolean }
/** One seat on a locked roster as members see it; `answer` = the confirmation (only when the part shows marks). */
export interface MemberSeat { display_name: string; character: string; cls: string; spec: string; role: string; answer?: string | null }
/** One raid of an event, locked or done, as members see it: its rosters and the joiners the lock left out. */
export interface MemberPart { key: string; raid_name: string; label: string; rosters: { n: number; groups: MemberSeat[][] }[]; not_rostered: string[]; marks: boolean }
/** One live event on the members' Events list (GET /api/me/events): `answer` = the viewer's own (rc.answer_word). */
export interface MemberEventRow { key: string; title: string; subtitle: string; state: string; chip: string; answer: string }
/** The members' Events list: Open (live events) and Coming up (nights with no sheet yet — plain text, no page). */
export interface MemberEventList { open: MemberEventRow[]; coming: { title: string; subtitle: string }[]; tz_words: string; empty: string }
/** One event as a member sees it (GET /api/me/event/{key}): the sheet's public facts, the signups by answer, the locked
 *  rosters, and the viewer's own answer rows (`answers`, one per live raid), open asks and characters. */
export interface MemberEventView {
  key: string; title: string; subtitle: string; state: string; chip: string; test_chip: string | null; details: Fact[];
  signups: { counts: Record<"in" | "sub" | "out" | "away", number>; rows: Record<"in" | "sub" | "out" | "away", MemberPerson[]> };
  rosters: MemberPart[]; part_keys: string[]; answers: MySheet[]; asks: MyAsk[]; characters: { label: string; is_main: boolean }[];
}
/** The confirmation box (design §7b, consequences.box): printed as it comes, Back then the verb; `verb` null = a refusal
 *  (only Back); `fp` goes back as `seen` with the press. */
/** `choice`: the lock box's one choice (consequences.lock_box, issue 14) — its label and whether it is ticked; absent elsewhere */
export interface Box { title: string; sentences: string[]; verb: string | null; danger: boolean; diff: string[]; fp: string; choice?: { label: string; value: boolean } }
/** A Details line (event_view): the label, its value in words, and where it links (a site path or a Discord jump link).
 *  `links` = several places at once (the Raids page's Played in: each schedule with its href); `level` = a chip's level. */
/** a Details row; `help` = the editors' (?) for the setting it names (constants.field_help: key, the sentence, its screen-reader name) */
export interface Fact { label: string; value: string; href?: string; links?: { name: string; href: string }[]; level?: string;
  help?: { key: string; text: string; about: string } }
/** A row of the Schedules list (GET /api/schedules): `repeats` = when its events start, in words. */
export interface ScheduleRow { id: string; name: string; repeats: string }
/** A removed schedule's leftover regular (schedule_view.orphans): `key` = the series key the Remove sends back, `label`
 *  its words (never the key), `box` Remove's box, `rev` the record's. */
export interface OrphanRegular { key: string; label: string; uid: string; display_name: string; character: string; cls: string; spec: string; box: Box; rev: string }
/** What the schedule editor picks from (schedule_view.editor_options): the palette (each raid's lockout, its own weights
 *  and the game's suggested timing), a pickup's, Copy a lineup, the lineup limit, the sheet-timing choices in words
 *  ("5 days before"), the presets, the split policies and the highest weight. */
export interface ScheduleEditorOptions {
  raids: { id: string; name: string; size: number; hours: number; lockout_days: number; weights: Record<string, number>; timing: Record<string, number | boolean> }[];
  adhoc: { timing: Record<string, number | boolean>; weights: Record<string, number> };
  lineups: { id: string; name: string; lineup: { raid: string; rosters: number; starts: number }[] }[];
  lineup_max: number; lineup_full: string;
  hours: Record<string, { value: number; words: string }[]>;
  presets: { name: string; values: Record<string, number> }[];
  split_policies: string[]; max_weight: number;
}
export interface ScheduleList { active: ScheduleRow[]; paused: ScheduleRow[]; reserved_ids: string[]; orphans: OrphanRegular[]; editor: ScheduleEditorOptions }
/** GET /api/schedules/{id} (schedule_view.schedule_json). `next` is empty while paused or gone (`next_empty` says why);
 *  `regulars` null for a pickup (first come); `stored` = the schedule in machine form — the editor sends it back whole as
 *  a `schedule` change (an unchanged one is no edit) with `rev` as revs["config:schedule:<id>"] (a stale one → 409). */
export interface ScheduleView {
  id: string; name: string; active: boolean; chip: string; kind: string; gone: string | null;
  details: Fact[];
  lineup: { raid: string; name: string; rosters: number; starts: number; time_words: string; seats: number | null }[];
  timing: Fact[];
  next: { starts: string; opens: string; locks: string; start_iso: string; occurrence: string; key: string | null }[]; next_empty: string | null;
  regulars: {
    count: number; words: string; expected?: string; short: Record<string, number> | null; outlook?: string;
    /** the next four weeks, a row per night (`can_open`: Open this run early works now; `why` the server's sentence when not) */
    forecast?: { day: string; when: string; short: Record<string, number>; away: string[]; away_words: string | null; expected: number; start_iso: string; occurrence: string;
      key: string | null; can_open: boolean; why: string | null }[];
    /** `box` = Remove's box (POST /api/admin/raid/regulars {schedule, uid, on: false, rev, seen: box.fp}) */
    rows: { uid: string; display_name: string; character: string; cls: string; spec: string; box: Box; rev: string }[];
  } | null;
  people: { uid: string; display_name: string; characters: { label: string; cls: string; spec: string; main: boolean }[] }[];
  boxes: { pause?: Box; resume?: Box; remove: Box };
  stored: Record<string, unknown> & { lineup: { raid: string; rosters: number; starts?: number }[] };
  rev: string;
}
/** /api/raids rows' `page` (raid_page.raid_row): Rules, Priority and the Reset box in words. `make_up` = Registry.minimums
 *  alone (one answer with Readiness). */
export interface RaidPage {
  details: Fact[]; make_up: { slot: string; label: string; min: number; note: string | null; own: boolean }[]; make_up_empty: string | null;
  priority: { key: string; label: string; value: number }[]; used_when: string; one_off_rule: string; open_events: number;
  boxes: { reset: Box };
}
/** /api/readiness rows' `view` (raid_page.readiness_view): the Raids page's Readiness — the same make-up rows as the Rules
 *  view with how many mains the guild has for each. */
export interface ReadinessView {
  who_could_play: Fact[]; make_up: { slot: string; label: string; have: number; min: number; own: boolean; level: string }[]; make_up_empty: string | null;
  /** Standing as the page prints it (raid_page.standing) and the sentence naming why — null when ready */
  level: string; standing_why: string | null;
}
/** A row of the Events list (GET /api/event-list): `key` = the event's first raid, `title` its start in words. */
export interface EventRow { key: string; title: string; subtitle: string; state: "open" | "locked" | "done" | "cancelled"; chip: string }
/** A schedule's night with no event yet: found again by (schedule, occurrence). */
export interface ComingRow { schedule: string; occurrence: string; title: string; subtitle: string }
export interface EventList { open: EventRow[]; coming: ComingRow[]; history: EventRow[]; history_more: boolean; /** "Times in Berlin." — the guild's time zone, once on the page */ tz_words: string }
/** One raid of an event on the Board (event_view._part_view); `sheet` = the run's full JSON, BoardView's input. */
export interface PartView {
  key: string; raid: string; raid_name: string; label: string; rosters_words: string;
  /** Placed before the lock ("18 of 18 joined"); a locked or finished raid sends `rostered_words` ("15 of 20 seats") instead */
  placed_words: string | null; rostered_words: string | null;
  /** why Undo my moves is greyed (the officer moved nobody here), the server's sentence; null with an undo box or once locked */
  undo_why: string | null;
  short: Record<string, number>; sheet: Sheet | null;
  /** open = the officers' board before the lock (nothing solved); locked = the roster as it stands (also a finished raid) */
  board_state: "open" | "locked";
  /** Auto-fill groups beside Not placed (open raids; null once locked): working while it runs (the server's words), else the
   *  sentence of an auto-fill that wrote nothing (`failed`), shown while the board is the one it was about */
  autofill: { working: boolean; words: string | null; failed: boolean } | null;
  mine: string[]; undo_box: Box | null;
  /** the officers' own placements (open raids; null once locked). Undo my moves is POST /api/run/{key}/layout {undo, seen}. */
  layout: string[][] | null;
  /** the split strategy this raid uses now (rc.run_strategy); null once locked — More › Split strategy… */
  strategy: string | null;
}
export interface EventPerson { uid: string; display_name: string; character: string; cls: string; spec: string }
/** GET /api/event/{key} (any raid's key answers as its event). */
export interface EventView {
  key: string; title: string; state: EventRow["state"]; chip: string; kind: "one_off" | "scheduled"; schedule: { id: string; name: string } | null;
  details: Fact[];
  signups: { joined: number; bench: number; no_thanks: number; no_answer: number; rows: { in: EventPerson[]; sub: EventPerson[]; out: EventPerson[]; none: EventPerson[] } };
  parts: PartView[]; activity: { when: string; line: string }[];
  /** `reschedule` = the box Reschedule opens on before anything is picked (consequences.reschedule_pick_box) */
  boxes: { lock: Box | null; cancel: Box | null; reschedule: Box | null };
  actions: { reschedule: boolean; edit: boolean; edit_lineup: boolean; lock: boolean; cancel: boolean; open_now: boolean;
    /** More › Clear the board: a raid of the event is still open (its box from POST /api/event/{key}/clear) */
    clear: boolean };
  lineup: { raid: string; rosters: number; starts: number }[]; start_iso: string;
  /** the event's own sheet timing by setting (rc.EVENT_TIMING), what POST /api/run/{key}/reschedule takes back */
  timing: Record<string, number | boolean>;
  people: { uid: string; display_name: string; characters: { label: string; cls: string; spec: string; role: string; is_main: boolean }[] }[];
  sheet_url: string | null;
  /** Reschedule's picker: the GUILD's date (the New day's lower bound) and the Locks choices, the event's own among them */
  today: string; locks: { hours: number; words: string }[];
  /** the start's day ('YYYY-MM-DD') and clock ('HH:MM') in the guild's CURRENT zone (Reschedule's picker starts there) */
  start_day: string; start_time: string;
  /** each answered member's characters (the row ⋮'s answer box) */
  characters: Record<string, Person["characters"]>;
  /** the Role switches section (rc.event_switches): drawn only when `short` has a raid */
  switches: Switches;
  /** the server's "test run" marker beside the state chip on a rehearsal, else null */
  test_chip: string | null;
  /** Split strategy…'s choices in the server's words */
  strategies: { id: string; label: string; words: string }[];
  /** Edit event (event_view.edit_options), null once it has started or is over: the settings it may change NOW (every
   *  one before the lock, the automatic asks and an ask's wait after it), its own values (rc.run_timing), each raid's
   *  split, the schedule editor's hour choices (its own value among them), the highest weight and the split policies */
  edit: EventEdit | null;
}
export interface EventEdit {
  fields: string[]; values: Record<string, number | boolean | Record<string, number>>;
  splits: { key: string; raid: string; value: string }[]; hours: Record<string, { value: number; words: string }[]>;
  max_weight: number; split_policies: string[];
}
/** One move a short raid could take (rc.event_switches): `from` / `to` in words ('Shadow', 'Nyx, Shadow', 'no answer yet'),
 *  `button` the server's word (Switch · Answer for them · Ask), `raids` the raids it helps, `box` its confirmation box;
 *  part / seat / option / character / spec go back with POST /api/run/{part}/switch. */
export interface SwitchRow {
  uid: string; display_name: string; role: string; from: string; to: string; answered: boolean; button: string; raids: string[];
  part: string; seat: number; option: number; character: string; spec: string; box: Box;
}
/** `short` = each raid short of a minimum (role → how many), `why` the sentence when it has no row; `revs` = each raid's plan rev. */
export interface Switches { short: { raid: string; roles: Record<string, number>; why: string | null }[]; rows: SwitchRow[]; revs: Record<string, string> }
/** GET /api/event/coming/{sid}/{occurrence}: `open` is what Open the sheet now carries back; `box` null while it can't. */
export interface ComingView {
  title: string; chip: string; schedule: { id: string; name: string }; details: Fact[]; can_open: boolean; why: string | null;
  open: { start_iso: string; schedule: string; occurrence: string; raid: string }; box: Box | null;
}
/** The Me page's Leveling guide tab (GET /api/me/guide, design §5.55): what the member has built from their own RestedXP
 *  import string. `files` = the downloads in order with their words; `groups` = one row per guide group; `built_when` is
 *  words (the clock rule); `starter_pack` = the host holds the addon zip, so the explorer covers the free 1–20 too. */
export interface GuideStatus {
  available: boolean; built: boolean; files: { name: string; words: string }[]; starter_pack: boolean;
  built_when?: string; guides?: number; steps?: number; starter?: number; range?: string; sizes?: Record<string, number>;
  groups?: { group: string; guides: number; steps: number; lo: number; hi: number; zones: string[]; dungeons: string[]; km: number; starter: boolean }[];
}
/** A recipe on a character's profession (design §5.60, §5.64): `id` = the game's recipe id (with the game's list); null =
 *  typed before the list and not on it — then `unmatched: true`, which a save sends back untouched. */
export interface Recipe { name: string; id: number | null; unmatched?: boolean }
/** One profession on a character: a profile profession id, its skill (1..meta.professions.skill_cap; null = not said yet),
 *  recipes. */
export interface Profession { id: string; skill: number | null; recipes: Recipe[];
  /** read by the server from the recipes the character knows (Tribal, Gnomish…); null = none shown */
  specialization?: string | null }
/** The Professions page (GET /api/professions, design §5.60): one crafter = a character holding a profession; `cls` = its
 *  class (icon and colour), `member` = the display name (a name, never a ping). */
export interface Crafter { label: string; member: string; uid: string; skill: number | null; profession: string; cls: string | null; specialization?: string | null }
/** A recipe and its makers (highest skill first; none = nobody makes it yet, then `nobody` is the server's sentence).
 *  `listed` = on the game's list: `category`, what it `makes` (null for an enchant) and its `reagents`; a recipe typed
 *  before the list that the list lacks has null / [] there. */
export interface CatalogRecipe {
  key: string; name: string; profession: string; id: number | null; crafters: Crafter[];
  listed?: boolean; category?: string | null; makes?: { id: number; name: string; quantity: number; quantity_max?: number } | null;
  reagents?: { id: number; name: string; quantity: number }[]; nobody?: string;
  /** the cost to make at the auction house's lowest prices (web/api_prices.cost_json); null without prices or reagents */
  cost?: RecipeCost | null;
}
export interface PricesMeta { on: boolean; state: "off" | "waiting" | "fresh" | "stale" | "old"; words: string; market: boolean }
/** A recipe's cost to make, all words from the server: one line per reagent (`unit_words` = its lowest price or "none listed",
 *  `total_words` = × the quantity, null when none listed), `sentence` = "Cost to make: …", `made_words` = what a craft makes
 *  and its own lowest price ("" for an enchant). */
export interface RecipeCost {
  lines: { id: number; name: string; quantity: number; unit_words: string; total_words: string | null }[];
  sentence: string; made_words: string;
}
/** GET /api/prices/find: at most 25 items; `total` = every match; `words` = how recent the prices are; `more` = how many more
 *  match, as a sentence ("" when none are left out); `message` = the whole answer in lines (its last line the empty answer). */
export interface PriceFind {
  q: string; rows: PriceRow[]; total: number; words: string; more: string; message: string;
}
/** One item's price (web/api_prices): `low_words` = its lowest price or "none listed", `qty` = how many are listed, `words` =
 *  the whole line; `watching` = on the viewer's own watch list (the search's rows only). While prices are hidden a watch
 *  list row carries its name alone (empty `low_words` and `words`). */
export interface PriceRow { id: number; name: string; low_words: string; qty: number; words: string; watching?: boolean; spark?: PriceSpark | null; group?: string }
/** A watch list row's small line (prices.spark): the last 24 hours (less while the history is younger) as [epoch seconds, lowest in copper | null = nothing
 *  listed or not read from then] over `since` .. `until` — positions only; null = nothing to draw. */
export interface PriceSpark {
  since: number; until: number; points: [number, number | null][];
  /** the price now against the first price of the stretch ('+12%' / '−8%' / '0%'; "" = nothing listed now); `up` = the
   *  line's colour: true risen, false fallen, null neither */
  change_words: string; up: boolean | null;
}
/** GET /api/prices/watching (price watch §4.2): the viewer's own list, newest first; `shown` = prices show now (else names
 *  alone); `empty` = the empty list's sentence; `registered` = the viewer has a record (watching needs one, C13). */
export interface WatchList {
  rows: PriceRow[]; words: string; shown: boolean; max: number; empty: string; registered: boolean;
  /** when the bot last checked the auction house and when it checks next ("" while no prices show), and in how many
   *  seconds to ask again (a little after that check) */
  checked?: string; again_in?: number | null;
  /** the list's choices, Movers' own: how far back each row's small line and change go, and the item groups (a row's `group`) */
  window?: string; windows?: { id: string; label: string }[]; groups?: { id: string; name: string }[];
  /** the viewer's own choice: their items count on the guild's list (POST /api/prices/watch/share {on}) */
  shared?: boolean;
}
/** GET /api/prices/guild-watching: the items the guild's members watch, the most watched first — a count per item
 *  (`watchers`, `watchers_words` "3 members watching"), never who. */
export interface GuildWatch { rows: (PriceRow & { watchers: number; watchers_words: string })[]; window: string; shown: boolean; empty: string }
/** GET /api/prices/history (price watch §4.3, C5): a watched item over one span. `t` = epoch seconds (the x position only),
 *  `when` = the server's words for the point (a time, or a day alone for a folded day), `low` / `high` in copper (low null =
 *  nothing listed from then: a gap; high > low = a folded day's range), `words` = the lowest in words; the span's lowest and
 *  highest as numbers and words; `sentences` = the 7- and 30-day sentences (and since when the bot has kept prices). */
export interface PriceHistory {
  id: number; name: string; spans: { id: string; label: string }[]; span: string;
  /** `unread` = the bot didn't read the house from `t` (low / high null, words "not read"): a break, as nothing listed */
  points: { t: number; when: string; low: number | null; high: number | null; words: string; unread: boolean;
    /** how many were listed from `t` (supply — the feed has no sales); null / "" on a folded day, nothing listed, unread */
    qty?: number | null; listed_words?: string }[];
  low: number | null; high: number | null; low_words: string; high_words: string; sentences: string[];
  /** the span's start and the server's now (epoch seconds): the chart's x axis */
  since: number; until: number;
  /** the recipes the item is a reagent of, in one sentence ("" = none) */
  used_in?: string;
}
/** GET /api/prices/movers (an experiment, site only): the items whose lowest price now is at least 10% off their usual
 *  price over the window (the middle value of the window; the floors are `floor_words`) — `dips` and `rising`, EVERY such item, the biggest move in percent
 *  first, every figure the server's words. `ready` false + `words` = the kept history doesn't cover the window yet; an
 *  empty list has its own sentence. */
export interface MoverRow {
  id: number; name: string; usual_words: string; now_words: string; change_words: string; qty: number; uses: number; uses_words: string;
  /** the move in percent and in copper (signed, for the card's order and filter), the copper in words ('−20g'), the item's group id */
  pct: number; diff: number; diff_words: string; group: string;
}
export interface Movers {
  window: string; windows: { id: string; label: string }[]; ready: boolean; words: string;
  dips: MoverRow[]; rising: MoverRow[]; dips_words: string; rising_words: string;
  /** Climbing / Sliding: items whose price went ONE way step after step over the window (`trend_words` says the step);
   *  the same rows, `usual_words` = the price in the first step. An empty list has its own sentence. */
  climbing: MoverRow[]; sliding: MoverRow[]; climbing_words: string; sliding_words: string; trend_words: string;
  /** the two floors in words ('at least 20 listed, from 1s') */
  floor_words: string;
  /** the card's choices: item groups (All first, Other last) and the one shown first, the orders, the smallest moves in
   *  percent, the rows shown at first and per Show more, the sentence for a choice that leaves a list empty */
  groups: { id: string; name: string }[]; group: string; sorts: { id: string; label: string }[]; steps: { id: string; label: string }[];
  page: number; none_words: string;
}
/** GET /api/prices/bargains (an experiment, site only): the materials whose lowest price here is far under where most
 *  materials sit against another economy's price (`ref_words`, e.g. Classic's), the widest gap first, every figure the
 *  server's words. `hint_words` "" = the version has no reference prices (no tab); `ready` false + `words` = why not yet. */
export interface BargainRow { id: number; name: string; ref_words: string; now_words: string; gap: number; gap_words: string; qty: number; uses: number; uses_words: string }
export interface Bargains {
  ready: boolean; words: string; hint_words: string; typical_words: string; source_words: string; rows: BargainRow[];
  page: number; none_words: string;
}
/** GET /api/prices/prep (an experiment, site only): `rows` = levelling-zone materials that end up in high-level
 *  consumables (`uses_words` names them; `gap_words` = Bargains' gap when it has one, `vendor_words` = what a vendor
 *  pays), `below_rows` = what is listed under a vendor's price. `hint_words` "" = the version has no Prep (no tab). */
export interface PrepRow { id: number; name: string; now_words: string; qty: number; ends: number; uses_words: string; vendor_words: string; gap: number | null; gap_words: string }
export interface BelowRow { id: number; name: string; now_words: string; vendor_words: string; profit_words: string; qty: number }
export interface Prep {
  ready: boolean; hint_words: string; rows: PrepRow[]; none_words: string;
  below_rows: BelowRow[]; below_words: string; below_none: string; page: number;
}
/** GET /api/admin/market: the guild's auction house (officers read, the owner picks). `words` = the choice in words (or what
 *  is missing), `prices` = how recent the prices are, `clear_sentences` = Turn prices off…'s confirm. */
export interface MarketState {
  selected: { house_id: number; ruleset_id: number; name: string; faction: string; picked_by: string; picked_label: string } | null;
  words: string; clear_sentences: string[]; prices: string; rev: string; owner: boolean;
}
/** POST /api/admin/market {choices: true}: the houses the owner may pick, each with how much is listed and three sample
 *  prices; `words` = why the list is empty, else "". */
export interface MarketChoices {
  choices: { house_id: number; name: string; listings: number; listings_words: string; samples: { id: number; name: string; low_words: string }[] }[];
  ruleset_id: number | null; words: string;
}
/** GET /api/professions/find: at most 25 rows; `total` = every match; `message` = the answer in words (the empty
 *  answer's sentence); `more` = how many more match, as a sentence ("" when none are left out). */
export interface FindResult { q: string; recipes: CatalogRecipe[]; total: number; message: string; more?: string }
/** A recipe typed before the game's list that the list lacks (officers fix it: Pick the recipe… / Remove). */
export interface UnlistedRecipe { uid: string; member: string; label: string; profession: string; name: string }
/** Every profession with its crafters, every recipe somebody makes, `officer` = the reader may fix the recipes not in the
 *  game's list, and those recipes (`unmatched`, officers only). */
export interface ProfessionsData {
  professions: { id: string; name: string; group: string; icon: string; crafters: Crafter[] }[]; recipes: CatalogRecipe[]; officer: boolean;
  unmatched?: UnlistedRecipe[];
}
/** `rosters` = the locked runs it holds a seat on (words); `regular_on` = the recurring schedules it is a regular of (words). */
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; regular_on?: string[]; confirmed: boolean; role: string; off_role: string | null; flex?: string[]; professions?: Profession[];
  /** armory link §5.4 (web/api_armory.char_extras): null where the version has no armory; `profession_sync` = the armory keeps
   *  its professions (false = kept by hand); `flags` (officers read them) = "class" · "missing" · "outside" */
  armory?: CharArmory | null; profession_sync?: boolean; flags?: ArmoryFlag[] }
/** A character's link (web/api_armory.armory_json): `updated` = "Updated from the armory <when>" in words, `when` = that
 *  <when> alone (null before the first check); `cls` / `level` = the armory's; `in_guild` true / false / null = not known (R9). */
export interface CharArmory { linked: boolean; id: number | null; name: string | null; surname: string | null; level: number | null; cls: string | null;
  updated: string | null; when: string | null; in_guild: boolean | null; missing: boolean }
export type ArmoryFlag = "class" | "missing" | "outside";
/** What a lookup found (POST …/armory/find, link / give previews): the armory's name, class and level. */
export interface ArmoryFound { id: number; name: string; surname: string | null; label: string; cls: string | null; level: number; source: "profile" | "roster" }
/** A character on the linked guild's roster (the not-claimed fold, the review's candidates). */
export interface RosterRow { id: number; name: string; surname: string | null; label: string; cls: string | null; level: number }
/** The roster as last read: `words` = the sentence when there is none (no guild linked / not read yet), else null; `read` in words. */
export interface RosterState { guild: string | null; read: string | null; words: string | null }
/** GET /api/armory/map (officers): the not-claimed rows, the roster's state and the counts (each character's link is on its row). */
export interface ArmoryMap { unclaimed: RosterRow[]; roster: RosterState; counts: { linked: number; not_linked: number; unclaimed: number } }
/** One registered character set against the roster (GET /api/armory/seed). */
export interface SeedRow { uid: string; member: string; label: string; cls: string; candidate: RosterRow | null; why: string; others: RosterRow[] }
export interface Seed { sure: SeedRow[]; likely: SeedRow[]; none: SeedRow[]; unclaimed: RosterRow[]; rev: string; roster: RosterState;
  counts: { sure: number; likely: number; none: number; unclaimed: number } }
/** One pick's result on POST /api/armory/seed/apply (preview: would it pass; write: did it): `text` = its sentence. */
export interface SeedResult { uid: string; label: string; id: number; ok: boolean; text: string }
export interface Absence { start: string; end: string; reason: string | null; span?: string }  // `span` = the days in words (the clock rule); start/end stay ISO for the clear call
/** One live sheet on the Me page (web/api_me.py sheet_row_extras): `may_answer` = Join / Bench / No thanks apply now (open);
 *  `may_cant` = Can't make it… (with a note) applies (locked, rostered or joined), `cant_consequence` = its confirm's sentences; `queue_note` = ad hoc and joined — a change
 *  away from Join asks this once; `receipt` = the state in words (rc.answer_receipt). The row renders from these only. */
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; label: string | null; character: string | null; note: string | null; rostered: boolean; roster: number | null; badge?: string; state_word?: string; kind?: "recurring" | "adhoc"; regular?: boolean; confirms?: boolean; may_answer?: boolean; may_cant?: boolean; cant_consequence?: string | null; queue_note?: string | null; receipt?: string | null;
  /** follow-on runs (design §5.62): `then` = the follow-on in words on a lead, `lead_raid` = the lead's name on a follow-on;
   *  `may_leave` = Leaving after the lead applies, `leave_consequence` = its confirm's sentence; `may_stay` = I'm staying undoes it */
  then?: string | null; lead_raid?: string | null; may_leave?: boolean; may_stay?: boolean; leave_consequence?: string | null }
/** One recurring schedule the viewer is a regular of (the Me page's My regular raids). */
export interface MyRegular { series: string; label: string; character: string; cls: string; spec: string; role: string }
/** One seat of the group an ask would put you in (spec icon alone on the card; `me` = you). */
export interface AskSeat { character: string; cls: string; spec: string; role: string; me: boolean }
/** One open ask wherever it waits (inbox.asks_of): `kind` confirm (answered with POST /api/me/ask/{id}) | fill | swap | prelock
 *  (POST /api/me/fill/{run}); `channel` dm | web (DMs off, or the DM failed: the Me page is where it waits); `held` = you said yes and
 *  `waiting_for` is the other half of a tied pair. Every label (`title`, `when`, `deadline`) is words from the server. */
export interface MyAsk {
  id: string; kind: "confirm" | "fill" | "swap" | "prelock"; title: string; run: string; roster: string; raid: string; when: string; starts_at: string;
  character: string; cls: string | null; spec: string | null; role: string | null; as_label: string;
  group: { roster: number; rosters: number; group: number; members: AskSeat[] } | null; deadline: string | null; channel: "dm" | "web";
  held: boolean; waiting_for: string | null; reason: string | null;
}
/** An outcome to know (D26 — never a DM): not rostered, seat released, answered for you, run cancelled, schedule removed, … */
export interface Notice { id: string; kind: string; text: string; run: string | null; when: string; read: boolean }
/** What an officer may record for a member on a run (GET /api/admin/actions — rc.member_actions, the SetWizard's list), each with the
 *  consequence sentence from the server; `rev` = the member's state the list was drawn from (a stale one is a 409 naming the change). */
export interface MemberAction { key: string; label: string; needs: "character" | "note" | null; characters?: string[]; consequence: string; by_character?: Record<string, string> }
export interface MemberActions { uid: string; display_name: string; run: string; actions: MemberAction[]; rev: string; now: string }
export interface Aura { abbr: string; colour: string; art: string | null; name: string; who?: string; in_run?: boolean; covered?: boolean }
export interface GroupSummary {
  roles: Record<string, number>; synergy: number | null; unmet: string[]; assumptions: string[];
  raid: { abbr: string; colour: string; art?: string | null; name: string; ok: string; n: number; detail: string; status: string }[];
  groups: { n: number; members: { name: string; member: string; cls: string; spec: string; role: string }[]; present: Aura[]; missing: Aura[]; picks: { element: string; id: string; abbr: string; name: string }[]; value: number }[];
}
export interface Signup { uid: string; display_name: string; character: string; cls: string; spec: string; offspec: string | null; role: string; status: string; label: string; source: string; note: string | null; pin: "in" | "out" | null }
export interface Seat { display_name: string; character: string; cls: string; spec: string; role: string; uid?: string | null; answer?: string | null; signed_at?: string | null }
/** `open` = the roster's empty seats a minimum still wants, as roles (tank first): the page draws the role icon + "open seat"; null on the Events page's board before the lock. */
export interface BoardRoster { n: number; rostered: number; synergy: number | null; advisories: string[]; groups: Seat[][]; summary: GroupSummary; open?: string[] | null }
/** `leftovers` = joiners the full roster(s) left out (names, also in `bank`); `another` = one line when leftovers + bench + pool could make another run.
 *  `bench` = before the lock, the Bench answers no group holds, in answer order (the Events board's second bank section); [] once locked. */
export interface Board { n_groups: number; group_size: number; size: number; bank: Seat[]; bench?: Seat[]; rosters: BoardRoster[]; leftovers?: string[]; another?: string | null; balance?: string | null }
/** Why the joiners make fewer runs than the headcount allows: run `run` is short these tank/healer counts (roles_allow 0 = not even one). */
export interface SplitReason { bodies_allow: number; roles_allow: number; run: number; short: Record<string, number> }
export interface Confirmation { uid: string | null; display_name: string; character: string; cls: string; spec: string; role: string; roster: number; answer: string | null; asked_at: string | null }
/** A regular of the run's series and what they answered (status null = no answer). */
export interface RegularAnswer { uid: string; display_name: string; character: string; cls: string; spec: string; role: string; status: string | null; source: string | null; signed_as: string | null }
export interface Sheet {
  key: string; run: string; name: string; size: number; instance: string | null; raid: string; starts_at: string; when: string; rel: string; state: string; state_word?: string; run_title?: string; live: boolean; fill_state: string;
  counts: Record<string, number>; rostered: number; n_rosters: number; /** the rosters the run expects up front (its schedule's rosters per run): what the count and the board show before the lock */ rosters_expected?: number;
  /** recurring (has a series and regulars) or ad hoc (first come); `confirms` false = Join is the commitment */
  kind?: "recurring" | "adhoc"; series?: string | null; series_name?: string | null; confirms?: boolean; test?: boolean;
  /** who `not_answered` lists: the series' regulars, everyone (no regulars set yet), the test bench, or nobody (ad hoc) */
  expected_from?: "regulars" | "everyone" | "test" | "none"; regulars?: RegularAnswer[];
  timeline?: { nudge: string | null; lock: string; confirm: string | null; locked_at?: string | null };
  signups?: Signup[]; not_answered?: Seat[]; absences?: { display_name: string; start: string; end: string; reason: string | null; signed: boolean }[]; double_booked?: string[];
  needs?: { headcount: number; roles: Record<string, number>; size: number } | null; board?: Board; has_layout?: boolean; rev?: string;
  split?: { strategy: string; policy: string; runs: number; reason?: SplitReason | null; reason_text?: string | null };
  confirmations?: Confirmation[];
  fill_asks?: { display_name: string; kind: string; character: string; spec: string; role: string; reason: string; why?: string | null; answer: string | null; phase?: "prelock" | "fill"; approved_by?: string | null }[];
  /** the planner's suggestions (computed on view, never stored): what an officer's Suggest / Ask would send */
  plan?: Plan | null;
  callouts?: { display_name: string; hours_before: number; late: boolean }[]; log?: string[];
  /** the confirm sentences, from the server (consequences.py) — the browser prints them, never composes its own;
   *  `release_named` and the board's moves (move / roster / swap / substitute) carry `{name}` / `{other}` / `{to}` where the
   *  names go (the board only substitutes them — shared.tsx fillNames); remind / end are whole sentences */
  consequences?: { cancel: string; clear: string; release: string; release_named?: string; move?: string; roster?: string; swap?: string; substitute?: string; remind?: string; end?: string };
  /** the run's loot council at a glance (web/api.py loot_json): `may_open` = locked with no council yet */
  loot?: { open: boolean; councils: number; thread: string | null; awards: number; pending: number; reasons: number; may_open: boolean;
    /** "Bosses 3 / 6" (design §3.5 L12): the slowest council's kills against the raid's bosses (total null while unlisted) */
    bosses?: { done: number; total: number | null; all_down: boolean; text: string } | null };
  /** follow-on runs: `follow` = the run after this one the same night (its raid NAME), `lead` = the run this one follows (its signup is the lead's);
   *  `leaving` = who leaves after the lead (names); `when` is words from the server */
  follows?: string | null; followed_by?: string | null; follow?: { raid: string; rosters: number; key: string; when: string; at?: string } | null;
  lead?: { raid: string; key: string } | null; leaving?: string[];
  /** the run's identity (phase 3): which raid of its run this is (0 = the first), the run it belongs to (its first raid's key),
   *  the schedule night it stands for, and the raids it plays (`starts` = minutes after the first raid) */
  part?: number; event?: string; occurrence?: string | null; lineup?: { raid: string; rosters: number; starts: number }[];
  /** its start in guild wall time, 'YYYY-MM-DDTHH:MM' — the Reschedule picker's machine value, never shown as text */
  start_local?: string;
  /** false for a later raid whose first raid has finished: a cancel posts nothing in the signup channel then */
  cancel_tells_channel?: boolean;
}
/** POST /api/run/{key}/reschedule and /lineup with preview — the confirm's sentences (from the server) and the run's rev a write carries back. */
export interface RunPreview { sentences: string[]; rev: string }
/** A raid's run open or locked now (never a test run), in words — the "When it runs" block on Raids and Rosters. */
export interface LiveRun { key: string; when: string; state: string; state_word?: string; schedule: string }
/** The next run an active schedule opens that has no sheet yet; `opens` = when its sheet opens on its own. */
export interface NextRun { when: string; schedule: string; opens?: string }
/** One upcoming run of an active schedule (web/api.py rosters_data → rc.upcoming_rows): `can_open` false comes with `why`, the server's sentence. */
/** `occurrence`: the stamp a schedule's run is found by (Open carries it back); a run has no key until it opens. */
export interface UpcomingRun { schedule: string; schedule_name: string; start: string; start_iso: string; occurrence: string; open_key: string | null; closed: boolean; opens: string; opens_iso: string; needs_channel: boolean; can_open: boolean; why: string | null; follow?: string }
export interface RaidRuns {
  id: string; name: string; size: number; slots: string[]; slot_labels?: string[]; lockout_days: number; opened: boolean; first_open: string | null; first_open_words?: string;
  live_runs?: LiveRun[]; next_run?: NextRun | null;
  open: Sheet[]; locked: Sheet[]; past: Sheet[]; past_total?: number; upcoming: UpcomingRun[];
  /** every schedule of the raid, in words (`label`), for the open-a-run control; pickup templates (ad hoc) open only on demand */
  schedules?: { id: string; name: string; kind: string; class?: "recurring" | "adhoc"; label: string; active: boolean; follow_words?: string }[];
  /** one entry per recurring schedule: its regulars (one member per line on the page) against the role minimums */
  series?: Series[];
  /** regulars of a schedule that was removed (or became ad hoc): listed, never stripped silently */
  /** a removed schedule's regulars; `clear` = the clear's sentence from code, `rev` = the member record's (a stale clear is a 409) */
  orphan_regulars?: { key: string; label: string; uid: string; display_name: string; character: string; cls: string; spec: string; clear?: string; rev?: string }[];
}
/** `remove` = the remove confirm's sentence from code (consequences.regular_remove), `rev` = the member record's (a stale remove is a 409) */
export interface Regular { uid: string; display_name: string; character: string; cls: string; spec: string; role: string; remove?: string; rev?: string }
/** One upcoming start of a recurring series: its regulars minus recorded absences, against the minimums. */
/** `open_key`: the run already open (or locked) at that start — the Readiness page shows it instead of "Open this run early" (GET /api/readiness only). */
export interface ForecastRow { start: string; day: string; when: string; expected: number; seats: number; short: Record<string, number>; away: string[]; open_key?: string | null }
/** `level`: green = regular mains cover the tank/healer minimums with a spare, amber = no spare (`no_spare`) or only with offspecs / flex / alts (`cover`), red = not even then (`recruit`); null = no regulars yet. */
export interface Series { key: string; id: string; name: string; label: string; active: boolean; seats: number; regulars: Regular[]; counts: Record<string, number>; need: Record<string, number>;
  level?: "green" | "amber" | "red" | null; no_spare?: string[]; cover?: string[]; recruit?: string[]; forecast?: ForecastRow[] }
/** Someone who can be made a regular (real members with active characters, main first). */
/** `plays` (the Events page, event_view.character_rows): the specs a character may answer as (rc.spec_role's rule). */
export interface Person { uid: string; display_name: string; characters: { label: string; cls: string; spec: string; role: string; is_main: boolean; plays?: string[] }[] }
/** One schedule of a raid (mirrors web/api.py schedule_json). Machine forms (`slots`, `days`, `time`) are for the editors;
 *  everything a person reads comes as words from the server (`label`, `slot_labels`, `time_label`, `next`). */
export interface Schedule {
  id: string; name: string; kind: "weekly" | "lockout" | "pickup"; active: boolean; rosters: number; seats: number;
  /** its lineup's raid ids (the first is the raid it is listed under) and its own rev (revs["config:schedule:<id>"]) */
  raids?: string[]; rev?: string;
  /** derived from the kind: weekly / lockout = recurring (regulars), pickup = ad hoc (first come, the ad hoc defaults) */
  class?: "recurring" | "adhoc"; class_label?: string; series?: string | null; regulars?: number;
  /** where each inherited value comes from: own | raid | adhoc (defaults); weights per key */
  source?: Record<string, string | Record<string, string>>; own_weights?: Record<string, number>; weights?: Record<string, number>;
  slots: string[]; slot_labels: string[]; days: number[]; time: string; time_label: string; label: string;
  /** cadence the schedule sets itself; anything missing inherits the raid's (`effective` = what applies) */
  own: Record<string, number | boolean | string>; effective: Record<string, number | boolean | string>;
  /** `next` = the runs that WILL open (empty while paused); `next_if_active` = the runs it would open (design RD-10) */
  next: string[]; next_if_active?: string[];
  /** the server's sentences: the Pause / Resume confirm, the removal confirm (Registry.schedule_removal_effects), the rosters-per-run copy per n */
  pause_sentence?: string; resume_sentence?: string; removal_sentence?: string; rosters_copy?: Record<string, string>;
  /** the raid run the same night from this schedule's signup (its id and rosters), and that in words ("Barrow Deeps ×2") */
  follow?: { raid: string; rosters: number } | null; follow_words?: string;
  /** the whole lineup, the first raid first ([{raid, rosters, starts?}]; `starts` = minutes after the first raid, absent = when the raid before it ends) */
  lineup?: { raid: string; rosters: number; starts?: number }[];
}
export interface Rosters { raids: RaidRuns[]; orphans: Sheet[]; people?: Person[]; tz: string }
export interface RaidRule {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; notes: string;
  /** sheet timing is each schedule's own (`schedules[].effective`), never the raid's */
  slots: string[]; slot_labels?: string[]; weights: Record<string, number>; split_policy: string;
  /** the guild's own minimums as stored (with their reasons), and what every run reads (Registry.minimums: the game's tank and healer ones included) */
  minimums: Record<string, MinimumEntry>; minimum: Record<string, number>;
  overridden: string[]; comp_groups: string[];
  /** the raid's sheets open or locked now (never a test run) and the next run an active schedule opens — the card's "When it runs" */
  live_runs?: LiveRun[]; next_run?: NextRun | null;
  first_open_local: string; /** Registry.local12 words for a person ("Wed 9 Dec 3:00 PM"); first_open_local is the input's machine form */ first_open_words?: string; opened: boolean; window: [string, string]; live: number;
  schedules: Schedule[];
  /** the raid section rev an edit-mode Save sends back as revs["config:raids:<id>"] */
  rev?: string;
  /** what a Reset sends back as revs["config:raid_all:<id>"]: the raid's changes AND the schedules the reset removes */
  reset_rev?: string;
  /** the Raids page (phase 4) */
  page?: RaidPage;
}
export interface Raids { raids: RaidRule[]; tz: string; owner: boolean; weight_keys: string[]; split_policies: string[]; schedule_kinds: string[]; schedule_cadence: string[]; max_rosters: number }
export interface SplitPreview { strategy: string; layout: string[][]; board: Board; synergy: number[]; total: number; gap: number; balance?: string | null; reason?: SplitReason | null; reason_text?: string | null }
export interface FillAskRow { uid?: string; display_name: string; kind: string; character: string; spec: string; cls: string | null; role: string; reason: string; why?: string | null; pair: string | number | null; vacates?: string | null }
/** One seat the planner covers: `options[0]` the suggestion, then alternates; an option is one ask or a tied swap + backfill. role "" = a plain seat after lock. */
export interface PlanSeat { role: string; options: FillAskRow[][] }
/** raidcycle.role_plan as data: before lock (`prelock`) the suggestions for the tank/healer minimums, after lock (`fill`) the seats to fill; `rev` must ride with every send. */
export interface Plan { phase: "prelock" | "fill"; short: { headcount: number; roles: Record<string, number> }; have: Record<string, number>; need: Record<string, number>; full: boolean; expected: number; waiting?: string[];
  seats: PlanSeat[]; short_after: { headcount: number; roles: Record<string, number> }; still_short: string; rev: string; lines: string[];
  /** after lock: how many more asks may go out now (at most 3 out at once); null before lock */ room?: number | null; batch_lines?: string[] }
/** What Fill seats would send: the shortfall, who is still being waited on, the plan, and `batch` = exactly what a send with the plan's rev (no picks) DMs now. */
export interface FillPreview { short: { headcount: number; roles: Record<string, number>; size?: number }; waiting: string[]; batch: FillAskRow[]; plan?: Plan }
/** An open Confirm ask on a LIVE run (GET /api/members, design §5.58): `words` = "<raid> on <day>" (the link's text, to
 *  /events/<key>), `line` = the standing line ("… waits for their Confirm"), `how_words` = how it reaches them. */
export interface MemberAsk { roster: string; answer: string | null; raid: string; when: string; key: string | null; words: string; line: string; how: "dm" | "web"; how_words: string }
/** One member on the Members page. `left` = the day the bot noticed they are not in the Discord server (words; null
 *  while they are), `left_line` its line, `remove_consequence` Remove record's sentence (only when left); `away` = the
 *  standing line of their current or next absence, `away_now`; `attendance` = the server's sentence once a run counts. */
export interface MemberRow { uid: string; display_name: string; verification: string; privilege: string; characters: Character[]; absences: Absence[]; asks: MemberAsk[];
  /** the member's rank (D25: on the main), DMs on, the character table's base rev (a stale save is refused, 409) */
  rank?: string | null; dm?: boolean; characters_rev?: string;
  test?: boolean; attendance?: string | null; left?: string | null; left_line?: string | null; remove_consequence?: string | null; away?: string | null; away_now?: boolean }
/** A character-table save (THE character verb): what changed, the ripple into sheets and seats, the new base rev; `refused` per member on the Members page. */
export interface TableSaved { message: string; lines: string[]; ripple: string[]; preview?: boolean; characters_rev?: string | null; refused?: { uid: string; display_name: string; error: string; stale: boolean }[];
  /** a preview's confirm words (consequences.save_consequence): per member on Members, flat on Me; [] = save on the first press */
  sentences?: { uid: string; display_name: string; sentences: string[] }[] | string[] }
/** POST /api/members/set (and /api/members/dm) with `preview`: THE confirm words and the record's rev to write with. */
export interface PeoplePreview { sentences: string[]; rev?: string; message?: string }
/** GET /api/absences: officers' table (puppets only for their tester). */
export interface AbsenceRow { uid: string; display_name: string; start: string; end: string; span: string; reason: string | null; by: string; current: boolean; test?: boolean }
/** An absence or I'm back preview: the span and what it does to each live sheet, in words; nothing written. */
export interface AbsencePreview { preview: true; message: string; lines: string[]; span: string; start?: string; end?: string; sentences?: string[] }
/** GET /api/members: `rank_choices` = the ranks an officer gives (never alt); `pending` = characters waiting for an officer's
 *  confirm; `own` = the refusals on the viewer's own record ({} for the owner). */
export interface Members { rows: MemberRow[]; members: number; tz: string; rank_choices?: string[]; pending?: number; own?: { rank?: string; confirm?: string } }
/** Clearing an absence: `message` is the headline plus the ripple; `lines` is the ripple alone (sheets re-opened, seat that had been freed). */
export interface AbsenceCleared { message: string; lines: string[] }
/** `scope: "bot"` (several guilds only): head, push, llm and uptime are the whole bot's, the rest is this guild's. */
export interface Ops { scope?: "bot"; head: string; push: boolean; llm: string; feed: string; up: number; started_at: number; rows: { time: string; level: string; text: string }[];
  /** OPS-01: errors folded into an earlier alert (no DM of their own) since the bot started; failed ops-channel posts in a row */
  suppressed?: number; ops_failing?: number }  // bot health only: precedents and the award log live on the Loot pages
/** A Discord role by id (a string: snowflakes overflow JS numbers); officer roles are stored by id so renames are safe. */
export interface RoleRef { id: string; name: string }
/** GET /api/public (anonymous): what the homepage and the not-a-member screen show. One guild: this shape; several:
 *  `{guilds: PublicInfo[]}` (each with its key) — read it through `publicGuilds`. */
export interface PublicInfo { name: string; about: string | null; invite_url: string | null; recruiting: boolean; icon: string | null; key?: string }
export const publicGuilds = (p: PublicInfo | { guilds: PublicInfo[] }): PublicInfo[] => ("guilds" in p ? p.guilds : [p]);
export interface Question { id?: string; label: string; hint: string | null; required: boolean }
/** GET /api/recruitment (officers); the owner saves the same shape to /api/admin/recruitment. */
export interface Recruitment { open: boolean; note: string; invite_url: string; questions: Question[]; member_role_ids: string[]; questions_max: number; guild_roles: RoleRef[]; owner: boolean }
export interface Config { yaml: string; docs: Record<string, { text: string; compiled: boolean; summary: string | null }> }
/** The news review (design §5.26): the local jobs as the monitor sees them. Times arrive as 12-hour labels from the server. */
export interface LaunchdState { loaded: boolean; state: string | null; pid: number | null; last_exit: string | null }
export interface AgentJob {
  job: string; name: string; label: string; state: "idle" | "running" | "failed"; light: "green" | "amber" | "red"; step: string | null; run_id: string | null;
  result: string | null; started_label: string; finished_label: string; next_label: string; schedule: string; launchd: LaunchdState;
  started?: string | null; finished?: string | null;
  /** running for another server's proposal (several guilds): the page says so instead of naming it */
  elsewhere?: boolean;
  /** not installed with launchd: the exact shell line that installs it */
  install?: string | null;
}
/** D33's raid guard for one guild (GET /api/guilds, GET /api/agents) */
export interface GuildBusy { raiding: boolean; until: string | null; until_label: string; why: string }
export interface McpCard { configured: boolean; local_only: boolean; last_used: string | null; last_used_label: string; config_file: boolean }
export interface LlmUsage { spent: number; cap: number; by_guild: { key: string; name: string; spent: number }[]; unattributed: number; shared: boolean }
export interface AgentRunRow { id: string; job: string; started: string; started_label: string; outcome: string }
export interface Agents {
  jobs: AgentJob[]; bot: LaunchdState; runs: AgentRunRow[]; owner: boolean; waiting: number; approved: number; guild?: string;
  /** the owner: a committed game-data edit the bot hasn't restarted for (the apply job's status `restart_pending`) */
  restart_pending?: { id: string; guild: string | null; commit: string | null; auto: boolean; title: string | null; text: string };
  apply_mode?: { mode: "main" | "pr" | "unknown"; source: string | null }; busy?: GuildBusy; ops_channel?: boolean; install_line?: string;
  /** owner only (host facts) */
  mcp?: McpCard; llm?: LlmUsage | null;
}
export interface AgentStep { kind: "runner" | "start" | "text" | "tool" | "result"; title: string; detail?: string | null; result?: string | null; ok: boolean | null; level?: string; tool?: string }
export interface AgentRun { id: string; live: boolean; outcome: string; steps: AgentStep[] }
export interface NewsItem { url: string; title: string; description: string; posted_at: string; posted_label: string; ingested_label: string; matched: string[] }
export interface Proposal {
  id: string; title: string; news: string[]; affects: string; kind: "guild_setting" | "profile" | "needs_developer"; change: string; edit: Record<string, unknown>;
  evidence: string; confidence: string; state: string; created_label: string; decided_by: string | null; decided_label: string; commit: string | null; note: string | null;
  /** state null = the apply job's annotation ("waiting: …"), not a move */
  history: { state: string | null; by: string; at_label: string; note: string | null; annotation?: boolean }[];
  /** the apply job's current "waiting: …" line */
  status_note?: string | null;
  /** the decide screen's words for each verdict still open (news.consequence — the page never composes them) */
  consequence?: { approved?: string[]; dismissed?: string[]; interrupted?: string[] };
  owner_only?: boolean; shared_with?: string[];
}

// ---- the Policy page (mirror policy.policy_state / web/api_policy.py)
export interface PolicyDoc {
  doc: "loot" | "comp" | "persona"; text: string; compiled: boolean; summary: string | null; items: string[]; applied_text: string | null;
  compiled_at: string | null; compiled_label: string; confirmed_by: string | null; differs: boolean; legacy: boolean;
  pending: boolean; pending_by: string | null; pending_at: string; pending_compiled: boolean; pending_reading: string | null; pending_compiled_label: string;
  modal_max: number;
}
// ---- the Help page (GET /api/manual)
export interface ManualSection { id: string; title: string; tier: "officers" | "owner" | null; body: string }
export interface ManualCommand { path: string; description: string; who: "member" | "officer" | "owner" }
export interface Manual { sections: ManualSection[]; commands: ManualCommand[]; tier: "member" | "officer" | "owner" }
export interface Me {
  display_name: string; registered: boolean; must_apply?: boolean; characters: Character[]; roles: { primary: string | null; flex: string[] };
  absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: MyAsk[]; notices?: Notice[]; regulars?: MyRegular[];
  characters_rev?: string | null; rank?: string | null;
  /** the one DM sentence (inbox.DM_ON/OFF_SENTENCE): the same words on /me view, /me dms and the manual */
  dm_sentence?: string;
  /** their own application (registry.application_state): open → waiting; decided → the decision (components/MyApplication.tsx) */
  application?: import("./setupApi").MyApplicationState | null;
  /** what the bot remembers them saying (design §5.61), newest first; memory_on = some channel of the guild remembers */
  memories?: MemoryPage; memory_on?: boolean;
}
/** One thing the bot remembers someone saying (design §5.61): `when` is already 12-hour words from the server. */
export interface MemoryRow { id: string; when: string; at: string; channel: string; channel_name: string; author: string; author_id: string; text: string; context: string; scope: string; scope_word: string }
export interface MemoryPage { rows: MemoryRow[]; total: number; more: boolean }

// ---- the loot council (mirror src/oibot_gm/web/api_loot.py). Every label a person reads arrives as words from the server
// (`*_label` in 12-hour guild time, `when`, the End sentence); scores, reasons and candidates are officers' only (D2).
export type DropState = "ticked" | "proposed" | "awarded";
export interface LootDrop { drop_id: string; item: string; item_id: number; key: string; tiered: boolean; source: string; by: string | null; at_label: string; held: boolean; state: DropState; to: string | null }
export interface LootCandidate {
  character: string; cls: string; spec: string; rank: string; tier: string; offspec: boolean; upgrade: number; attendance: string; attendance_known: boolean;
  wishlist_rank: number | null; recent_power: number; recent_items: string[]; base: number; factors: Record<string, number>; already_has: boolean; unmapped: boolean;
}
export interface LootOverride { to: string; reason: string; by: string; at_label: string }
export interface LootProposal {
  drop_id: string; item: string; item_id: number; boss: string; bot: string; to: string; source: string; override: LootOverride | null; batch: number; order: number;
  tiered: boolean; close_call: boolean; order_sensitive: boolean; judged_by: string; justification: string; alternates: string[]; warnings: string[]; candidates: LootCandidate[]; proposed_label: string;
}
export interface LootAwardRow { drop_id: string | null; item: string; item_id: number; boss: string; to: string; disenchant: boolean; source: string | null; bot: string | null; by: string | null; at_label: string; tier: string; weight: number; offspec: boolean; tiered: boolean; void: boolean; import_id: string | null }
export interface LootOwed { drop_id: string; item: string; recipient?: string; bot?: string; in_game?: string; recorded?: string; at_label?: string }
export interface LootCouncil {
  id: string; run: string; label: string | null; roster_index: number | null; test: boolean; live: boolean; title: string; when: string; run_state: string;
  thread_id: string | null; thread_pending: boolean; bosses: { boss: string; drops: LootDrop[]; counts: Record<DropState, number> }[];
  proposals: LootProposal[]; awards: LootAwardRow[]; pending_reasons: LootOwed[]; conflicts: LootOwed[]; departed: string[];
  roster: { character: string; cls: string; spec: string; role: string | null }[];
  pending: { proposals: number; overrides: number; reasons: number; undistributed: number; conflicts: number }; untiered: number;
  distributing: { by: string; since_label: string; drops: number } | null; interrupted: { since_label: string; drops: number } | null; scores_only: { by: string; at_label: string } | null;
  closing: { notice_label: string; keep_open_label: string }; last_change: string | null; opened_by: string | null; opened_label: string; batch_rev: string; drops_rev: string;
}
export interface LootRun { key: string; title: string; when: string; state: string; instance: string; test: boolean; rosters: number; councils: LootCouncil[]; end_consequence: string;
  /** before the start with no council open: "This event hasn't started yet." (rc.NOT_STARTED_YET), and whether Start now is offered beside it */
  end_note?: string | null; may_start?: boolean }
export interface Companion { character: string; client?: string; state: "live" | "quiet" | "disconnected"; last_seen_label: string; events?: number; routes_to?: string | null }
export interface LootLogState { state: "off" | "channel" | "no_channel"; channel_id: string | null; text: string }
export interface LootOverview {
  runs: LootRun[]; reasons_pending: { import_id: string; run: string; session: string; drop_id: string; item: string; recipient: string; bot: string | null; received_label: string }[];
  conflicts: { drop_id: string; item: string; in_game: string; recorded: string; session: string; run: string; ended: boolean }[];
  companions: Companion[]; feed_waiting: { count: number; since: string; characters: string[] } | null; log: LootLogState;
}
/** A write's answer: the line, and the run's councils as they are now (a 409 carries the same `state` with `error`). */
export interface LootReply { message: string; state: LootRun; batch_rev?: string; drops_rev?: string }
export interface LootHistoryRow { id: string; file: string; run: string; label: string | null; date: string; date_label: string; ended_label: string; test: boolean; awards: number; overrides: number; agreed: number; never_confirmed: number; undistributed: number; reasons_pending: number; text: string }
export interface Precedent { ref: string; item: string; bot: string; human: string; reason: string; by?: string; date?: string; date_label: string; at_label: string; status: "active" | "retired" | "superseded"; status_by?: string | null; status_reason?: string | null; status_at_label?: string; import_id?: string }
/** One award as the log shows it. The officer fields come only when an officer asked for `scores` (filtered on the server). */
export interface LogRow {
  item: string; item_id: number; key: string; character: string; disenchant: boolean; cls: string | null; spec: string | null; boss: string; instance: string; received: string; date_label: string; offspec: boolean; mine: boolean;
  tier?: string; weight?: number; source?: string; bot?: string | null; by?: string | null; import_id?: string | null; candidates?: { character: string; spec?: string; tier?: string; base?: number }[]; reason?: string | null;
  /** My loot only: the award's id (what a question is asked about) and my question about it, if any (L32) */
  award?: string; appeal?: MyAppeal | null;
}
/** A member's own question about one of their awards (L32) — as THEY see it: never the officer's note or who resolved it. */
export interface MyAppeal { id: string; item: string; character: string; award: string; text: string; state: "open" | "resolved"; outcome: string | null; outcome_label: string | null; state_text: string }
/** Loot › Appeals (officers): the question plus the council's record of the award and the card / thread link. */
export interface OfficerAppeal extends MyAppeal {
  member: string; member_id: string; cls: string | null; spec: string | null; item_id: number; boss: string | null; date_label: string; offspec: boolean; asked_label: string; via: string;
  note: string | null; resolved_by: string | null; resolved_label: string | null; link: string | null;
  details: { found: boolean; void?: boolean; source?: string | null; bot?: string | null; by?: string | null; tier?: string; weight?: number; confirmed_label?: string; candidates?: { character: string; spec?: string; tier?: string; base?: number }[]; reason?: string | null };
  /** open only: THE consequence sentence per outcome (the server's words) */
  consequences?: Record<string, string>;
}
export interface WishEntry { item_id: number; name: string; key: string; rank: number; offspec: boolean; note: string | null; source: string; known: boolean; tiered: boolean; boss: string | null; received: string | null; received_label: string | null }
export interface WishCharacter { label: string; cls: string; spec: string; is_main: boolean; rev: string; entries: WishEntry[] }
export interface ItemBrief { key: string; id: number; name: string; boss: string | null; slot: string; type: string; tiered: boolean; learned: boolean; first_seen_label?: string; times_dropped?: number; first_run?: string; source?: string; awards?: number }
export interface ItemGrid extends ItemBrief {
  rows: { key: string; cls: string; spec: string; role: string; tier: string; own: string | null; profile: string | null; equippable: boolean; why: string | null }[];
  wide: Record<string, string>; own: Record<string, string>; tiers: string[]; roles: string[]; awards: number;
}

// ---- the structured ops path (design §5.43): typed changes → POST /api/ops/change → Described (preview) → apply
/** A typed draft: `kind` names what it edits, `target` which one, `fields` typed values (never the plain-text grammar).
 *  kinds: settings · raid · raid_reset · schedule (target "<raid>:<schedule>") · schedule_remove · buff · family (target null + name = new) ·
 *  aura_reset (target null = everything) · recruitment · plain. The server builds the ops (configops.ops_from_fields). */
export interface OpsChange { kind: "settings" | "raid" | "raid_reset" | "schedule" | "schedule_remove" | "buff" | "family" | "aura_reset" | "recruitment" | "plain" | "comp" | "loot_weights"; target?: string | null; fields?: Record<string, unknown> }
/** One setting of a write, current → new, in words (mentions already rendered as #channel / @Role). `before` null = a new thing. */
export interface DescribedPart { label: string; before: string | null; after: string | null }
/** One WRITE (a buff, a family, a raid, a schedule, the guild settings): its parts, the refusal its apply would give, warn, consequences. */
export interface DescribedLine { op: string; target: string | null; label: string; text: string; error: string | null; warn: boolean; parts: DescribedPart[]; notes: string[]; consequences: string[] }
/** configops.Described as the site gets it: `review` = the batch must be reviewed before it is written (warn class or a consequence line). */
export interface Described { lines: DescribedLine[]; refused: string[]; consequences: string[]; warn: boolean; review: boolean; fingerprint: string;
  /** the confirmation box (design §7b) when the batch is one schedule's or one raid's write */
  box?: Box | null }
export interface OpsResult { label: string; ok: boolean; line: string }
/** POST /api/ops/change's answer. Preview: described + token. A Save: applied directly (applied/results/summary) or `needs_review` + token.
 *  A stale token: `updated` (re-previewed, the lines moved) or `restarted` (from before a restart). 409: `changed` = config sections someone else changed. */
export interface OpsReply {
  kind: "change"; described?: Described; describe?: string[]; refused: string[]; review?: boolean; warn?: boolean; fingerprint?: string;
  token?: string; expires_in?: number; needs_review?: boolean; updated?: boolean; restarted?: boolean;
  applied?: string[]; results?: OpsResult[]; error?: string | null; changed?: string[]; summary?: string; note?: string; revs?: Record<string, string>;
}
export const opsApi = {
  /** Preview only: nothing is written. `revs`: the section revs the page loaded ({"config:buffs": rev}) — a stale one is a 409. */
  preview: (changes: OpsChange[], revs?: Record<string, string>) => api.post<OpsReply>("/api/ops/change", { changes, revs }),
  /** A Save: applied at once unless the batch needs review (then `needs_review` + token, nothing written). */
  save: (changes: OpsChange[], revs?: Record<string, string>) => api.post<OpsReply>("/api/ops/change", { changes, revs, apply: true }),
  /** Apply a reviewed preview. With `changes` + `seen` (the fingerprint shown) an expired token re-previews and applies when nothing moved. */
  apply: (token: string, changes?: OpsChange[], seen?: string, revs?: Record<string, string>) => api.post<OpsReply>("/api/ops/change", { token, apply: true, changes, seen, revs }),
  /** A press on a box the page already printed (a raid's Reset from /api/raids' `page.boxes.reset`): applied now; a stale rev is a 409. */
  confirm: (changes: OpsChange[], revs?: Record<string, string>) => api.post<OpsReply>("/api/ops/change", { changes, revs, apply: true, confirmed: true }),
};

// ---- phase 3: minimums, readiness, the effective configuration, the Tell the bot box
/** A raid's minimum as stored (registry.min_entry): a number, or {min, note}; no upper number (D16). */
export type MinimumEntry = number | { min: number; note?: string | null };
/** One minimum as the `comp` change writes it: at least, and why (optional). */
export interface Minimum { min: number; note?: string | null }
/** One minimum as readiness measures it (comp.readiness): `source` officer (the guild's) | derived; `why` = the reason in words. */
export interface CompLine { key: string; want: number; min: number; have: number; level: string; why: string; note: string | null; source: string; flex?: number; flex_who?: string[] }
export interface ReadinessRole { role: string; have: number; need: number; level: string; hint: string | null; cover: Record<string, string[]> }
export interface ReadinessRaid {
  raid: string; name: string; size: number; level: "green" | "amber" | "red";
  headcount: { mains: number; size: number; alts: number; on_roster: number; unnamed: number; level: string };
  roles: ReadinessRole[]; missing_buffs: { id: string; name: string; abbr: string; colour: string; art: string | null }[];
  comp: CompLine[]; short: { key: string; have: number; want: number; why: string }[]; notes: string[];
  series: Series[]; minimums: Record<string, MinimumEntry>; minimum: Record<string, number>; rev: string; comp_groups: string[];
  /** the Raids page's Readiness view (phase 4) */
  view?: ReadinessView;
}
/** A target slot: a role, a class or Class:Spec, with its words (`label`) for selects and tooltips. */
export interface CompSlot { value: string; label: string; kind: "role" | "class" | "spec" }
export interface Readiness { raids: ReadinessRaid[]; slots: CompSlot[]; note_max: number }
/** configops.config_summary(audience) as the site gets it (mentions rendered as names). */
export interface ConfigSummary { audience: "owner" | "officer"; sections: { key: string; title: string; lines: string[] }[] }
/** POST /api/ask: the answer, `act` (an officer's request to do something: preview it on /api/ops/change {text}), or `guide` (outside the ask audience). */
export interface AskReply { answer: string | null; act?: boolean; guide?: boolean; message?: string; officer_view?: boolean }
/** POST /api/ops/change {text}: Claude's reading as ops, the Discord diff's lines (`described`), refusals with who may and where, a token. */
export interface TextPreview extends OpsReply { reply?: string; questions?: string[]; ops?: Record<string, unknown>[] }
