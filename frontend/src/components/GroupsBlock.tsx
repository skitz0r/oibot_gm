import { Box, Group, Tooltip } from "@mantine/core";
import type { GroupSummary } from "../api";

const OK: Record<string, string> = { green: "var(--mantine-color-green-5)", amber: "var(--mantine-color-yellow-5)", red: "var(--mantine-color-red-5)" };

/** Raid-wide cast buffs as icons only: coloured when someone brings it, greyed with a red edge when nobody does (names in the tooltips). */
export function RaidWide({ sm }: { sm: GroupSummary }) {
  return (
    <Box mt="sm">
      <Group gap={4} align="center">
        {sm.raid.map((b) => (
          <Tooltip key={b.abbr} label={`${b.name} ×${b.n} · ${b.detail} · ${b.status}`}>
            {b.art ? (
              <Box component="img" src={`/img/icon/${b.art}.jpg`} alt={b.abbr} style={{ width: 26, height: 26, borderRadius: 5, border: `2px solid ${b.n ? (OK[b.ok] || "transparent") : "var(--mantine-color-red-5)"}`, opacity: b.n ? 1 : 0.35, filter: b.n ? undefined : "grayscale(1)" }} />
            ) : (
              <Box style={{ minWidth: 30, textAlign: "center", padding: "2px 5px", borderRadius: 6, fontSize: 11, fontWeight: 700, color: b.n ? "#14181F" : "var(--mantine-color-red-5)", background: b.n ? b.colour : "transparent", border: b.n ? undefined : "2px solid var(--mantine-color-red-5)" }}>{b.abbr}</Box>
            )}
          </Tooltip>
        ))}
      </Group>
    </Box>
  );
}
