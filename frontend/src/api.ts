// Thin JSON client. The session cookie carries identity; the header marks requests as ours (CSRF guard on the server).
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
    headers: { "Content-Type": "application/json", "X-Requested-With": "oibot" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (r.status === 401) {
    window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
    throw new ApiError(401, "login required");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new ApiError(r.status, (data as { error?: string }).error || r.statusText); err.body = data; throw err; }
  return data as T;
}

export const api = {
  get: <T>(path: string) => call<T>("GET", path),
  post: <T>(path: string, body?: unknown) => call<T>("POST", path, body),
};

// ---- shapes (mirror src/oibot_gm/web/api.py)
export interface Meta {
  guild: string; tz: string; classes: Record<string, Record<string, string>>; icons: { classes: Record<string, string>; roles: Record<string, string>; specs: Record<string, string> };
  raids: { id: string; name: string; size: number; lockout_days: number }[];
  viewer: { uid: string; name: string; officer: boolean; owner: boolean };
  labels: Record<string, string>;
  /** answer statuses in order (in, sub, out), split philosophies, role order, per-group role caps, class colours, bot start (epoch s) */
  statuses: string[]; split_policies: string[]; roles: string[]; group_caps: Record<string, number>; class_colours: Record<string, string>; started_at: number;
}
/** `rosters` = the locked runs it holds a seat on (words); `regular_on` = the recurring schedules it is a regular of (words). */
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; regular_on?: string[]; confirmed: boolean; role: string; off_role: string | null }
export interface Absence { start: string; end: string; reason: string | null }
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; label: string | null; character: string | null; note: string | null; rostered: boolean; roster: number | null; kind?: "recurring" | "adhoc"; regular?: boolean; confirms?: boolean }
/** One recurring schedule the viewer is a regular of (the Me page's My regular raids). */
export interface MyRegular { series: string; label: string; character: string; cls: string; spec: string; role: string }
/** `roster` is the run's key (what the answer is posted with); `raid` + `when` are what people see.
 *  `channel` is where the ask went: "dm", or "web" when DMs are off (the Me page is then the only place to answer). */
export interface PlacementAsk { roster: string; character: string; asked_at: string; channel?: string | null; raid: string; when: string; key: string | null }
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
export interface MemberRow { uid: string; display_name: string; verification: string; privilege: string; characters: Character[]; absences: Absence[]; asks: { roster: string; answer: string | null; raid: string; when: string; key: string | null }[] }
export interface Members { rows: MemberRow[]; members: number; tz: string }
/** Clearing an absence: `message` is the headline plus the ripple; `lines` is the ripple alone (sheets re-opened, seat that had been freed). */
export interface AbsenceCleared { message: string; lines: string[] }
export interface Ops { head: string; push: boolean; llm: string; feed: string; up: number; started_at: number; rows: { time: string; level: string; text: string }[]; precedents: Record<string, string>[]; ledger: Record<string, string>[] }
/** A Discord role by id (a string: snowflakes overflow JS numbers); officer roles are stored by id so renames are safe. */
export interface RoleRef { id: string; name: string }
/** GET /api/public (anonymous): what the homepage and the not-a-member screen show. */
export interface PublicInfo { name: string; about: string | null; invite_url: string | null; recruiting: boolean; icon: string | null }
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
  absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: PlacementAsk[]; regulars?: MyRegular[];
}
