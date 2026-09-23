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
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; confirmed: boolean; role: string; off_role: string | null }
export interface Absence { start: string; end: string; reason: string | null }
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; label: string | null; character: string | null; note: string | null; rostered: boolean; roster: number | null }
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
export interface Sheet {
  key: string; run: string; name: string; size: number; instance: string | null; raid: string; starts_at: string; when: string; rel: string; state: string; live: boolean; fill_state: string;
  counts: Record<string, number>; rostered: number; n_rosters: number;
  timeline?: { nudge: string; lock: string; confirm: string; locked_at?: string | null };
  signups?: Signup[]; not_answered?: Seat[]; absences?: { display_name: string; start: string; end: string; reason: string | null; signed: boolean }[]; double_booked?: string[];
  needs?: { headcount: number; roles: Record<string, number>; size: number } | null; board?: Board; has_layout?: boolean; rev?: string;
  split?: { strategy: string; policy: string; runs: number; reason?: SplitReason | null; reason_text?: string | null };
  confirmations?: Confirmation[];
  fill_asks?: { display_name: string; kind: string; character: string; spec: string; role: string; reason: string; answer: string | null }[];
  callouts?: { display_name: string; hours_before: number; late: boolean }[]; log?: string[];
}
export interface RaidRuns {
  id: string; name: string; size: number; slots: string[]; slot_labels?: string[]; lockout_days: number; opened: boolean; first_open: string | null;
  open: Sheet[]; locked: Sheet[]; past: Sheet[]; upcoming: { slot: string; start: string; opens: string }[];
}
export interface Rosters { raids: RaidRuns[]; orphans: Sheet[]; tz: string }
export interface RaidRule {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; notes: string; comp: Record<string, { min?: number; max?: number }>;
  slots: string[]; slot_labels?: string[]; signup_lead_hours: number; lock_hours_before: number; confirm_hours_before: number; fill_ask_hours: number; weights: Record<string, number>; split_policy: string; nudge: boolean; nudge_hours_before: number; autofill: boolean; open_dm: boolean;
  overridden: string[]; comp_targets: Record<string, unknown>; comp_groups: string[]; first_open_local: string; opened: boolean; window: [string, string]; live: number;
}
export interface Raids { raids: RaidRule[]; tz: string; owner: boolean; weight_keys: string[]; split_policies: string[] }
export interface SplitPreview { strategy: string; layout: string[][]; board: Board; synergy: number[]; total: number; gap: number; reason?: SplitReason | null; reason_text?: string | null }
export interface FillAskRow { display_name: string; kind: string; character: string; spec: string; cls: string | null; role: string; reason: string; pair: number | null }
/** What Fill seats would send: the shortfall, who is still being waited on, the next batch of asks. */
export interface FillPreview { short: { headcount: number; roles: Record<string, number>; size: number }; waiting: string[]; batch: FillAskRow[] }
export interface MemberRow { uid: string; display_name: string; verification: string; privilege: string; characters: Character[]; absences: Absence[]; asks: { roster: string; answer: string | null; raid: string; when: string; key: string | null }[] }
export interface Members { rows: MemberRow[]; members: number; tz: string }
/** Clearing an absence: `message` is the headline plus the ripple; `lines` is the ripple alone (sheets re-opened, seat that had been freed). */
export interface AbsenceCleared { message: string; lines: string[] }
export interface Ops { head: string; push: boolean; llm: string; feed: string; up: number; started_at: number; rows: { time: string; level: string; text: string }[]; precedents: Record<string, string>[]; ledger: Record<string, string>[] }
/** A Discord role by id (a string: snowflakes overflow JS numbers); officer roles are stored by id so renames are safe. */
export interface RoleRef { id: string; name: string }
export interface Config { yaml: string; docs: Record<string, { text: string; compiled: boolean; summary: string | null }> }
export interface Me {
  display_name: string; registered: boolean; characters: Character[]; roles: { primary: string | null; flex: string[] };
  absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: PlacementAsk[];
}
