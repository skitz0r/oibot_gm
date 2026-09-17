// Thin JSON client. The session cookie carries identity; the header marks requests as ours (CSRF guard on the server).
export class ApiError extends Error {
  status: number;
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
  if (!r.ok) throw new ApiError(r.status, (data as { error?: string }).error || r.statusText);
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
}
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; confirmed: boolean; role: string; off_role: string | null }
export interface Absence { start: string; end: string; reason: string | null }
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; label: string | null; character: string | null; note: string | null; seated: boolean; roster: number | null }
export interface PlacementAsk { roster: string; character: string; asked_at: string }
export interface Aura { abbr: string; colour: string; art: string | null; name: string; who?: string; in_run?: boolean; covered?: boolean }
export interface GroupSummary {
  roles: Record<string, number>; synergy: number | null; unmet: string[]; assumptions: string[];
  raid: { abbr: string; colour: string; art?: string | null; name: string; ok: string; n: number; detail: string; status: string }[];
  groups: { n: number; members: { name: string; member: string; cls: string; spec: string; role: string }[]; present: Aura[]; missing: Aura[]; picks: string[]; value: number }[];
}
export interface Signup { uid: string; display_name: string; character: string; cls: string; spec: string; offspec: string | null; role: string; status: string; label: string; source: string; note: string | null; pin: "in" | "out" | null }
export interface Seat { display_name: string; character: string; cls: string; spec: string; role: string; uid?: string | null; answer?: string | null }
export interface BoardRoster { n: number; seated: number; synergy: number | null; advisories: string[]; groups: Seat[][]; summary: GroupSummary }
export interface Board { n_groups: number; group_size: number; size: number; bank: Seat[]; rosters: BoardRoster[] }
export interface Confirmation { uid: string | null; display_name: string; character: string; cls: string; spec: string; role: string; roster: number; answer: string | null; asked_at: string | null }
export interface Sheet {
  key: string; run: string; name: string; size: number; instance: string | null; raid: string; starts_at: string; when: string; rel: string; state: string; live: boolean; fill_state: string;
  counts: Record<string, number>; seated: number; n_rosters: number;
  timeline?: { nudge: string; lock: string; confirm: string };
  signups?: Signup[]; not_answered?: Seat[]; absences?: { display_name: string; start: string; end: string; reason: string | null; signed: boolean }[]; double_booked?: string[];
  needs?: { headcount: number; roles: Record<string, number>; size: number } | null; board?: Board; has_layout?: boolean;
  confirmations?: Confirmation[];
  fill_asks?: { display_name: string; kind: string; character: string; spec: string; role: string; reason: string; answer: string | null }[];
  callouts?: { display_name: string; hours_before: number; late: boolean }[]; log?: string[];
}
export interface RaidRuns {
  id: string; name: string; size: number; slots: string[]; lockout_days: number; opened: boolean; first_open: string | null;
  open: Sheet[]; locked: Sheet[]; past: Sheet[]; upcoming: { slot: string; start: string; opens: string }[];
}
export interface Rosters { raids: RaidRuns[]; orphans: Sheet[]; tz: string }
export interface RaidRule {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; notes: string; comp: Record<string, { min?: number; max?: number }>;
  slots: string[]; signup_lead_hours: number; lock_hours_before: number; confirm_hours_before: number; weights: Record<string, number>;
  overridden: string[]; comp_targets: Record<string, unknown>; comp_groups: string[]; first_open_local: string; opened: boolean; window: [string, string]; live: number;
}
export interface Raids { raids: RaidRule[]; tz: string; owner: boolean; weight_keys: string[] }
export interface MemberRow { uid: string; display_name: string; verification: string; privilege: string; characters: Character[]; absences: Absence[]; asks: { roster: string; answer: string | null }[] }
export interface Members { rows: MemberRow[]; members: number; tz: string }
export interface Ops { head: string; push: boolean; llm: string; feed: string; up: number; rows: { time: string; level: string; text: string }[]; precedents: Record<string, string>[]; ledger: Record<string, string>[] }
export interface Config { yaml: string; docs: Record<string, { text: string; compiled: boolean; summary: string | null }> }
export interface Me {
  display_name: string; registered: boolean; characters: Character[]; roles: { primary: string | null; flex: string[] };
  absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: PlacementAsk[];
}
