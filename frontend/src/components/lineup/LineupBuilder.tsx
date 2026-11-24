import { useState } from "react";
import { ActionIcon, Button, Select, Text, Tooltip } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconArrowDown, IconArrowLeft, IconArrowRight, IconArrowUp, IconMinus, IconPlus, IconX } from "@tabler/icons-react";
import { clock12, minutesOf } from "../WhenPicker";
import css from "./lineup.module.css";

/** One step of a lineup: a raid, its rosters (1–4), its start in minutes after the first step's (step 1 = 0). */
export type LineupEntry = { raid: string; rosters: number; starts: number };
/** A raid the palette offers: its id, name, size (seats of one roster) and length in hours (the next step's default start). */
export type PaletteRaid = { id: string; name: string; size: number; hours: number };

const ADD = "Add";
const MAX_ROSTERS = 4;
const HEAD_H = 176;  // a step's head (title, Starts, Rosters): every step's nodes start at the same height, so connectors meet them
const NODE_H = 64;
const NODE_GAP = 10;
const LINK_W = 46;

/** Each step from `from` on starts when the step before it ends (C12: the previous start + the previous raid's length);
 *  the first step starts at 0. */
function chain(list: LineupEntry[], raids: PaletteRaid[], from: number): LineupEntry[] {
  const len = (rid: string) => Math.round((raids.find((r) => r.id === rid)?.hours || 0) * 60) || 60;
  const out = list.map((e) => ({ ...e }));
  for (let i = Math.max(0, from); i < out.length; i++) out[i].starts = i === 0 ? 0 : out[i - 1].starts + len(out[i - 1].raid);
  return out;
}

/** '3.5 hours after the start' — a step's start when the lineup has no clock of its own yet. */
function afterWords(min: number): string {
  const h = Math.round((min / 60) * 100) / 100;
  return `${h} hour${h === 1 ? "" : "s"} after the start`;
}

/** The nodes of one step: one per roster — '{Raid}' + '{n} seats', or '{Raid} · roster {k}' when there are several. */
export function StepNodes({ name, rosters, seats, pad }: { name: string; rosters: number; seats: number | null; pad: number }) {
  return (
    <div className={css.nodes} style={{ paddingTop: pad }}>
      {Array.from({ length: rosters }, (_, k) => (
        <div key={k} className={css.node}>
          <b>{name}</b>{rosters > 1 ? ` · roster ${k + 1}` : ""}{seats !== null && <><br /><span className={css.seats}>{seats} seats</span></>}
        </div>
      ))}
    </div>
  );
}

/** The connectors between two steps: one curve from each node of the step before to each node of this step — sideways
 *  on a desktop (the nodes' centres, under the fixed head), downwards on a phone (the nodes side by side). `dashed` = to
 *  the empty slot. */
export function Link({ from, to, height, phone, dashed, head = HEAD_H }: { from: number; to: number; height: number; phone: boolean; dashed?: boolean; head?: number }) {
  const stroke = dashed ? "var(--mantine-color-slate-4)" : "var(--mantine-color-teal-6)";
  if (phone) {
    const x = (k: number, n: number) => ((k + 0.5) / n) * 100;
    const paths = dashed ? [`M${x(0, 1)} 0 V36`] : Array.from({ length: from }, (_, a) => Array.from({ length: to }, (_, b) =>
      `M${x(a, from)} 0 C${x(a, from)} 18 ${x(b, to)} 18 ${x(b, to)} 36`)).flat();
    return (
      <svg className={css.link} viewBox="0 0 100 36" preserveAspectRatio="none" fill="none" aria-hidden="true">
        {paths.map((d) => <path key={d} d={d} stroke={stroke} strokeWidth={2} vectorEffect="non-scaling-stroke" strokeDasharray={dashed ? "5 5" : undefined} />)}
      </svg>
    );
  }
  const ys = (n: number) => { const h = n * NODE_H + (n - 1) * NODE_GAP; const top = head + NODE_GAP + (height - h) / 2; return Array.from({ length: n }, (_, k) => top + k * (NODE_H + NODE_GAP) + NODE_H / 2); };
  const a = ys(from);
  const paths = dashed ? [`M0 ${a[0]} H${LINK_W}`] : a.flatMap((y1) => ys(to).map((y2) => `M0 ${y1} C${LINK_W / 2} ${y1} ${LINK_W / 2} ${y2} ${LINK_W} ${y2}`));
  return (
    <svg className={css.link} width={LINK_W} height={head + NODE_GAP + height} viewBox={`0 0 ${LINK_W} ${head + NODE_GAP + height}`} fill="none" aria-hidden="true">
      {paths.map((d) => <path key={d} d={d} stroke={stroke} strokeWidth={2} strokeDasharray={dashed ? "5 5" : undefined} />)}
    </svg>
  );
}

