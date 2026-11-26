import { Box, Group, Stack, Text } from "@mantine/core";
import type { MemberPart, Meta } from "../../api";
import { RoleCounts, SeatLine } from "../board/shared";
import boardCss from "../board/board.module.css";

/** The locked roster as members see it (member_event_view's `rosters`): for each raid its label, then each roster as
 *  Roster N with its role counts (icon + number), its groups as the board's columns — one member per line, the spec icon
 *  alone, the confirmation mark only when the raid shows marks — and the joiners the lock left out on one line under the
 *  server's word (rc.BANK_WORDS). Read only: nothing here moves, pins or asks. */
export function MemberRoster({ parts, meta }: { parts: MemberPart[]; meta: Meta }) {
  return (
    <Stack gap="lg">
      {parts.map((part) => (
        <Stack key={part.key} gap="sm">
          <Text fw={700}>{part.label}</Text>
          {part.rosters.map((r) => {
            const counts: Record<string, number> = {};
            r.groups.flat().forEach((s) => { counts[s.role] = (counts[s.role] || 0) + 1; });
            return (
              <Box key={r.n}>
                <Group justify="space-between" mb={4} wrap="wrap">
                  <Text size="sm" fw={700}>{`Roster ${r.n}`}</Text>
                  <RoleCounts meta={meta} counts={counts} size={16} />
                </Group>
                <Box className={boardCss.groups}>
                  {r.groups.map((g, gi) => g.length === 0 ? null : (
                    <Box key={gi} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
                      <Text size="sm" fw={700} mb={4}>{`Group ${gi + 1}`}</Text>
                      {g.map((s) => <SeatLine key={`${s.display_name}:${s.character}`} meta={meta} s={s} mark={part.marks ? (s.answer ?? null) : undefined} />)}
                    </Box>
                  ))}
                </Box>
              </Box>
            );
          })}
          {part.not_rostered.length > 0 && (
            <Text size="sm"><Text span size="sm" fw={700}>{meta.run_words.bank.locked}</Text> · {part.not_rostered.join(", ")}</Text>
          )}
        </Stack>
      ))}
    </Stack>
  );
}
