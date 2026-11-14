import { useState } from "react";
import { Box, SegmentedControl, Stack, Text } from "@mantine/core";
import { useElementSize } from "@mantine/hooks";
import type { PriceHistory } from "../api";

/** Layout in pixels: the left gutter holds the two price labels, the bottom the two time labels (a second row when they
 *  would meet). The plot is drawn at the container's real width, so text never shrinks on a phone. */
const H = 180;
const GUTTER = 92;
const TOP = 10;
const BOTTOM = 40;
const RIGHT = 8;
/** A label's width at 11 px, near enough to keep two labels apart (the words are the server's, measured by length). */
const textWidth = (words: string) => words.length * 6.2;

const LINE = "var(--mantine-color-teal-4)";
const GRID = "var(--mantine-color-slate-5)";
const INK = "var(--mantine-color-dimmed)";
const CROSS = "var(--mantine-color-slate-3)";
const RING = "var(--mantine-color-body)";

/** A watched item's lowest price over one span (price watch §4.3, C5, C12, C17), drawn by the site itself as inline SVG:
 *  a step line (a price holds until the next point), broken where nothing was listed (a gap, never a zero), a faint band
 *  from the lowest to the highest on a folded day, the span's highest and lowest as the two price labels and the first and
 *  last point's time as the time labels — every label the server's words; `t` is only the x position. A hover or the
 *  arrow keys read one point out (its time and price, the server's words). With fewer than two points there is no frame:
 *  the page prints the server's sentences alone. The span switch's labels are the server's. */
