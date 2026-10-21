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

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
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
}
/** `rosters` = the locked runs it holds a seat on (words); `regular_on` = the recurring schedules it is a regular of (words). */
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; regular_on?: string[]; confirmed: boolean; role: string; off_role: string | null; flex?: string[] }
export interface Absence { start: string; end: string; reason: string | null; span?: string }  // `span` = the days in words (the clock rule); start/end stay ISO for the clear call
/** One live sheet on the Me page (web/api_me.py sheet_row_extras): `may_answer` = Join / Bench / No thanks apply now (open);
 *  `may_cant` = Can't make it… (with a note) applies (locked, rostered or joined); `queue_note` = ad hoc and joined — a change
 *  away from Join asks this once; `receipt` = the state in words (rc.answer_receipt). The row renders from these only. */
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; label: string | null; character: string | null; note: string | null; rostered: boolean; roster: number | null; kind?: "recurring" | "adhoc"; regular?: boolean; confirms?: boolean; may_answer?: boolean; may_cant?: boolean; queue_note?: string | null; receipt?: string | null }
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
  groups: { n: number; members: { name: string; member: string; cls: string; spec: string; role: string }[]; present: Aura[]; missing: Aura[]; picks: string[]; value: number }[];
}
export interface Signup { uid: string; display_name: string; character: string; cls: string; spec: string; offspec: string | null; role: string; status: string; label: string; source: string; note: string | null; pin: "in" | "out" | null }
export interface Seat { display_name: string; character: string; cls: string; spec: string; role: string; uid?: string | null; answer?: string | null; signed_at?: string | null }
export interface BoardRoster { n: number; rostered: number; synergy: number | null; advisories: string[]; groups: Seat[][]; summary: GroupSummary }
/** `leftovers` = joiners the full roster(s) left out (names, also in `bank`); `another` = one line when leftovers + bench + pool could make another run. */
export interface Board { n_groups: number; group_size: number; size: number; bank: Seat[]; rosters: BoardRoster[]; leftovers?: string[]; another?: string | null }
/** Why the joiners make fewer runs than the headcount allows: run `run` is short these tank/healer counts (roles_allow 0 = not even one). */
export interface SplitReason { bodies_allow: number; roles_allow: number; run: number; short: Record<string, number> }
export interface Confirmation { uid: string | null; display_name: string; character: string; cls: string; spec: string; role: string; roster: number; answer: string | null; asked_at: string | null }
/** A regular of the run's series and what they answered (status null = no answer). */
export interface RegularAnswer { uid: string; display_name: string; character: string; cls: string; spec: string; role: string; status: string | null; source: string | null; signed_as: string | null }
export interface Sheet {
  key: string; run: string; name: string; size: number; instance: string | null; raid: string; starts_at: string; when: string; rel: string; state: string; live: boolean; fill_state: string;
  counts: Record<string, number>; rostered: number; n_rosters: number;
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
  /** the confirm sentences for lock / cancel / clear / release, from the server (consequences.py) — the browser prints them, never composes its own;
   *  `release_named` is the release sentence about one member with `{name}` where the name goes (the board substitutes the dragged member) */
  consequences?: { lock: string; cancel: string; clear: string; release: string; release_named?: string };
  /** the run's loot council at a glance (web/api.py loot_json): `may_open` = locked with no council yet */
  loot?: { open: boolean; councils: number; thread: string | null; awards: number; pending: number; reasons: number; may_open: boolean };
}
export interface RaidRuns {
  id: string; name: string; size: number; slots: string[]; slot_labels?: string[]; lockout_days: number; opened: boolean; first_open: string | null;
  open: Sheet[]; locked: Sheet[]; past: Sheet[]; upcoming: { slot: string; start: string; opens: string; schedule?: string; schedule_name?: string }[];
  /** every schedule of the raid, in words (`label`), for the open-a-run control; pickup templates (ad hoc) open only on demand */
  schedules?: { id: string; name: string; kind: string; class?: "recurring" | "adhoc"; label: string; active: boolean }[];
  /** one entry per recurring schedule: its regulars (one member per line on the page) against the role minimums */
  series?: Series[];
  /** regulars of a schedule that was removed (or became ad hoc): listed, never stripped silently */
  orphan_regulars?: { key: string; label: string; uid: string; display_name: string; character: string; cls: string; spec: string }[];
}
export interface Regular { uid: string; display_name: string; character: string; cls: string; spec: string; role: string }
/** One upcoming start of a recurring series: its regulars minus recorded absences, against the minimums. */
export interface ForecastRow { start: string; day: string; when: string; expected: number; seats: number; short: Record<string, number>; away: string[] }
/** `level`: green = regular mains cover the tank/healer minimums with a spare, amber = no spare (`no_spare`) or only with offspecs / flex / alts (`cover`), red = not even then (`recruit`); null = no regulars yet. */
export interface Series { key: string; id: string; name: string; label: string; active: boolean; seats: number; regulars: Regular[]; counts: Record<string, number>; need: Record<string, number>;
  level?: "green" | "amber" | "red" | null; no_spare?: string[]; cover?: string[]; recruit?: string[]; forecast?: ForecastRow[] }
