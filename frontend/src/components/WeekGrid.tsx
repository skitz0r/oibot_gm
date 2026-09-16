import { useEffect, useMemo, useRef, useState } from "react";
import { Box, Button, Group, SegmentedControl, Text } from "@mantine/core";
import type { WeekBlock } from "../api";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const N = 48;
type Level = "" | "preferred" | "available";
const PREF = "#2E9E6B", AVAIL = "#B8892A";

function toState(blocks: WeekBlock[]): Level[][] {
  const s: Level[][] = Array.from({ length: 7 }, () => Array(N).fill(""));
  blocks.forEach((b) => { for (let i = Math.floor(b.start / 30); i < Math.ceil(b.end / 30); i++) s[b.day][i] = b.level; });
  return s;
}
export function toBlocks(state: Level[][]): WeekBlock[] {
  const out: WeekBlock[] = [];
  for (let d = 0; d < 7; d++) {
    let i = 0;
    while (i < N) {
      const lv = state[d][i];
      if (!lv) { i++; continue; }
      let j = i;
      while (j < N && state[d][j] === lv) j++;
      out.push({ day: d, start: i * 30, end: j * 30, level: lv });
      i = j;
    }
  }
  return out;
}
const hourLabel = (h: number) => (h === 0 ? "12a" : h < 12 ? `${h}a` : h === 12 ? "12p" : `${h - 12}p`);