/** The nodes' column height on a desktop: the tallest step's. */
export const nodesHeight = (counts: number[]) => { const n = Math.max(1, ...counts); return n * NODE_H + (n - 1) * NODE_GAP; };
/** How far down a step with `n` nodes starts its column, so every step is centred on the tallest. */
export const nodesPad = (n: number, height: number) => (height - (n * NODE_H + (n - 1) * NODE_GAP)) / 2;
export const HEAD_STYLE = { "--head-h": `${HEAD_H}px` } as React.CSSProperties;

/** The lineup builder (phase 4, design §7a — NewEvent.dc.html), ONE component for New event, Edit lineup (`fixedFirst`:
 *  step 1's raid stays put) and the schedule editor. The palette: each raid with its size and length, an Add button
 *  (greyed once used or when the lineup is full). The track: each step's head (Step n, Starts — a select of 15-minute
 *  steps after the step before it, labelled from `startClock` or as hours after the start; `times` = the server's own
 *  words for each step's start, shown for the picked value —, Rosters −/+ 1 to 4), one node per roster, connectors
 *  between steps, Move earlier / Move later / Remove per step, the empty slot while fewer than `max` steps. Dragging is
 *  an enhancement on a desktop (a raid onto the slot or a step, a step onto another to reorder); every action has its
 *  button, and below sm the steps stack with no drag and no sideways scroll. Pure: it never fetches; `onChange` gets
 *  the whole new lineup (a new step starts when the one before it ends — C12). `fullWords` = the server's sentence
 *  shown in the slot's place once the lineup holds `max` raids (registry.LINEUP_MAX_WORDS). */