/** Someone who can be made a regular (real members with active characters, main first). */
export interface Person { uid: string; display_name: string; characters: { label: string; cls: string; spec: string; role: string; is_main: boolean }[] }
/** One schedule of a raid (mirrors web/api.py schedule_json). Machine forms (`slots`, `days`, `time`) are for the editors;
 *  everything a person reads comes as words from the server (`label`, `slot_labels`, `time_label`, `next`). */
export interface Schedule {
  id: string; name: string; kind: "weekly" | "lockout" | "pickup"; active: boolean; rosters: number; seats: number; is_default: boolean;
  /** derived from the kind: weekly / lockout = recurring (regulars), pickup = ad hoc (first come, the ad hoc defaults) */
  class?: "recurring" | "adhoc"; class_label?: string; series?: string | null; regulars?: number;
  /** where each inherited value comes from: own | raid | adhoc (defaults); weights per key */
  source?: Record<string, string | Record<string, string>>; own_weights?: Record<string, number>; weights?: Record<string, number>;
  slots: string[]; slot_labels: string[]; days: number[]; time: string; time_label: string; label: string;
  /** cadence the schedule sets itself; anything missing inherits the raid's (`effective` = what applies) */
  own: Record<string, number | boolean | string>; effective: Record<string, number | boolean | string>; next: string[];
}
export interface Rosters { raids: RaidRuns[]; orphans: Sheet[]; people?: Person[]; tz: string }
export interface RaidRule {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; notes: string; comp: Record<string, { min?: number; max?: number }>;
  slots: string[]; slot_labels?: string[]; signup_lead_hours: number; lock_hours_before: number; confirm_hours_before: number; fill_ask_hours: number; weights: Record<string, number>; split_policy: string; nudge: boolean; nudge_hours_before: number; autofill: boolean; open_dm: boolean; confirm?: boolean;
  overridden: string[]; comp_targets: Record<string, unknown>; comp_groups: string[]; first_open_local: string; opened: boolean; window: [string, string]; live: number;
  schedules: Schedule[];
}
export interface Raids { raids: RaidRule[]; tz: string; owner: boolean; weight_keys: string[]; split_policies: string[]; schedule_kinds: string[]; schedule_cadence: string[]; max_rosters: number }
export interface SplitPreview { strategy: string; layout: string[][]; board: Board; synergy: number[]; total: number; gap: number; reason?: SplitReason | null; reason_text?: string | null }
export interface FillAskRow { uid?: string; display_name: string; kind: string; character: string; spec: string; cls: string | null; role: string; reason: string; why?: string | null; pair: string | number | null; vacates?: string | null }
/** One seat the planner covers: `options[0]` the suggestion, then alternates; an option is one ask or a tied swap + backfill. role "" = a plain seat after lock. */
export interface PlanSeat { role: string; options: FillAskRow[][] }
/** raidcycle.role_plan as data: before lock (`prelock`) the suggestions for the tank/healer minimums, after lock (`fill`) the seats to fill; `rev` must ride with every send. */
export interface Plan { phase: "prelock" | "fill"; short: { headcount: number; roles: Record<string, number> }; have: Record<string, number>; need: Record<string, number>; full: boolean; expected: number; waiting?: string[];
  seats: PlanSeat[]; short_after: { headcount: number; roles: Record<string, number> }; still_short: string; rev: string; lines: string[];
  /** after lock: how many more asks may go out now (at most 3 out at once); null before lock */ room?: number | null; batch_lines?: string[] }
/** What Fill seats would send: the shortfall, who is still being waited on, the plan, and `batch` = exactly what a send with the plan's rev (no picks) DMs now. */
export interface FillPreview { short: { headcount: number; roles: Record<string, number>; size?: number }; waiting: string[]; batch: FillAskRow[]; plan?: Plan }
export interface MemberRow { uid: string; display_name: string; verification: string; privilege: string; characters: Character[]; absences: Absence[]; asks: { roster: string; answer: string | null; raid: string; when: string; key: string | null }[];
  /** the member's rank (D25: on the main), DMs on, the character table's base rev (a stale save is refused, 409) */
  rank?: string | null; dm?: boolean; characters_rev?: string }
