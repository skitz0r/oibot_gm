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
}
export interface Character { label: string; name: string | null; surname: string | null; cls: string; spec: string; offspec: string | null; is_main: boolean; status: string; rank: string; rosters: string[]; confirmed: boolean; role: string; off_role: string | null }
export interface WeekBlock { day: number; start: number; end: number; level: "preferred" | "available" }
export interface Absence { start: string; end: string; reason: string | null }
export interface MySheet { key: string; raid: string; starts_at: string; when: string; state: string; status: string | null; character: string | null; note: string | null }
export interface PlacementAsk { roster: string; character: string; asked_at: string }
export interface Aura { abbr: string; colour: string; art: string | null; name: string; who?: string }
export interface GroupSummary {
  roles: Record<string, number>; synergy: number | null; unmet: string[]; assumptions: string[];
  raid: { abbr: string; colour: string; art?: string | null; name: string; ok: string; n: number; detail: string; status: string }[];
  groups: { n: number; members: { name: string; member: string; cls: string; spec: string; role: string }[]; present: Aura[]; missing: Aura[]; picks: string[]; value: number }[];
}
export interface Signup { uid: string; display_name: string; character: string; cls: string; spec: string; role: string; status: string; source: string; note: string | null }
export interface Sheet {
  key: string; name: string; roster: string; size: number; instance: string | null; starts_at: string; when: string; state: string; live: boolean; fill_state: string;
  by: Record<string, Signup[]>; needs: { headcount: number; roles: Record<string, number>; size: number } | null; double_booked: number; summary: GroupSummary | null;
  fill_asks: { display_name: string; kind: string; character: string; spec: string; role: string; reason: string; answer: string | null }[];
  callouts: { display_name: string; hours_before: number; late: boolean }[]; log: string[];
}
export interface ProposalRun { key: string; name: string; size: number; starts_at: string; when: string; slot: string; seats: number; shortfalls: Record<string, number>; summary: GroupSummary | null }
export interface Proposal { id: string; state: string; viable: boolean; problems: string[]; notes: string[]; unplaced: [string, string][]; window: [string, string]; decided_by: string | null; created_at: string; runs: ProposalRun[] }
export interface RaidRuns {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; opened: boolean; window: [string, string]; first_open: string | null;
  current: Sheet[]; past: Sheet[]; open: Proposal[]; history: Proposal[]; standing: { key: string; name: string | null; schedule: string | null }[];
}
export interface Rosters { raids: RaidRuns[]; orphans: Sheet[]; tz: string }
export interface RaidRule {
  id: string; name: string; size: number; lockout_days: number; duration_hours: number; notes: string; auto: boolean; comp: Record<string, { min?: number; max?: number }>;
  overridden: string[]; comp_targets: Record<string, unknown>; comp_groups: string[]; first_open_local: string; opened: boolean; window: [string, string]; runs: number; open: number;
}
export interface Raids { raids: RaidRule[]; tz: string; owner: boolean }
export interface Me {
  display_name: string; registered: boolean; characters: Character[]; roles: { primary: string | null; flex: string[] };
  week: WeekBlock[]; absences: Absence[]; dm: boolean; sheets: MySheet[]; asks: PlacementAsk[]; raid_windows: { slot: string; name: string }[];
}
