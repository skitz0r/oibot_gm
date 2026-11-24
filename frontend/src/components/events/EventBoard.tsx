import { useState } from "react";
import { Alert, Box, Button, Group, Loader, Text } from "@mantine/core";
import { api, type Box as ConfirmBoxShape, type EventView, type Meta } from "../../api";
import { ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { BoardView } from "../board/BoardView";
import { boxOf } from "./shared";
import css from "../../pages/listpage.module.css";

/** Role icon + number per short role, red (the Iconography rule: never "2 tanks"); the role's name is the tooltip. */
export function ShortRoles({ meta, roles }: { meta: Meta; roles: Record<string, number> }) {
  return (
    <Group gap={10} wrap="wrap">
      {meta.roles.filter((r) => roles[r]).map((r) => (
        <Group key={r} gap={4} wrap="nowrap">
          <GameIcon meta={meta} kind="role" id={r} size={15} title={r} />
          <Text span fw={700} c="red.4" style={{ fontVariantNumeric: "tabular-nums" }}>{roles[r]}</Text>
        </Group>
      ))}
    </Group>
  );
}

/** The event's Board (Main.dc.html): the raid switch `{time} · {Raid}` (a row of buttons that wraps on a phone; none
 *  for a one-raid event), the facts Rosters / Placed / Short, then the picked raid's board — the SERVER's picture of
 *  where the lock would place everyone now (event_board), drawn by BoardView `variant="event"`. While the server places
 *  everyone again (`board_state` working) the picture it sends — the last one with the officers' moves on it — is drawn
 *  dimmed under its words; `failed` prints the server's sentence over the officers' layout. The footer sentence and Undo
 *  my moves (the server's box, its count) while the sheet is open. A locked event shows its roster; a finished one the
 *  board as it was locked, read only. The page places nobody: every move goes to the server and the reload draws its answer. */
export function EventBoard({ meta, view, onReload }: { meta: Meta; view: EventView; onReload: () => void }) {
  const parts = view.parts.filter((p) => p.sheet?.board);
  const [pick, setPick] = useState<string | null>(null);
  const [ask, dialog] = useConfirmBox(["run:"]);
  const part = parts.find((p) => p.key === pick) || parts[0];
  if (!part || !part.sheet) return null;
  const words = meta.page_words;
  const finished = view.state === "done" || view.state === "cancelled";
  const open = part.board_state !== "locked";
  const short = Object.keys(part.short || {}).length > 0;
  const url = `/api/run/${encodeURIComponent(part.key)}/layout`;

  /** Undo my moves: the server's box (its count of this officer's moves); the press sends that box's fp as `seen`, so a
   *  board that changed while it was open brings the fresh box back. */
  async function undo() {
    if (!part.undo_box) return;
    let said = "";
    const done = await ask({
      box: part.undo_box,
      refresh: async (): Promise<ConfirmBoxShape> => {
        try { return (await api.post<{ box: ConfirmBoxShape }>(url, { undo: true, preview: true })).box; } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      },
      act: async (fp) => {
        try { said = (await api.post<{ message?: string }>(url, { undo: true, seen: fp })).message || ""; return null; }
        catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      },
    });
    if (done) { if (said) ok(said); onReload(); }
  }

  const raidSwitch = parts.length > 1 ? (
    <Group gap="xs" wrap="wrap" role="group" aria-label="Which raid">
      {parts.map((p) => (
        <Button key={p.key} size="sm" variant={p.key === part.key ? "light" : "default"} aria-pressed={p.key === part.key}
          onClick={() => setPick(p.key)}>{p.label}</Button>
      ))}
    </Group>
  ) : undefined;

  return (
    <ViewSection title={finished ? "Board, as it was locked" : "Board"} action={raidSwitch}>
      <dl className={css.facts} style={{ marginBottom: 12 }}>
        <div><dt>Rosters</dt><dd>{part.rosters_words}</dd></div>
        <div><dt>Placed</dt><dd>{part.placed_words}</dd></div>
        {short && <div><dt>Short</dt><dd><ShortRoles meta={meta} roles={part.short} /></dd></div>}
      </dl>
      {part.board_state === "working" && (
        <Group gap="xs" wrap="nowrap" mb="xs" role="status"><Loader size="xs" /><Text size="sm" c="dimmed">{words.board_working}</Text></Group>
      )}
      {part.board_state === "failed" && part.board_error && <Alert color="red" variant="light" p="xs" mb="xs"><Text size="sm">{part.board_error}</Text></Alert>}
      <Box style={{ opacity: part.board_state === "working" ? 0.6 : 1, transition: "opacity 150ms" }}>
        <BoardView key={part.key} variant="event" e={part.sheet} meta={meta} onBoard={() => onReload()} onReload={onReload} />
      </Box>
      {open && !finished && (
        <Group justify="space-between" wrap="wrap" gap="sm" mt="md">
          <Text size="sm" c="dimmed">{words.board_footer}</Text>
          {part.undo_box && <Button variant="default" size="sm" onClick={undo}>Undo my moves</Button>}
        </Group>
      )}
      {dialog}
    </ViewSection>
  );
}
