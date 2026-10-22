// Shapes of the setup routes (mirror src/oibot_gm/web/api_setup.py and the applications routes in web/api.py; design §5.46).
// Every sentence a person reads arrives as words from the server (consequences, the checklist's text, the prior-decision line).

/** One bot channel on the Config page: `missing` = the permissions the bot lacks there (perms.missing_for, Discord's own
 *  names), `problem` = the whole sentence ("I can't post in #x: missing …" / "deleted, or the bot can't see it"). */
export interface BotChannel { kind: string; label: string; hint: string; id: string | null; name: string | null; missing: string[]; problem: string | null; required: string[] }
export interface GuildChannel { id: string; name: string; category: string | null }
export interface ChannelsData {
  channels: BotChannel[]; guild_channels: GuildChannel[]; ready: boolean; owner: boolean; rev: string;
  links: { server?: string; channels?: Record<string, string> }; unreachable: string | null;
}
/** One Setup row: every ✓ computed by the runtime's own test (`test` names it, shown on hover). */
export interface SetupRow { key: string; label: string; state: "ok" | "warn" | "bad"; text: string; test: string }
export interface SetupData {
  ready: boolean; checklist: SetupRow[]; owner: boolean;
  invite: { state: "none" | "ok" | "dead" | "unknown"; text: string; checked: number | null };
  links: { server?: string; channels?: Record<string, string> };
  /** OPS-01: who gets the error alerts; how many errors were folded into an earlier alert; failed ops-channel posts in a row */
  errors: { to: string[]; words: string; default: boolean; suppressed: number; ops_failing: number;
    /** the stored list (null = the owner, the default; [] = nobody) and, for the owner, who may be picked */
    ids: string[] | null; choices: { id: string; name: string }[] };
  /** the settings rev the error-alerts edit saves against */
  rev: string;
  /** the host (a superuser) only: the bot's invite link, servers the bot is in that aren't guilds, parked guilds */
  host: null | { invite_link: string | null; unclaimed: { id: string; name: string; missing: string[] }[]; detached: { key: string; name: string; detached: string | null }[] };
}
export interface WebhookReply { url: string | null; note: string; created: boolean }

/** An application on the officers' Applications page. `consequence` (open only) = what Accept / Decline does, from code —
 *  the same sentences as the Discord decide screen. `prior` = "Applied before: declined 12 Mar by Jonny — full on healers". */
export interface Application {
  uid: string; display_name: string; label: string; cls: string; spec: string; offspec: string | null; schedules: string[];
  answers: { label: string; answer: string }[]; availability: string | null; about: string | null; logs_url: string | null; source: string;
  status: "open" | "accepted" | "declined" | "withdrawn"; created: string; decided: string | null; decided_by: string | null; note: string | null;
  prior: string | null; role_note: string | null; history: { label: string | null; status: string; by: string | null; note: string | null; when: string | null }[];
  consequence?: { accept: string[]; decline: string[] };
}
export interface ApplicationsData { open: Application[]; decided: Application[]; recruiting: boolean; note_max: number }
/** The viewer's own application (GET /api/me → application): `text` is the whole sentence. */
export interface MyApplicationState { state: "open" | "accepted" | "declined" | "withdrawn"; label: string; sent: string; when: string | null; note: string | null; by: string | null; text: string }
export interface LeftMember { uid: string; name: string; left: string; characters: string[]; consequence: string }
