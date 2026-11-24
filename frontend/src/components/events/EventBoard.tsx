import { useState } from "react";
import { Button, Group, Loader, Stack, Text, Tooltip } from "@mantine/core";
import { api, type Box as ConfirmBoxShape, type EventView, type Meta } from "../../api";
import { fail, ok } from "../Page";
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
 *  for a one-raid event), the facts Rosters / Placed / Short, then the picked raid's board, drawn by BoardView
 *  `variant="event"`. Before the lock it is the OFFICERS' board (the owner's tweaks C): who they placed, everyone else in
 *  Not placed — nothing is solved until **Auto-fill groups** (beside Not placed) is pressed: POST /api/run/{key}/autofill
 *  answers at once, the button then shows the server's working words until the board updates (`run:` reload), and an
 *  auto-fill that wrote nothing leaves its server sentence under it. The footer sentence and Undo my moves (the server's
 *  box, its count) while the sheet is open. A locked event shows its roster; a finished one the board as it was locked,
 *  read only. The page places nobody: every move goes to the server and the reload draws its answer. */
export function EventBoard({ meta, view, onReload }: { meta: Meta; view: EventView; onReload: () => void }) {
  const parts = view.parts.filter((p) => p.sheet?.board);
  const [pick, setPick] = useState<string | null>(null);
  const [ask, dialog] = useConfirmBox(["run:"]);
  const [pressing, setPressing] = useState(false);
  const part = parts.find((p) => p.key === pick) || parts[0];
  if (!part || !part.sheet) return null;
  const words = meta.page_words;
  const finished = view.state === "done" || view.state === "cancelled";
  const open = part.board_state !== "locked";
  const short = Object.keys(part.short || {}).length > 0;
  const url = `/api/run/${encodeURIComponent(part.key)}/layout`;
  const af = open && !finished ? part.autofill : null;

  /** Auto-fill groups: the route answers at once (a refusal is its sentence); the reload draws the server's working line. */
  async function autofill() {
    setPressing(true);
    try { await api.post<{ message: string }>(`/api/run/${encodeURIComponent(part.key)}/autofill`, {}); } catch (e) { fail(e); }
    finally { setPressing(false); onReload(); }
  }

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
        {part.placed_words && <div><dt>Placed</dt><dd>{part.placed_words}</dd></div>}
        {/* a finished one-raid event says Rostered once, in Details */}
        {part.rostered_words && !(finished && parts.length === 1) && <div><dt>{"Rostered"}</dt><dd>{part.rostered_words}</dd></div>}
        {short && <div><dt>Short</dt><dd><ShortRoles meta={meta} roles={part.short} /></dd></div>}
      </dl>
      <BoardView key={part.key} variant="event" e={part.sheet} meta={meta} onBoard={() => onReload()} onReload={onReload}
        bankAction={af ? (
          <Stack gap={4} mt={6}>
            <Button variant="light" size="sm" mih={44} disabled={af.working || pressing} onClick={autofill}
              leftSection={af.working || pressing ? <Loader size="xs" /> : undefined}>{words.autofill_button}</Button>
            {af.words && <Text size="xs" c={af.failed ? "red.4" : "dimmed"} role="status">{af.words}</Text>}
          </Stack>
        ) : undefined} />
      {open && !finished && (
        <Group justify="space-between" wrap="wrap" gap="sm" mt="md">
          <Text size="sm" c="dimmed">{words.board_footer}</Text>
          {part.undo_box ? <Button variant="default" size="sm" onClick={undo}>Undo my moves</Button> : part.undo_why && (
            <Tooltip label={part.undo_why} multiline w={260}>
              <span tabIndex={0} aria-label={part.undo_why}><Button variant="default" size="sm" disabled style={{ pointerEvents: "none" }}>Undo my moves</Button></span>
            </Tooltip>
          )}
        </Group>
      )}
      {dialog}
    </ViewSection>
  );
}
