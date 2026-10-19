import type { Board, Sheet } from "../../api";

/** The raid-night fields ev_json adds in dual interfaces phase 2 (web/api.py night_json, links_json, health_json):
 *  every clock and sentence comes as words from the server — the page prints them, never composes its own. */
export interface NightFields {
  /** "Lock failed Tue 5:30 PM — the sheet stays open, retrying: <why>" while an open sheet's last lock failed */
  lock_error?: string | null;
  /** the fill engine's state in words ("waiting for an officer: a role swap would cover it") */
  fill_state_text?: string;
  early_warning?: boolean;
  spec_switches?: { roster: number; uid: string | null; display_name: string; character: string; cls: string; spec: string; signed_spec: string | null }[];
  regulars_not_rostered?: { uid: string; display_name: string; character: string; cls: string; spec: string; status: string }[];
  confirm_by?: string | null;
  confirmations_sent_at?: string | null;
  /** locked with confirmations on, but the lock's Confirm / Can't fan-out hasn't finished (the scheduler resumes it) */
  confirmations_pending?: boolean;
  /** asks in play that wait on members' Me pages (DMs off, or the DM failed) */
  asks_on_site?: number;
  autofill_paused?: boolean; autofill_paused_by?: string | null;
  reminded_at?: string | null; reminded_by?: string | null;
  may_remind?: boolean; remind_note?: string | null;
  may_end?: boolean; end_note?: string | null;
  may_reopen?: boolean; reopen_note?: string | null;
  closed_at?: string | null;
  log_rows?: { at: string; when: string; text: string; officer: boolean }[];
  links?: { sheet: string | null; card?: string | null; thread?: string | null; updates: string | null; loot: string | null };
  health?: { roles: { role: string; have: number; need: number; level: string; hint: string }[]; no_provider: string[]; no_provider_buffs?: { id: string; abbr: string; colour: string; name: string; art: string | null }[] } | null;
  fill_asks?: { uid?: string; display_name: string; kind: string; character: string; spec: string; role: string; reason: string; why?: string | null; answer: string | null;
    phase?: "prelock" | "fill"; approved_by?: string | null; answered_by?: string | null; channel?: "dm" | "web"; pending?: boolean; deadline?: string | null }[];
  callouts?: { display_name: string; hours_before: number; late: boolean; note?: string | null }[];
}

export type NightSheet = Omit<Sheet, "fill_asks" | "callouts"> & NightFields;

/** POST /api/run/{key}/lock/preview — the roster(s) the lock would build now (nothing saved). */
export interface LockPreview { board: Board; consequence: string; leftovers: string[]; strategy?: string | null; rev: string }

/** POST /api/raid/{rid}/open/preview — what Open would do (rc.open_preview); `start` is carried back to Open. */
export interface OpenPreview {
  kind: "scheduled" | "adhoc"; schedule: string | null; schedule_name: string; key: string; start: string; label: string; when_note: string;
  exists: string | null; channel: number | null; channel_name?: string | null; rules: string; past: boolean;
}

/** One upcoming run on the Rosters page (web/api.py rosters_data). */
export interface UpcomingRow { slot: string; start: string; start_iso?: string; key?: string; opens: string; schedule?: string; schedule_name?: string; needs_channel?: boolean }

/** GET /api/runs — past runs, paged. */
export interface RunsPage { runs: Sheet[]; total: number; offset: number; limit: number; more: boolean }