export function LineupBuilder({ raids, value, onChange, startClock, fixedFirst, max, fullWords, times }: {
  raids: PaletteRaid[]; value: LineupEntry[]; onChange: (next: LineupEntry[]) => void; startClock: string | null;
  fixedFirst?: boolean; max: number; fullWords?: string; times?: (string | undefined)[];
}) {
  const phone = !!useMediaQuery("(max-width: 48em)");
  const [over, setOver] = useState<number | "slot" | null>(null);
  const byId = (rid: string) => raids.find((r) => r.id === rid);
  const nameOf = (rid: string) => byId(rid)?.name || rid;
  const used = new Set(value.map((e) => e.raid));
  const full = value.length >= max;
  const locked = (i: number) => !!fixedFirst && i === 0;

  const add = (rid: string) => {
    if (full || used.has(rid) || !byId(rid)) return;
    onChange(chain([...value, { raid: rid, rosters: 1, starts: 0 }], raids, value.length));
  };
  const remove = (i: number) => { if (!locked(i)) onChange(chain(value.filter((_, k) => k !== i), raids, i)); };
  const move = (i: number, j: number) => {
    if (i === j || j < 0 || j >= value.length || locked(i) || locked(j)) return;
    const next = [...value];
    const [e] = next.splice(i, 1);
    next.splice(j, 0, e);
    onChange(chain(next, raids, Math.min(i, j)));
  };
  const setRosters = (i: number, n: number) => onChange(value.map((e, k) => (k === i ? { ...e, rosters: Math.max(1, Math.min(MAX_ROSTERS, n)) } : e)));
  const setStart = (i: number, starts: number) => {
    const delta = starts - value[i].starts;  // the steps after it keep their gaps
    onChange(value.map((e, k) => (k >= i ? { ...e, starts: e.starts + delta } : e)));
  };

  const drop = (spot: number | "slot") => (ev: React.DragEvent) => {
    ev.preventDefault();
    setOver(null);
    const what = ev.dataTransfer.getData("text/plain");
    if (what.startsWith("raid:")) add(what.slice(5));  // a raid dropped anywhere on the track: the slot takes it
    else if (what.startsWith("step:") && spot !== "slot") move(Number(what.slice(5)), spot);
  };
  const dragOver = (spot: number | "slot") => (ev: React.DragEvent) => { ev.preventDefault(); if (over !== spot) setOver(spot); };

  const label = (off: number) => (startClock ? clock12(minutesOf(startClock) + off) : afterWords(off));
  const startOptions = (i: number) => {
    const lo = value[i - 1].starts + 15, hi = value[i - 1].starts + 12 * 60;
    const offs = new Set<number>();
    for (let m = Math.ceil(lo / 15) * 15; m <= hi; m += 15) offs.add(m);
    offs.add(value[i].starts);
    return [...offs].sort((a, b) => a - b).map((m) => ({ value: String(m), label: m === value[i].starts && times?.[i] ? times[i]! : label(m) }));
  };
  const height = nodesHeight(value.map((e) => e.rosters));

  return (
    <div className={css.builder}>
      <div className={css.palette} aria-label="Raids to add">
        <Text size="xs" fw={600} c="dimmed">Raids</Text>
        {raids.map((r) => {
          const taken = used.has(r.id);
          return (
            <div key={r.id} className={`${css.raid} ${taken ? css.used : ""}`} draggable={!phone && !taken && !full}
              onDragStart={(ev) => ev.dataTransfer.setData("text/plain", `raid:${r.id}`)}>
              <div className={css.raidText}><b>{r.name}</b><br /><Text span size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{r.size}-player · about {r.hours} h</Text></div>
              <Button size="compact-sm" variant="default" disabled={taken || full} onClick={() => add(r.id)} aria-label={`Add ${r.name}`} style={{ flex: "none" }}>{ADD}</Button>
            </div>
          );
        })}
        {!phone && <span className={css.hint}>Drag a raid onto the track. Drag a step to reorder it.</span>}
      </div>

      <div className={css.track} style={HEAD_STYLE} onDragOver={dragOver("slot")} onDragLeave={() => setOver(null)} onDrop={drop("slot")}>
        <div className={css.steps}>
          {value.map((e, i) => {
            const r = byId(e.raid);
            const name = nameOf(e.raid);
            return (
              <div key={e.raid} style={{ display: phone ? "block" : "flex" }}>
                {i > 0 && <Link from={value[i - 1].rosters} to={e.rosters} height={height} phone={phone} />}
                <div className={`${css.step} ${over === i ? css.over : ""}`} aria-label={`Step ${i + 1}: ${name}`} role="group"
                  onDragOver={(ev) => { ev.stopPropagation(); dragOver(i)(ev); }} onDrop={(ev) => { ev.stopPropagation(); drop(i)(ev); }}>
                  <div className={css.head}>
                    <div className={css.headTop} draggable={!phone && !locked(i)} onDragStart={(ev) => ev.dataTransfer.setData("text/plain", `step:${i}`)}>
                      <b>Step {i + 1}</b>
                      {!locked(i) && (
                        <span className={css.moves}>
                          <Tooltip label="Move earlier"><ActionIcon variant="subtle" color="gray" size="md" aria-label={`Move ${name} earlier`}
                            disabled={i === 0 || locked(i - 1)} onClick={() => move(i, i - 1)}>{phone ? <IconArrowUp size={16} /> : <IconArrowLeft size={16} />}</ActionIcon></Tooltip>
                          <Tooltip label="Move later"><ActionIcon variant="subtle" color="gray" size="md" aria-label={`Move ${name} later`}
                            disabled={i === value.length - 1} onClick={() => move(i, i + 1)}>{phone ? <IconArrowDown size={16} /> : <IconArrowRight size={16} />}</ActionIcon></Tooltip>
                          <Tooltip label="Remove"><ActionIcon variant="subtle" color="gray" size="md" aria-label={`Remove ${name}`}
                            onClick={() => remove(i)}><IconX size={16} /></ActionIcon></Tooltip>
                        </span>
                      )}
                    </div>
                    {i === 0 ? (
                      <Select label="Starts" size="sm" w={140} disabled value="0"
                        data={[{ value: "0", label: times?.[0] || (startClock ? clock12(minutesOf(startClock)) : "At the start") }]} />
                    ) : (
                      <Select label="Starts" size="sm" w={phone || !startClock ? 196 : 150} allowDeselect={false} value={String(e.starts)} data={startOptions(i)}
                        onChange={(v) => v && setStart(i, Number(v))} maxDropdownHeight={260} comboboxProps={{ withinPortal: true }} aria-label={`${name} starts`} />
                    )}
                    <div>
                      <Text size="sm" fw={600} c="dimmed" mb={4}>Rosters</Text>
                      <span className={css.stepper}>
                        <ActionIcon variant="default" size={36} aria-label={`one roster fewer for ${name}`} disabled={e.rosters <= 1} onClick={() => setRosters(i, e.rosters - 1)}><IconMinus size={16} /></ActionIcon>
                        <b aria-live="polite">{e.rosters}</b>
                        <ActionIcon variant="default" size={36} aria-label={`one roster more for ${name}`} disabled={e.rosters >= MAX_ROSTERS} onClick={() => setRosters(i, e.rosters + 1)}><IconPlus size={16} /></ActionIcon>
                      </span>
                    </div>
                  </div>
                  <StepNodes name={name} rosters={e.rosters} seats={r?.size || 0} pad={phone ? 0 : nodesPad(e.rosters, height)} />
                </div>
              </div>
            );
          })}
          {full && fullWords && <Text size="sm" c="dimmed" className={css.full} role="status">{fullWords}</Text>}
          {!full && (
            <div style={{ display: phone ? "block" : "flex" }}>
              {value.length > 0 && <Link from={value[value.length - 1].rosters} to={1} height={height} phone={phone} dashed />}
              <div className={`${css.drop} ${over === "slot" ? css.over : ""}`}
                style={phone ? undefined : { marginTop: HEAD_H + NODE_GAP, height: value.length ? height : NODE_H }}>
                {phone ? `Add a raid above for step ${value.length + 1}` : `Drop a raid here for step ${value.length + 1}`}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