/** A character-table save (THE character verb): what changed, the ripple into sheets and seats, the new base rev; `refused` per member on the Members page. */
export interface TableSaved { message: string; lines: string[]; ripple: string[]; preview?: boolean; characters_rev?: string | null; refused?: { uid: string; display_name: string; error: string; stale: boolean }[] }
/** GET /api/absences: officers' table (puppets only for their tester). */
export interface AbsenceRow { uid: string; display_name: string; start: string; end: string; span: string; reason: string | null; by: string; current: boolean; test?: boolean }
/** An absence or I'm back preview: the span and what it does to each live sheet, in words; nothing written. */
export interface AbsencePreview { preview: true; message: string; lines: string[]; span: string; start?: string; end?: string }
export interface Members { rows: MemberRow[]; members: number; tz: string }
/** Clearing an absence: `message` is the headline plus the ripple; `lines` is the ripple alone (sheets re-opened, seat that had been freed). */
export interface AbsenceCleared { message: string; lines: string[] }
/** `scope: "bot"` (several guilds only): head, push, llm and uptime are the whole bot's, the rest is this guild's. */
export interface Ops { scope?: "bot"; head: string; push: boolean; llm: string; feed: string; up: number; started_at: number; rows: { time: string; level: string; text: string }[] }  // bot health only: precedents and the award log live on the Loot pages
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
}
export interface AgentRunRow { id: string; job: string; started: string; started_label: string; outcome: string }
export interface Agents { jobs: AgentJob[]; bot: LaunchdState; runs: AgentRunRow[]; owner: boolean; waiting: number; approved: number }
export interface AgentStep { kind: "runner" | "start" | "text" | "tool" | "result"; title: string; detail?: string | null; result?: string | null; ok: boolean | null; level?: string; tool?: string }
export interface AgentRun { id: string; live: boolean; outcome: string; steps: AgentStep[] }
export interface NewsItem { url: string; title: string; description: string; posted_at: string; posted_label: string; ingested_label: string; matched: string[] }
export interface Proposal {
  id: string; title: string; news: string[]; affects: string; kind: "guild_setting" | "profile" | "needs_developer"; change: string; edit: Record<string, unknown>;
  evidence: string; confidence: string; state: string; created_label: string; decided_by: string | null; decided_label: string; commit: string | null; note: string | null;
  history: { state: string; by: string; at_label: string; note: string | null }[];
}
export interface Me {
  display_name: string; registered: boolean; must_apply?: boolean; characters: Character[]; roles: { primary: string | null; flex: string[] };
  absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: MyAsk[]; notices?: Notice[]; regulars?: MyRegular[];
  characters_rev?: string | null; rank?: string | null;
  /** the one DM sentence (inbox.DM_ON/OFF_SENTENCE): the same words on /me view, /me dms and the manual */
  dm_sentence?: string;
}

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
export interface LootRun { key: string; title: string; when: string; state: string; instance: string; test: boolean; rosters: number; councils: LootCouncil[]; end_consequence: string }
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
}
export interface WishEntry { item_id: number; name: string; key: string; rank: number; offspec: boolean; note: string | null; source: string; known: boolean; tiered: boolean; boss: string | null; received: string | null; received_label: string | null }
export interface WishCharacter { label: string; cls: string; spec: string; is_main: boolean; rev: string; entries: WishEntry[] }
export interface ItemBrief { key: string; id: number; name: string; boss: string | null; slot: string; type: string; tiered: boolean; learned: boolean; first_seen_label?: string; times_dropped?: number; first_run?: string; awards?: number }
export interface ItemGrid extends ItemBrief {
  rows: { key: string; cls: string; spec: string; role: string; tier: string; own: string | null; profile: string | null; equippable: boolean; why: string | null }[];
  wide: Record<string, string>; own: Record<string, string>; tiers: string[]; roles: string[]; awards: number;
}

// ---- the structured ops path (design §5.43): typed changes → POST /api/ops/change → Described (preview) → apply
/** A typed draft: `kind` names what it edits, `target` which one, `fields` typed values (never the plain-text grammar).
 *  kinds: settings · raid · raid_reset · schedule (target "<raid>:<schedule>") · schedule_remove · buff · family (target null + name = new) ·
 *  aura_reset (target null = everything) · recruitment · plain. The server builds the ops (configops.ops_from_fields). */
export interface OpsChange { kind: "settings" | "raid" | "raid_reset" | "schedule" | "schedule_remove" | "buff" | "family" | "aura_reset" | "recruitment" | "plain"; target?: string | null; fields?: Record<string, unknown> }
/** One setting of a write, current → new, in words (mentions already rendered as #channel / @Role). `before` null = a new thing. */
export interface DescribedPart { label: string; before: string | null; after: string | null }
/** One WRITE (a buff, a family, a raid, a schedule, the guild settings): its parts, the refusal its apply would give, warn, consequences. */
export interface DescribedLine { op: string; target: string | null; label: string; text: string; error: string | null; warn: boolean; parts: DescribedPart[]; notes: string[]; consequences: string[] }
/** configops.Described as the site gets it: `review` = the batch must be reviewed before it is written (warn class or a consequence line). */
export interface Described { lines: DescribedLine[]; refused: string[]; consequences: string[]; warn: boolean; review: boolean; fingerprint: string }
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
};
