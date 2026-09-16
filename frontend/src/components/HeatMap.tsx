import { useMemo } from "react";
import { Box, Text, Tooltip } from "@mantine/core";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const hourLabel = (h: number) => (h === 0 ? "12a" : h < 12 ? `${h}a` : h === 12 ? "12p" : `${h - 12}p`);

/** Everyone's availability: per half-hour, how many mains marked it (green when mostly preferred). */
export function HeatMap({ heat, n }: { heat: [number, number][][]; n: number }) {
  const cells = useMemo(() => heat.map((row, d) => row.map(([p, a], i) => {
    const tot = p + a;
    const rgb = p >= a ? "46,158,107" : "184,137,42";
    return (
      <Tooltip key={`${d}:${i}`} label={`${DAYS[d]} ${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"} — ${p} preferred, ${a} available`} openDelay={300}>
        <Box style={{ height: 18, borderRadius: 2, background: tot ? `rgba(${rgb}, ${(0.15 + 0.85 * (tot / (n || 1))).toFixed(2)})` : "var(--mantine-color-slate-6)" }} />
      </Tooltip>
    );
  })), [heat, n]);
  return (
    <Box style={{ overflowX: "auto" }}>
      <Box style={{ display: "grid", gridTemplateColumns: "44px repeat(48, minmax(0, 1fr))", gap: 1, minWidth: 560 }}>
        <Box />
        {Array.from({ length: 24 }, (_, h) => <Text key={h} size="xs" c="dimmed" ff="monospace" style={{ gridColumn: "span 2", fontSize: 10 }}>{h % 2 === 0 ? hourLabel(h) : ""}</Text>)}
        {cells.map((row, d) => [<Text key={`l${d}`} size="xs" c="dimmed" style={{ alignSelf: "center" }}>{DAYS[d]}</Text>, ...row])}
      </Box>
    </Box>
  );
}
