import { useEffect, useRef, useState } from "react";
import { ActionIcon, Box, Button, Group, Popover, SegmentedControl, Stack, Text, Tooltip } from "@mantine/core";
import { IconInfoCircle, IconRefresh, IconScale, IconUsers } from "@tabler/icons-react";
import { api, type Board, type GroupSummary, type Meta, type Seat, type Sheet } from "../../api";
import { SPLIT_LABEL } from "../../pages/Raids";
import { RaidWide } from "../GroupsBlock";
import { Eyebrow, fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { Aura, RosterHeader, SeatLine, SplitReasonLine, chunk, sortBank, splitBank, live, type Act, type BankSort, type OnBoard } from "./shared";
import { SplitModal } from "./SplitModal";
import css from "../../pages/rosters.module.css";

type Drag = { name: string; from: { r: number; g: number } | null };

/** The board: bank + groups, drag and drop. Before lock it is the layout the lock will use; after lock it is the roster.
 *  One ⓘ legend says what the marks mean and how dragging works (never repeated under every card); an open seat is a
 *  dashed slot with no word. After the lock only the groups with someone in them show, plus one empty group to drop
 *  into. `onHide`: before the lock the card collapses it back to one line. */
export function BoardView({ e, meta, busy, onAct, onBoard, onHide }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onHide?: () => void }) {
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
  const { left, rest } = splitBank(board, bank);  // leftovers: joiners the full roster(s) left out
  const reason = e.split?.reason || null;
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
  const commit = (groups: string[][][], strategy?: string) => send("layout", { groups: groups.flat(), rev: e.rev, ...(strategy ? { strategy } : {}) });
  /** A drag is sent as one move and applied server-side to the board as it is now, so two officers merge. `state` = the
   *  state this board was drawn in: a drag dropped after the scheduler locked the run is refused (409 + the board). */
  const move = (member: string, to: { r: number; g: number; i: number } | null) => send("move", { member, to, state: e.state === "open" ? "open" : "locked" });

  async function clear() {
    if (placed > 0 && !(await ask({ title: "Clear the board?", message: e.consequences?.clear || `${placed} placement${placed === 1 ? "" : "s"} go back to the bank.`, confirmLabel: "Clear", color: "red" }))) return;  // the sentence comes from the server (consequences.py)
    commit(board.rosters.map((r) => r.groups.map(() => [])));
  }

  async function drop(target: { r: number; g: number; i: number } | "bank") {
    const d = drag.current; drag.current = null; setOver(null);
    if (!d) return;
    const L = layout();
    if (d.from) L[d.from.r][d.from.g] = L[d.from.r][d.from.g].filter((n) => n !== d.name);
    if (target === "bank") {
      if (!d.from) return;
      // the sentence comes from the server (consequences.py); the browser only puts the member's name where the server left the {name} token
      const released = e.consequences?.release_named ? e.consequences.release_named.replace("{name}", d.name) : e.consequences?.release;
      if (locked && !(await ask({ title: `Take ${d.name} off the roster?`, message: released || `${d.name} is told their seat was released and the bench is asked to fill it.`, confirmLabel: "Release the seat", color: "red" }))) return;
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
        <Group gap="sm" wrap="wrap">
          <Text fw={700}>Board</Text>
          <Legend locked={locked} />
          {canSplit && multi && e.split && <Text size="xs" c="dimmed">{e.split.runs} runs this slot · {SPLIT_LABEL[e.split.strategy] || e.split.strategy}</Text>}
          {canSplit && reason && <SplitReasonLine meta={meta} r={reason} />}
          {saving && <Text size="xs" c="dimmed">saving…</Text>}
        </Group>
        {!locked && (
          <Group gap="xs" wrap="wrap">
            <Button size="xs" variant="default" leftSection={<IconUsers size={13} />} loading={busy === `auto${e.key}`} onClick={() => onAct(`auto${e.key}`, `/api/run/${e.key}/autofill`)}>Auto-fill empty seats</Button>
            {canSplit && <Button size="xs" variant="default" leftSection={<IconScale size={13} />} onClick={() => setSplitOpen(true)}>{multi ? "Propose splits" : "Propose roster"}</Button>}
            <Button size="xs" variant="subtle" color="red" leftSection={<IconRefresh size={13} />} disabled={saving} onClick={clear}>Clear</Button>
            {onHide && <Button size="xs" variant="subtle" color="gray" onClick={onHide}>Hide the board</Button>}
          </Group>
        )}
      </Group>
      <Box className={css.board}>
        <Box>
          <Group justify="space-between" gap="xs" wrap="wrap">
            <Eyebrow plain>Bank · {board.bank.length} unplaced</Eyebrow>
            <SegmentedControl size="xs" value={bankSort} onChange={pickSort} data={[{ value: "signup", label: "signup" }, { value: "spec", label: "spec" }, { value: "role", label: "role" }]} />
          </Group>
          <Stack gap={4} mt={6} p={8} style={{ minHeight: 120, border: `1px dashed ${over === "bank" ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, background: "var(--mantine-color-slate-7)" }} {...zone("bank", () => drop("bank"))}>
            {board.bank.length === 0 && <Text size="xs" c="dimmed">{placed ? "Everyone who joined is placed." : "Nobody joined this run."}</Text>}
            {left.length > 0 && <Text size="xs" fw={700}>Leftovers ({left.length})</Text>}
            {left.map((s) => chip(s, null))}
            {left.length > 0 && locked && reason && <SplitReasonLine meta={meta} r={reason} />}
            {left.length > 0 && board.another && <Text size="xs" c="dimmed">{board.another}</Text>}
            {left.length > 0 && rest.length > 0 && <Text size="xs" fw={700} mt={6}>Bench</Text>}
            {rest.map((s) => chip(s, null))}
          </Stack>
        </Box>
        <Stack gap="md" style={{ minWidth: 0 }}>
          {board.rosters.map((r, ri) => (
            <Box key={r.n}>
              {board.rosters.length > 1 && <RosterHeader meta={meta} r={r} />}
              <Box className={css.groups}>
                {r.groups.map((g, gi) => {
                  // after the lock: the groups with someone in them, plus the first empty one to drop into
                  if (locked && g.length === 0 && r.groups.findIndex((x) => x.length === 0) !== gi) return null;
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
                            {s ? chip(s, { r: ri, g: gi }, conf[s.display_name]) : <Tooltip label="open seat"><Box style={{ alignSelf: "stretch", flex: 1 }} aria-label="open seat" /></Tooltip>}
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
      {canSplit && <SplitModal e={e} meta={meta} multi={multi} opened={splitOpen} onClose={() => setSplitOpen(false)} onUse={async (layout, strategy) => { await commit(layout.map(() => []).length ? chunk(layout, board.n_groups) : [], strategy); }} />}
      {confirmDialog}
    </Box>
  );
}

/** The board's one legend (ⓘ): what its marks mean and how a drag works — said once, not under every card. */
function Legend({ locked }: { locked: boolean }) {
  const row = (mark: React.ReactNode, words: string) => <Group gap={8} wrap="nowrap" align="center"><Box style={{ width: 34, flex: "none", display: "flex", justifyContent: "center" }}>{mark}</Box><Text size="xs">{words}</Text></Group>;
  const sq = (style: React.CSSProperties) => <Box style={{ width: 20, height: 20, borderRadius: 5, ...style }} />;
  return (
    <Popover width={340} position="bottom-start" shadow="md" withinPortal>
      <Popover.Target>
        <ActionIcon variant="subtle" color="gray" size="md" aria-label="What the board shows"><IconInfoCircle size={16} /></ActionIcon>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={8}>
          <Text size="sm" fw={700}>What the board shows</Text>
          {row(<Box style={{ width: 30, height: 16, border: "1px dashed var(--mantine-color-slate-4)", borderRadius: 5 }} />, "an open seat — the lock fills it, or a fill ask after the lock")}
          {row(sq({ background: "var(--mantine-color-teal-7)" }), "a raid buff someone in the group brings")}
          {row(sq({ border: "2px solid var(--mantine-color-red-5)", opacity: 0.6 }), "greyed with a red edge: wanted in this group, and the one who brings it is in another")}
          {row(sq({ border: "2px solid var(--mantine-color-slate-5)", opacity: 0.6 }), "plain grey: nobody in the run brings it (hover for its name)")}
          {row(sq({ border: "2px solid var(--mantine-color-teal-4)", opacity: 0.8 }), "teal edge: covered by a raid-wide buff of the same family")}
          <Text size="xs" c="dimmed">Totems count one per element.</Text>
          <Text size="xs" c="dimmed">{locked
            ? "The board is the roster: moving someone between groups is free, dragging someone in from the bank asks them to confirm, dragging someone out frees their seat."
            : "Drag a name into a group, or back to the bank to take it out again; dropping on someone swaps the two."}</Text>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
