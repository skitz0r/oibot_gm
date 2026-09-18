import { useEffect, useRef, useState } from "react";
import { Box, Button, Group, SegmentedControl, Stack, Text } from "@mantine/core";
import { IconRefresh, IconScale, IconSparkles } from "@tabler/icons-react";
import { api, type Board, type GroupSummary, type Meta, type Seat, type Sheet } from "../../api";
import { SPLIT_LABEL } from "../../pages/Raids";
import { RaidWide } from "../GroupsBlock";
import { Eyebrow, fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { Aura, RosterHeader, SeatLine, chunk, sortBank, live, type Act, type BankSort, type OnBoard } from "./shared";
import { SplitModal } from "./SplitModal";
import css from "../../pages/rosters.module.css";

type Drag = { name: string; from: { r: number; g: number } | null };

/** The board: bank + groups, drag and drop. Before lock it is the layout the lock will use; after lock it is the roster. */
export function BoardView({ e, meta, busy, onAct, onBoard }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard }) {
  const board = e.board!;
  const locked = e.state !== "open";
  const conf = Object.fromEntries((e.confirmations || []).map((c) => [c.display_name, c.answer]));
  const drag = useRef<Drag | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [splitOpen, setSplitOpen] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const multi = (e.split?.runs || 1) > 1;
  const canSplit = !locked;  // Propose works for one roster too; the philosophy picker only matters for splits
  const [bankSort, setBankSort] = useState<BankSort>(() => { try { return (localStorage.getItem("oibot.bankSort") as BankSort) || "signup"; } catch { return "signup"; } });
  const pickSort = (v: string) => { setBankSort(v as BankSort); try { localStorage.setItem("oibot.bankSort", v); } catch { /* per-viewer convenience only */ } };
  const bank = sortBank(board.bank, bankSort, meta.roles);
  const caps = meta.group_caps || {};
  const placed = board.rosters.reduce((n, r) => n + r.groups.reduce((m, g) => m + g.length, 0), 0);

  const layout = () => board.rosters.map((r) => r.groups.map((g) => g.map((s) => s.display_name)));

  type Reply = { board: Board; needs: Sheet["needs"]; rev?: string; message?: string };
  /** Any board write. A refusal (someone else got there first, group full) still carries the current board: show it. */
  async function send(path: string, payload: unknown) {
    setSaving(true);
    try {
      const r = await api.post<Reply>(`/api/run/${e.key}/${path}`, payload);
      onBoard(e.key, r.board, r.needs, r.rev);
      if (r.message) ok(r.message);
    } catch (err) {
      const b = (err as { body?: Partial<Reply> }).body;
      if (b?.board) onBoard(e.key, b.board, b.needs, b.rev);
      fail(err);
    } finally { setSaving(false); }
  }
  /** Whole-board writes (Clear, Use this split) are tied to the board this officer was looking at. */
  const commit = (groups: string[][][]) => send("layout", { groups: groups.flat(), rev: e.rev });
  /** A drag is sent as one move and applied server-side to the board as it is now, so two officers merge. */
  const move = (member: string, to: { r: number; g: number; i: number } | null) => send("move", { member, to });

  async function clear() {
    if (placed > 0 && !(await ask({ title: "Clear the board?", message: `${placed} placement${placed === 1 ? "" : "s"} go back to the bank. Nobody is told — nothing has been sent before lock.`, confirmLabel: "Clear", color: "red" }))) return;
    commit(board.rosters.map((r) => r.groups.map(() => [])));
  }

  async function drop(target: { r: number; g: number; i: number } | "bank") {
    const d = drag.current; drag.current = null; setOver(null);
    if (!d) return;
    const L = layout();
    if (d.from) L[d.from.r][d.from.g] = L[d.from.r][d.from.g].filter((n) => n !== d.name);
    if (target === "bank") {
      if (!d.from) return;
      if (locked && !(await ask({ title: `Take ${d.name} off the roster?`, message: `${d.name} is told their seat was released and the bench is asked to fill it.`, confirmLabel: "Release the seat", color: "red" }))) return;
      move(d.name, null); return;
    }
    const g = L[target.r][target.g];
    const displaced = g[target.i];
    if (displaced && displaced !== d.name) {
      g.splice(target.i, 1, d.name);
      if (d.from) L[d.from.r][d.from.g].push(displaced); // swap
      else if (!locked) { /* from bank: displaced goes back to bank */ }
      else if (!(await ask({ title: `Swap ${displaced} out for ${d.name}?`, message: `${displaced} is told their seat was released and the bench is asked; ${d.name} gets a confirmation DM.`, confirmLabel: "Swap" }))) return;
    } else if (g.length >= board.group_size) {
      fail(new Error(`Group ${target.g + 1} is full`));
      return;
    } else {
      g.splice(Math.min(target.i, g.length), 0, d.name);
      if (locked && !d.from && !(await ask({ title: `Roster ${d.name}?`, message: `${d.name} gets a confirmation DM; the seat is theirs once they say yes.`, confirmLabel: "Ask them" }))) return;
    }
    move(d.name, target);
  }
  const onDragStart = (name: string, from: Drag["from"]) => (ev: React.DragEvent) => { drag.current = { name, from }; ev.dataTransfer.effectAllowed = "move"; live.dragging = true; };
  useEffect(() => { const end = () => { live.dragging = false; }; window.addEventListener("dragend", end); window.addEventListener("drop", end); return () => { window.removeEventListener("dragend", end); window.removeEventListener("drop", end); }; }, []);
  const zone = (id: string, onDrop: () => void) => ({
    onDragOver: (ev: React.DragEvent) => { ev.preventDefault(); if (over !== id) setOver(id); },
    onDragLeave: () => setOver((o) => (o === id ? null : o)),
    onDrop: (ev: React.DragEvent) => { ev.preventDefault(); onDrop(); },
  });
  const chip = (s: Seat, from: Drag["from"], mark?: string | null) => (
    <Box key={s.display_name} draggable onDragStart={onDragStart(s.display_name, from)} style={{ cursor: "grab", padding: "2px 6px", borderRadius: 6, background: from ? "transparent" : "var(--mantine-color-slate-6)", border: from ? "1px solid transparent" : "1px solid var(--mantine-color-slate-5)" }}>
      <SeatLine meta={meta} s={s} mark={locked ? (mark ?? null) : undefined} />
    </Box>
  );
  const groupSummary = (sm: GroupSummary, gi: number) => sm.groups[gi];

  return (
    <Box mt="lg">
      <Group justify="space-between" wrap="wrap" mb="xs">
        <Group gap="sm"><Eyebrow>Roster builder</Eyebrow>{canSplit && multi && e.split && <Text size="xs" c="dimmed">{e.split.runs} runs this slot · {SPLIT_LABEL[e.split.strategy] || e.split.strategy}</Text>}{saving && <Text size="xs" c="dimmed">saving…</Text>}{locked && <Text size="xs" c="dimmed">the board is the roster: group moves are free, dragging in from the bench asks that person to confirm, dragging out frees the seat</Text>}</Group>
        {!locked && (
          <Group gap="xs">
            {canSplit && <Button size="xs" leftSection={<IconScale size={13} />} onClick={() => setSplitOpen(true)}>{multi ? "Propose splits" : "Propose roster"}</Button>}
            <Button size="xs" variant="default" leftSection={<IconSparkles size={13} />} loading={busy === `auto${e.key}`} onClick={() => onAct(`auto${e.key}`, `/api/run/${e.key}/autofill`)}>Auto-fill empty seats</Button>
            <Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} disabled={saving} onClick={clear}>Clear</Button>
          </Group>
        )}
      </Group>
      <Box className={css.board}>
        <Box>
          <Group justify="space-between" gap="xs" wrap="wrap">
            <Eyebrow>Bank · {board.bank.length} unplaced</Eyebrow>
            <SegmentedControl size="xs" value={bankSort} onChange={pickSort} data={[{ value: "signup", label: "signup" }, { value: "spec", label: "spec" }, { value: "role", label: "role" }]} />
          </Group>
          <Stack gap={4} mt={6} p={8} style={{ minHeight: 120, border: `1px dashed ${over === "bank" ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, background: "var(--mantine-color-slate-7)" }} {...zone("bank", () => drop("bank"))}>
            {board.bank.length === 0 && <Text size="xs" c="dimmed">everyone who joined is placed</Text>}
            {bank.map((s) => chip(s, null))}
          </Stack>
          <Text size="xs" c="dimmed" mt={6}>Drag into a group. Drag a placed name back here to bench them. Dropping on someone swaps.</Text>
        </Box>
        <Stack gap="md" style={{ minWidth: 0 }}>
          {board.rosters.map((r, ri) => (
            <Box key={r.n}>
              {board.rosters.length > 1 && <RosterHeader meta={meta} r={r} />}
              <Box className={css.groups}>
                {r.groups.map((g, gi) => {
                  const sm = groupSummary(r.summary, gi);
                  const overCap = Object.entries(caps).filter(([role, cap]) => g.filter((s) => s.role === role).length > cap).map(([role]) => role);
                  return (
                    <Box key={gi} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
                      <Group justify="space-between" mb={4}><Text size="sm" fw={700}>Group {gi + 1}</Text><Text size="xs" c={overCap.length ? "red" : "dimmed"}>{g.length}/{board.group_size}{overCap.map((role) => ` · ${g.filter((s) => s.role === role).length} ${role}s (max ${caps[role]})`).join("")}</Text></Group>
                      {Array.from({ length: board.group_size }, (_, i) => {
                        const id = `${ri}:${gi}:${i}`;
                        const s = g[i];
                        return (
                          <Box key={i} {...zone(id, () => drop({ r: ri, g: gi, i }))} style={{ minHeight: 30, margin: "2px 0", borderRadius: 6, border: `1px dashed ${over === id ? "var(--mantine-color-teal-4)" : s ? "transparent" : "var(--mantine-color-slate-5)"}`, display: "flex", alignItems: "center", paddingLeft: s ? 0 : 8 }}>
                            {s ? chip(s, { r: ri, g: gi }, conf[s.display_name]) : <Text size="xs" c="dimmed">· open</Text>}
                          </Box>
                        );
                      })}
                      {sm && (
                        <>
                          <Group gap={4} mt={8} style={{ minHeight: 30 }}>
                            {sm.present.map((a) => <Aura key={a.abbr} a={a} who={a.who} />)}
                            {sm.missing.map((a) => <Aura key={`m${a.abbr}`} a={a} missing />)}
                          </Group>
                          {sm.picks.length > 0 && <Text size="xs" c="dimmed" mt={4}>totems: {sm.picks.join(" · ")}</Text>}
                        </>
                      )}
                    </Box>
                  );
                })}
              </Box>
              <RaidWide sm={r.summary} />
              {r.advisories.length > 0 && <Text size="xs" c="dimmed" mt={4}>{r.advisories.join(" · ")}</Text>}
            </Box>
          ))}
        </Stack>
      </Box>
      {canSplit && <SplitModal e={e} meta={meta} multi={multi} opened={splitOpen} onClose={() => setSplitOpen(false)} onUse={async (layout, strategy) => { await api.post(`/api/run/${e.key}/strategy`, { strategy }).catch(fail); await commit(layout.map(() => []).length ? chunk(layout, board.n_groups) : []); }} />}
      {confirmDialog}
      <Text size="xs" c="dimmed" mt={6}>Coloured = present in that group · greyed with a red edge = wanted here and the provider sits in another group · plain grey = nobody in the run brings it · teal edge = covered by a raid-wide buff of the same family · totems one per element.</Text>
    </Box>
  );
}
