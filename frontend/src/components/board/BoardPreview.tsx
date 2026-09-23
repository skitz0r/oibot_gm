import { Box, Group, Stack, Text } from "@mantine/core";
import type { Board, Meta, SplitReason } from "../../api";
import { RaidWide } from "../GroupsBlock";
import { Aura, RosterHeader, SeatLine, SplitReasonLine, splitBank } from "./shared";
import css from "../../pages/rosters.module.css";

/** Read-only rendering of a board (the split preview): the same groups, badges and raid-wide row as the builder. */
export function BoardPreview({ meta, board, reason }: { meta: Meta; board: Board; reason?: SplitReason | null }) {
  const { left, rest } = splitBank(board, board.bank);
  return (
    <Stack gap="md">
      {board.rosters.map((r) => (
        <Box key={r.n}>
          <RosterHeader meta={meta} r={r} />
          <Box className={css.groups}>
            {r.groups.map((g, gi) => {
              const sm = r.summary.groups[gi];
              return (
                <Box key={gi} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
                  <Group justify="space-between" mb={4}><Text size="sm" fw={700}>Group {gi + 1}</Text><Text size="xs" c="dimmed">{g.length}/{board.group_size}</Text></Group>
                  {g.map((s) => <SeatLine key={s.display_name} meta={meta} s={s} />)}
                  {sm && (
                    <>
                      <Group gap={4} mt={8}>{sm.present.map((a) => <Aura key={a.abbr} a={a} who={a.who} />)}{sm.missing.map((a) => <Aura key={`m${a.abbr}`} a={a} missing />)}</Group>
                      {sm.picks.length > 0 && <Text size="xs" c="dimmed" mt={4}>totems: {sm.picks.join(" · ")}</Text>}
                    </>
                  )}
                </Box>
              );
            })}
          </Box>
          <RaidWide sm={r.summary} />
        </Box>
      ))}
      {left.length > 0 && (
        <Box p="sm" style={{ border: "1px dashed var(--mantine-color-slate-5)", borderRadius: 8 }}>
          <Text size="sm" fw={700} mb={4}>Leftovers ({left.length})</Text>
          {left.map((s) => <SeatLine key={s.display_name} meta={meta} s={s} />)}
          {reason && <Box mt={6}><SplitReasonLine meta={meta} r={reason} /></Box>}
          {board.another && <Text size="xs" c="dimmed" mt={4}>{board.another}</Text>}
        </Box>
      )}
      {rest.length > 0 && <Text size="xs" c="dimmed">Bench: {rest.map((b) => b.character).join(", ")}</Text>}
    </Stack>
  );
}
