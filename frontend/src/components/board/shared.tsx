import { Box, Group, Text, Tooltip } from "@mantine/core";
import type { Board, BoardRoster, Meta, Seat, Sheet, Signup } from "../../api";
import { GameIcon } from "../Icons";
import { classColour } from "../../theme";

export type Act = (k: string, p: string, b?: unknown) => Promise<void>;
export type OnBoard = (key: string, board: Board, needs?: Sheet["needs"], rev?: string) => void;
/** Shared between the board and the page: a refresh must not re-render the board under a drag in progress. */
export const live = { dragging: false };

export const ANSWER: Record<string, { mark: string; colour: string; label: string }> = { yes: { mark: "✓", colour: "var(--mantine-color-teal-4)", label: "confirmed" }, no: { mark: "✗", colour: "var(--mantine-color-red-5)", label: "can't make it" }, expired: { mark: "⌛", colour: "var(--mantine-color-slate-3)", label: "no answer" } };

/** One member line: spec icon (role + spec) + character + display name; an optional confirmation mark on the left. */
export function SeatLine({ meta, s, right, mark }: { meta: Meta; s: Seat | Signup; right?: React.ReactNode; mark?: string | null }) {
  const a = mark !== undefined && mark !== null ? ANSWER[mark] : null;
  return (
    <Group gap={6} wrap="nowrap" style={{ lineHeight: 1.8, minWidth: 0 }}>
      {mark !== undefined && <Tooltip label={a ? a.label : "waiting for confirmation"}><Text span size="xs" style={{ width: 14, textAlign: "center", color: a ? a.colour : "var(--mantine-color-slate-3)" }}>{a ? a.mark : "⏳"}</Text></Tooltip>}
      <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={`${s.cls} ${s.spec} · ${s.role}`} />
      <Text size="sm" fw={600} c={classColour(meta, s.cls)} truncate>{s.character}</Text>
      <Text size="xs" c="dimmed" truncate>{s.display_name}</Text>
      {right}
    </Group>
  );
}

/** Role counts for a roster: role icon + number, nothing else. */
export function RosterHeader({ meta, r, title }: { meta: Meta; r: BoardRoster; title?: string }) {
  const counts = Object.fromEntries(meta.roles.map((role) => [role, 0])) as Record<string, number>;
  r.groups.flat().forEach((s) => { counts[s.role] = (counts[s.role] || 0) + 1; });
  return (
    <Group justify="space-between" mb={4} wrap="wrap">
      <Group gap="sm"><Text size="sm" fw={700}>{title || `Roster ${r.n}`}</Text>{r.synergy !== null && r.synergy !== undefined && <Text size="xs" c="dimmed">synergy {r.synergy}</Text>}</Group>
      <RoleCounts meta={meta} counts={counts} size={16} />
    </Group>
  );
}

export function RoleCounts({ meta, counts, size = 16 }: { meta: Meta; counts: Record<string, number>; size?: number }) {
  return (
    <Group gap="md">{meta.roles.map((role) => <Group key={role} gap={4} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={size} title={role} /><Text size={size > 16 ? "sm" : "xs"} style={{ fontVariantNumeric: "tabular-nums" }}>{counts[role] || 0}</Text></Group>)}</Group>
  );
}

export function Aura({ a, missing, who }: { a: { abbr: string; colour: string; art: string | null; name: string; in_run?: boolean; covered?: boolean }; missing?: boolean; who?: string }) {
  const fixable = missing && a.in_run;  // a provider is rostered in another group: regrouping can fix it
  const tip = `${a.name}${missing ? (fixable ? " — wanted here; the provider is in another group" : " — wanted here; nobody in this run brings it") : who ? ` — ${who}` : ""}`;
  const edge = fixable ? "var(--mantine-color-red-5)" : a.covered ? "var(--mantine-color-teal-4)" : "transparent";
  return (
    <Tooltip label={tip}>
      {a.art ? <Box component="img" src={`/img/icon/${a.art}.jpg`} alt={a.abbr} style={{ width: 26, height: 26, borderRadius: 5, border: `2px solid ${edge}`, opacity: missing ? 0.35 : a.covered ? 0.75 : 1, filter: missing ? "grayscale(1)" : undefined }} />
        : <Box style={{ minWidth: 30, textAlign: "center", padding: "2px 5px", borderRadius: 6, fontSize: 11, fontWeight: 700, color: missing ? (fixable ? "var(--mantine-color-red-5)" : "var(--mantine-color-slate-3)") : "#14181F", background: missing ? "transparent" : a.colour, border: `2px solid ${missing ? (fixable ? "var(--mantine-color-red-5)" : "var(--mantine-color-slate-5)") : "transparent"}` }}>{a.abbr}</Box>}
    </Tooltip>
  );
}

export type BankSort = "signup" | "spec" | "role";
/** The bank in the order the officer asked for: who answered first, by class + spec, or by role (the profile's role order). */
export function sortBank(bank: Seat[], by: BankSort, roleOrder: string[]): Seat[] {
  const rows = [...bank];
  const rank = (role: string) => { const i = roleOrder.indexOf(role); return i < 0 ? 9 : i; };
  if (by === "signup") return rows.sort((a, b) => (a.signed_at || "").localeCompare(b.signed_at || "") || a.character.localeCompare(b.character));
  if (by === "spec") return rows.sort((a, b) => a.cls.localeCompare(b.cls) || a.spec.localeCompare(b.spec) || a.character.localeCompare(b.character));
  return rows.sort((a, b) => rank(a.role) - rank(b.role) || a.cls.localeCompare(b.cls) || a.character.localeCompare(b.character));
}

export function chunk(flat: string[][], n: number): string[][][] {
  const out: string[][][] = [];
  for (let i = 0; i < flat.length; i += n) out.push(flat.slice(i, i + n));
  return out;
}

/** "{raid} · {when}" — how a run is named to people (never its key). */
export const runName = (e: { raid: string; when: string }) => `${e.raid} · ${e.when}`;