/** Drag-to-select availability grid. Landscape (days as rows) on wide screens, transposed on phones. */
export function WeekGrid({ value, onChange, raidWindows, tz }: { value: WeekBlock[]; onChange: (b: WeekBlock[]) => void; raidWindows: { slot: string; name: string }[]; tz: string }) {
  const [state, setState] = useState<Level[][]>(() => toState(value));
  const [mode, setMode] = useState<"preferred" | "available" | "clear">("preferred");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [portrait, setPortrait] = useState(window.innerWidth < 760);
  const drag = useRef<{ d: number; i: number } | null>(null);

  useEffect(() => setState(toState(value)), [value]);
  useEffect(() => { const f = () => setPortrait(window.innerWidth < 760); window.addEventListener("resize", f); return () => window.removeEventListener("resize", f); }, []);

  const raidCells = useMemo(() => {
    const WD: Record<string, number> = { mon: 0, tue: 1, wed: 2, thu: 3, fri: 4, sat: 5, sun: 6 };
    const s = new Set<string>();
    raidWindows.forEach((r) => {
      const m = /^(\w{3})\w*\s+(\d{1,2}):(\d{2})$/.exec(r.slot.trim());
      if (!m) return;
      const d = WD[m[1].toLowerCase()], start = Math.floor((+m[2] * 60 + +m[3]) / 30);
      for (let i = start; i < Math.min(N, start + 6); i++) s.add(`${d}:${i}`);
    });
    return s;
  }, [raidWindows]);

  function apply(cells: Set<string>) {
    const next = state.map((r) => [...r]);
    cells.forEach((k) => { const [d, i] = k.split(":").map(Number); next[d][i] = mode === "clear" ? "" : mode; });
    setState(next);
    onChange(toBlocks(next));
  }
  function rect(a: { d: number; i: number }, b: { d: number; i: number }) {
    const s = new Set<string>();
    for (let d = Math.min(a.d, b.d); d <= Math.max(a.d, b.d); d++) for (let i = Math.min(a.i, b.i); i <= Math.max(a.i, b.i); i++) s.add(`${d}:${i}`);
    return s;
  }
  function cellAt(ev: React.PointerEvent): { d: number; i: number } | null {
    const el = document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null;
    if (!el || el.dataset.d === undefined) return null;
    return { d: +el.dataset.d, i: +el.dataset.i! };
  }
  const onDown = (ev: React.PointerEvent) => { const c = cellAt(ev); if (!c) return; drag.current = c; setSel(rect(c, c)); ev.preventDefault(); };
  const onMove = (ev: React.PointerEvent) => { if (!drag.current) return; const c = cellAt(ev); if (c) setSel(rect(drag.current, c)); };
  const onUp = () => { if (!drag.current) return; drag.current = null; if (sel.size) apply(sel); setSel(new Set()); };

  const hours = state.flat().filter(Boolean).length / 2, pref = state.flat().filter((x) => x === "preferred").length / 2;
  const cell = (d: number, i: number) => {
    const lv = state[d][i];
    const bg = lv === "preferred" ? PREF : lv === "available" ? AVAIL : "var(--mantine-color-slate-6)";
    return (
      <Box key={`${d}:${i}`} data-d={d} data-i={i} title={`${DAYS[d]} ${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`}
        style={{ height: portrait ? 12 : 24, borderRadius: 4, background: bg, cursor: "crosshair",
          outline: sel.has(`${d}:${i}`) ? "2px solid var(--mantine-color-slate-0)" : undefined, outlineOffset: -2,
          boxShadow: raidCells.has(`${d}:${i}`) ? "inset 0 -3px 0 var(--mantine-color-teal-4)" : undefined }} />
    );
  };
  const lab = (t: string, key: string) => <Text key={key} size="xs" c="dimmed" ff="monospace" style={{ alignSelf: "center" }}>{t}</Text>;

  return (
    <Box>
      <Group justify="space-between" mb="sm" wrap="wrap">
        <Group gap="sm">
          <SegmentedControl value={mode} onChange={(v) => setMode(v as typeof mode)} size="sm"
            data={[{ value: "preferred", label: "Preferred" }, { value: "available", label: "Available" }, { value: "clear", label: "Clear" }]}
            styles={{ indicator: { background: mode === "preferred" ? PREF : mode === "available" ? AVAIL : "var(--mantine-color-slate-5)" } }} />
          <Button size="xs" variant="default" onClick={() => {
            const next = state.map((r) => [...r]);
            for (let d = 0; d < 5; d++) for (let i = 36; i < 44; i++) if (!next[d][i]) next[d][i] = "available";
            for (let d = 5; d < 7; d++) for (let i = 24; i < 44; i++) if (!next[d][i]) next[d][i] = "available";
            setState(next); onChange(toBlocks(next));
          }}>Seed usual times</Button>
        </Group>
        <Group gap="md">
          <Legend colour={PREF} label="preferred" /><Legend colour={AVAIL} label="available" /><Legend colour="var(--mantine-color-slate-6)" label="unavailable" />
          <Text size="xs" c="dimmed">{tz} · {hours} h marked · {pref} h preferred</Text>
        </Group>
      </Group>
      <Box onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerLeave={onUp} style={{ display: "grid", gap: 2, userSelect: "none",
        gridTemplateColumns: portrait ? "44px repeat(7, minmax(0, 1fr))" : "44px repeat(48, minmax(0, 1fr))" }}>
        <Box />
        {!portrait
          ? Array.from({ length: 24 }, (_, h) => <Text key={`h${h}`} size="xs" c="dimmed" ff="monospace" style={{ gridColumn: "span 2" }}>{h % 2 === 0 ? hourLabel(h) : ""}</Text>)
          : DAYS.map((d) => <Text key={d} size="xs" c="dimmed" ta="center">{d}</Text>)}
        {!portrait
          ? DAYS.flatMap((day, d) => [lab(day, `d${d}`), ...Array.from({ length: N }, (_, i) => cell(d, i))])
          : Array.from({ length: N }, (_, i) => [lab(i % 4 === 0 ? hourLabel(i / 2) : "", `i${i}`), ...DAYS.map((_, d) => cell(d, i))]).flat()}
      </Box>
      <Text size="xs" c="dimmed" mt={6}>Drag to select, then it's marked with the mode above. Underlines are scheduled raids; a run only counts you in when its whole window is inside your marked time.</Text>
    </Box>
  );
}

function Legend({ colour, label }: { colour: string; label: string }) {
  return <Group gap={6}><Box style={{ width: 12, height: 12, borderRadius: 3, background: colour }} /><Text size="xs" c="dimmed">{label}</Text></Group>;
}