export function PriceChart({ data, onSpan, busy }: { data: PriceHistory; onSpan: (span: string) => void; busy?: boolean }) {
  const { ref, width } = useElementSize();
  const [active, setActive] = useState<number | null>(null);
  const pts = data.points;
  const label = data.sentences.join(" ") || data.name;

  const switcher = pts.length > 0 && data.spans.length > 1 && (
    <SegmentedControl size="xs" value={data.span} onChange={onSpan} disabled={busy} aria-label="How far back"
      data={data.spans.map((s) => ({ value: s.id, label: s.label }))} style={{ alignSelf: "flex-start" }} />
  );
  if (pts.length < 2) return switcher ? <Stack gap="xs">{switcher}</Stack> : null;

  const lows = pts.flatMap((p) => (p.low == null ? [] : [p.low]));
  const highs = pts.flatMap((p) => (p.high == null ? (p.low == null ? [] : [p.low]) : [p.high]));
  const lo = Math.min(...lows, ...(data.low == null ? [] : [data.low]));
  const hi = Math.max(...highs, ...(data.high == null ? [] : [data.high]));
  const flat = !(hi > lo);
  const pad = flat ? Math.max(Math.abs(lo) * 0.1, 1) : (hi - lo) * 0.12;  // a flat line sits mid-height
  const yMin = lo - pad;
  const yMax = hi + pad;

  const t0 = pts[0].t;
  const t1 = Math.max(pts[pts.length - 1].t, Math.floor(Date.now() / 1000), t0 + 1);  // the last price holds until now
  const plotW = Math.max(width - GUTTER - RIGHT, 40);
  const plotH = H - TOP - BOTTOM;
  const x = (t: number) => GUTTER + ((t - t0) / (t1 - t0)) * plotW;
  const y = (v: number) => TOP + (1 - (v - yMin) / (yMax - yMin)) * plotH;
  const next = (i: number) => (i + 1 < pts.length ? pts[i + 1].t : t1);

  let path = "";
  let drawing = false;
  pts.forEach((p, i) => {
    if (p.low == null) { drawing = false; return; }
    const yy = y(p.low).toFixed(1);
    path += drawing ? ` V${yy}` : ` M${x(p.t).toFixed(1)},${yy}`;
    path += ` H${x(next(i)).toFixed(1)}`;
    drawing = true;
  });
  const bands = pts.flatMap((p, i) => (p.low != null && p.high != null && p.high > p.low
    ? [<rect key={p.t} x={x(p.t)} width={Math.max(x(next(i)) - x(p.t), 1)} y={y(p.high)} height={y(p.low) - y(p.high)} fill={LINE} fillOpacity={0.18} />]
    : []));

  const first = pts[0];
  const last = pts[pts.length - 1];
  const wFirst = textWidth(first.when);
  const wLast = textWidth(last.when);
  const xLast = Math.min(Math.max(x(last.t), GUTTER + wLast), GUTTER + plotW);  // the last time ends at its point, inside the frame
  const twoRows = xLast - wLast < GUTTER + wFirst + 12;  // the two times would meet: the last goes on a second row
  const yHigh = y(hi);
  const yLow = y(lo);

  const pick = (clientX: number, box: DOMRect) => {
    const t = t0 + ((clientX - box.left - GUTTER) / plotW) * (t1 - t0);
    let i = 0;
    while (i + 1 < pts.length && pts[i + 1].t <= t) i++;
    setActive(i);
  };
  const onKey = (e: React.KeyboardEvent) => {
    const at = active ?? pts.length - 1;
    const to = e.key === "ArrowLeft" ? at - 1 : e.key === "ArrowRight" ? at + 1 : e.key === "Home" ? 0 : e.key === "End" ? pts.length - 1 : null;
    if (to === null) return;
    e.preventDefault();
    setActive(Math.min(Math.max(to, 0), pts.length - 1));
  };
  const shown = active !== null ? pts[active] : null;

  return (
    <Stack gap="xs">
      {switcher}
      <Text size="xs" c={shown ? undefined : "dimmed"} aria-live="polite" style={{ minHeight: 18 }}>
        {shown ? `${shown.when} · ${shown.words}` : " "}
      </Text>
      <Box ref={ref} style={{ width: "100%" }}>
        {width > 0 && (
          <svg width={width} height={H} role="img" aria-label={label} tabIndex={0} style={{ display: "block", touchAction: "pan-y" }}
            onPointerMove={(e) => pick(e.clientX, e.currentTarget.getBoundingClientRect())}
            onPointerDown={(e) => pick(e.clientX, e.currentTarget.getBoundingClientRect())}
            onPointerLeave={() => setActive(null)} onKeyDown={onKey}
            onFocus={() => setActive((a) => a ?? pts.length - 1)} onBlur={() => setActive(null)}>
            <title>{label}</title>
            <line x1={GUTTER} x2={GUTTER + plotW} y1={yHigh} y2={yHigh} stroke={GRID} strokeDasharray="3 4" />
            {!flat && <line x1={GUTTER} x2={GUTTER + plotW} y1={yLow} y2={yLow} stroke={GRID} strokeDasharray="3 4" />}
            <text x={GUTTER - 8} y={yHigh} dy="0.35em" textAnchor="end" fontSize={11} fill={INK}>{data.high_words}</text>
            {!flat && <text x={GUTTER - 8} y={yLow} dy="0.35em" textAnchor="end" fontSize={11} fill={INK}>{data.low_words}</text>}
            {bands}
            <path d={path} fill="none" stroke={LINE} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            <text x={GUTTER} y={TOP + plotH + 16} fontSize={11} fill={INK}>{first.when}</text>
            {last.when !== first.when && (
              <text x={twoRows ? GUTTER : xLast} y={TOP + plotH + (twoRows ? 32 : 16)} textAnchor={twoRows ? "start" : "end"} fontSize={11} fill={INK}>{last.when}</text>
            )}
            {shown && (
              <g pointerEvents="none">
                <line x1={x(shown.t)} x2={x(shown.t)} y1={TOP} y2={TOP + plotH} stroke={CROSS} strokeWidth={1} />
                {shown.low != null && <circle cx={x(shown.t)} cy={y(shown.low)} r={4.5} fill={LINE} stroke={RING} strokeWidth={2} />}
              </g>
            )}
          </svg>
        )}
      </Box>
    </Stack>
  );
}
