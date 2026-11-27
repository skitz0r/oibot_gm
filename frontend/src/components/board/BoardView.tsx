import { useEffect, useRef, useState } from "react";
import { ActionIcon, Box, Button, Group, Popover, SegmentedControl, Stack, Text, Tooltip } from "@mantine/core";
import { IconInfoCircle, IconRefresh, IconScale, IconUsers } from "@tabler/icons-react";
import { api, ApiError, type Board, type Box as BoxT, type GroupSummary, type Meta, type Seat, type Sheet } from "../../api";
import { SPLIT_LABEL } from "./shared";
import { RaidWide } from "../GroupsBlock";
import { Eyebrow, fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { useConfirmBox } from "../ConfirmBox";
import { GameIcon } from "../Icons";
import { Aura, PinMark, RosterHeader, SeatLine, SplitReasonLine, chunk, fillNames, markOf, sortBank, splitBank, live, type BankSort, type OnBoard } from "./shared";
import { SplitModal } from "./SplitModal";
import { MemberMenu, type MoveTarget } from "./MemberMenu";
import css from "./board.module.css";

type From = { r: number; g: number } | null;
type Drag = { name: string; from: From };
/** Where a move goes: a seat (roster, group, position) or null = back to Not placed / Not rostered. */
type Target = { r: number; g: number; i: number } | null;
/** The board's moves, each with its sentence in ev_json.consequences (consequences.py): move / roster / swap / substitute / release. */
type Verb = "move" | "roster" | "swap" | "substitute" | "release";

/** The board: the joiners not placed yet (before the lock — "Not placed") or not rostered (after it — "Not rostered"), the
 *  groups, drag and drop. Every word comes from the server (meta.run_words / run_labels / marks): the list's name, the pin
 *  marks' tooltips, Place the rest. Before lock it is the layout the lock will use; after lock it is the roster.
 *  One ⓘ legend says what the marks mean and how dragging works (never repeated under every card); an open seat is a
 *  dashed slot with no word. After the lock only the groups with someone in them show, plus one empty group to drop
 *  into. `onHide`: before the lock the card collapses it back to one line.
 *  `variant="event"` (the Events page): before the lock the board is the OFFICERS' layout (nothing solved) — no Clear,
 *  Place the rest or split control here (Clear the board is in the event's More menu); `bankAction` (Auto-fill groups)
 *  sits beside Not placed (no note under it: issue #12); under Not placed a second section, the server's Bench word
 *  and count, lists the Bench answers no group holds (`board.bench`, not drawn when empty) — each line drags like a Not
 *  placed line, and a name dropped on either section is un-placed (the server's answer says which list it lands in);
 *  a name kept off through the ⋮ menu carries the server's
 *  word `kept_off` on its line. A locked roster's `open` roles (the server's)
 *  are drawn as dashed slots, role icon + "open seat". A drop or Move to… sends ONE move and the page draws what the
 *  server answers (the answer's board, through `onBoard`), never a place of its own; while it is written the moved name
 *  is dimmed in place — no "saving" line, nothing shifts (issue #12) — and a refusal leaves it where it was with the
 *  server's sentence. Before the lock a move goes at once; a locked event takes no drag, and its
 *  Move to… asks through the server-worded box (useConfirmBox); a finished event is read only. Without `variant` (the
 *  Test bench) as before. */
export function BoardView({ e, meta, onBoard, onHide, onReload, variant, bankAction }: { e: Sheet; meta: Meta; onBoard: OnBoard; onHide?: () => void; onReload?: () => Promise<void> | void; variant?: "event"; bankAction?: React.ReactNode }) {
  const board = e.board!;
  const locked = e.state !== "open";
  const event = variant === "event";
  const readOnly = event && e.state !== "open" && e.state !== "locked";  // a finished event: the board as it was locked
  const drags = !event || !locked;  // the Events page drags only before the lock
  const [askBox, boxDialog] = useConfirmBox(event ? ["run:"] : []);
  const conf = Object.fromEntries((e.confirmations || []).map((c) => [c.display_name, c.answer]));
  const drag = useRef<Drag | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [moving, setMoving] = useState<string | null>(null);  // the name a move is being written for: dimmed in place, no line
  const [placing, setPlacing] = useState(false);
  const [splitOpen, setSplitOpen] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const multi = (e.split?.runs || 1) > 1;
  const canSplit = !locked && !event;  // Propose works for one roster too; the philosophy picker only matters for splits
  const [bankSort, setBankSort] = useState<BankSort>(() => { try { return (localStorage.getItem("oibot.bankSort") as BankSort) || "signup"; } catch { return "signup"; } });
  const pickSort = (v: string) => { setBankSort(v as BankSort); try { localStorage.setItem("oibot.bankSort", v); } catch { /* per-viewer convenience only */ } };
  const bank = sortBank(board.bank, bankSort, meta.roles);
  const { left, rest } = splitBank(board, bank);  // leftovers: joiners the full roster(s) left out
  // the Events board before the lock: the Bench answers no group holds, a second section under Not placed (none = not drawn)
  const bench = event && !locked ? sortBank(board.bench || [], bankSort, meta.roles) : [];
  const reason = e.split?.reason || null;
  const caps = meta.group_caps || {};
  const placed = board.rosters.reduce((n, r) => n + r.groups.reduce((m, g) => m + g.length, 0), 0);
  const pins = Object.fromEntries((e.signups || []).map((s) => [s.display_name, s.pin]));  // officers' pins: an icon on the chip
  const answers = Object.fromEntries((e.signups || []).map((s) => [s.display_name, s.status]));  // "sub" = a Bench answer
  const words = meta.run_words;

  const layout = () => board.rosters.map((r) => r.groups.map((g) => g.map((s) => s.display_name)));

  type Reply = { board: Board; needs: Sheet["needs"]; rev?: string; message?: string };
  /** Any board write. A refusal (someone else got there first, group full) still carries the current board: show it. */
  async function send(path: string, payload: unknown, name?: string) {
    setSaving(true);
    if (name) setMoving(name);
    try {
      const r = await api.post<Reply>(`/api/run/${encodeURIComponent(e.key)}/${path}`, payload);
      onBoard(e.key, r.board, r.needs, r.rev);
      if (r.message) ok(r.message);
    } catch (err) {
      const b = (err as { body?: Partial<Reply> }).body;
      if (b?.board) onBoard(e.key, b.board, b.needs, b.rev);
      fail(err);
    } finally { setSaving(false); if (name) setMoving(null); }
  }
  /** Whole-board writes (Clear, Use this split) are tied to the board this officer was looking at. */
  const commit = (groups: string[][][], strategy?: string) => send("layout", { groups: groups.flat(), rev: e.rev, ...(strategy ? { strategy } : {}) });
  /** A drag is sent as one move and applied server-side to the board as it is now, so two officers merge. `state` = the
   *  state this board was drawn in: a drag dropped after the scheduler locked the run is refused (409 + the board). */
  const move = (member: string, to: { r: number; g: number; i: number } | null) => send("move", { member, to, state: e.state === "open" ? "open" : "locked" }, member);

  async function clear() {
    // the sentence comes from the server (consequences.py "clear"); the browser prints it, never composes one
    if (placed > 0 && !(await ask({ title: "Clear the board?", message: e.consequences?.clear, confirmLabel: "Clear", cancelLabel: "Back", color: "red" }))) return;
    commit(board.rosters.map((r) => r.groups.map(() => [])));
  }
  /** Place the rest: the solver places everyone the officers left unplaced, around their placements. Carries the board's
   *  rev (a board that changed meanwhile is refused with the current one) and shows its progress while it solves. */
  async function placeRest() {
    setPlacing(true);
    try { await send("autofill", { rev: e.rev, wait: true }); } finally { setPlacing(false); }
  }

  const groupName = (r: number, g: number) => (board.rosters.length > 1 ? `Roster ${r + 1} · Group ${g + 1}` : `Group ${g + 1}`);
  const bankWord = words.bank[locked ? "locked" : "open"];
  /** The confirm before a move (the Test bench's board): a title naming the people, and ONLY the server's sentence for
   *  the verb (ev_json.consequences, with the names put where the server left {name} / {other} / {to}) — Back / the verb. */
  function confirmMove(verb: Verb, name: string, n: { other?: string; to?: string }) {
    const c = e.consequences || ({} as NonNullable<Sheet["consequences"]>);
    const sentence = verb === "release" ? (c.release_named || c.release) : c[verb];
    const title = { move: `Move ${name} to ${n.to}?`, roster: `Move ${name} to ${n.to}?`, swap: `Swap ${name} and ${n.other}?`,
      substitute: `Put ${name} in ${n.other}'s seat?`, release: locked ? `Take ${name} off the roster?` : `Move ${name} to ${bankWord.toLowerCase()}?` }[verb];
    const label = { move: "Move", roster: "Move", swap: "Swap", substitute: "Put them in", release: locked ? "Release the seat" : "Move" }[verb];
    return ask({ title, message: fillNames(sentence, { name, ...n }), confirmLabel: label, cancelLabel: "Back", color: verb === "release" || verb === "substitute" ? "red" : undefined });
  }
  /** A LOCKED event's move (the Events page, §7b): the SERVER's box for it (POST /move `preview` →
   *  consequences.board_move_box: which move it is on the board as it stands, its title, sentences and verb), the press
   *  sends the box's fp as `seen`. A board that changed while the box was open answers 409 with the fresh box (and the
   *  board, drawn at once); a move that can't be made, its refusal box (Back alone). */
  async function serverMove(name: string, target: Target) {
    const url = `/api/run/${encodeURIComponent(e.key)}/move`;
    const base = { member: name, to: target, state: "locked" };
    const boxOfErr = (err: unknown): BoxT | undefined => (err instanceof ApiError ? (err.body as { box?: BoxT } | undefined)?.box : undefined);
    const preview = async (): Promise<BoxT> => {
      try { return (await api.post<{ box: BoxT }>(url, { ...base, preview: true })).box; } catch (err) { const b = boxOfErr(err); if (b) return b; throw err; }
    };
    let first: BoxT;
    try { first = await preview(); } catch (err) { fail(err); return; }
    const said = { text: "" };
    const done = await askBox({
      box: first, refresh: preview,
      act: async (fp) => {
        try {
          const r = await api.post<Reply>(url, { ...base, seen: fp });
          onBoard(e.key, r.board, r.needs, r.rev); said.text = r.message || ""; return null;
        } catch (err) {
          const b = (err as { body?: Partial<Reply> }).body;
          if (b?.board) onBoard(e.key, b.board, b.needs, b.rev);
          const bx = boxOfErr(err); if (bx) return bx; throw err;
        }
      },
    });
    if (done && said.text) ok(said.text);
  }
  /** ONE path for every move — a drag and the ⋮ menu's Move to… — so both send the same POST /move body. After the lock
   *  every move edits the roster and asks first; before it a drag is free (it only shapes the layout the lock uses) and
   *  Move to… still shows where it goes (a tap is easy to misplace). */
  async function place(name: string, from: From, target: Target, via: "drag" | "menu") {
    const L = layout();
    if (from) L[from.r][from.g] = L[from.r][from.g].filter((x) => x !== name);
    const asks = locked || (via === "menu" && !event);  // the Events page: before the lock a move goes at once
    if (event && locked) {  // a locked event's move: the server says which move it is and asks (a reorder in one group goes at once)
      const same = !!from && !!target && from.r === target.r && from.g === target.g;
      if (target === null && !from) return;
      if (same) { move(name, target); return; }
      await serverMove(name, target); return;
    }
    if (target === null) {
      if (!from) return;
      if (asks && !(await confirmMove("release", name, {}))) return;
      move(name, null); return;
    }
    const g = L[target.r][target.g];
    const to = groupName(target.r, target.g);
    const sameGroup = !!from && from.r === target.r && from.g === target.g;
    const other = sameGroup ? undefined : g[target.i];
    let verb: Verb;
    if (other && other !== name) verb = from ? "swap" : "substitute";
    else if (g.length >= board.group_size && !event) { fail(new Error(`${to} is full`)); return; }  // the Events page: the server's answer says
    else verb = from ? "move" : "roster";
    if (asks && !sameGroup && !(await confirmMove(verb, name, { other, to }))) return;
    move(name, target);
  }
  async function drop(target: { r: number; g: number; i: number } | "bank") {
    const d = drag.current; drag.current = null; setOver(null);
    if (!d) return;
    await place(d.name, d.from, target === "bank" ? null : target, "drag");
  }
  /** Move to…: every group with room (its first open seat — the same {r, g, i} a drop on that seat sends) and the list
   *  of joiners without a place (a Bench answer's: the Bench section). A tap, a keyboard, a phone: no drag needed. */
  const movesFor = (name: string, from: From): MoveTarget[] => {
    const out: MoveTarget[] = [];
    board.rosters.forEach((r, ri) => r.groups.forEach((g, gi) => {
      if (from && from.r === ri && from.g === gi) return;
      if (locked && g.length === 0 && r.groups.findIndex((x) => x.length === 0) !== gi) return;  // as drawn: one empty group to move into
      const full = g.length >= board.group_size;
      out.push({ key: `${ri}:${gi}`, label: groupName(ri, gi), disabled: full, hint: `${g.length}/${board.group_size}`, onPick: () => place(name, from, { r: ri, g: gi, i: g.length }, "menu") });
      if (event && full) g.forEach((o, oi) => out.push(swapWith(name, from, o.display_name, ri, gi, oi)));  // a phone can't drag onto someone
    }));
    // a Bench answer taken off a group lands in the Bench section before the lock, so its entry says Bench
    const back = event && !locked && answers[name] === "sub" ? words.bank.bench : bankWord;
    if (from) out.push({ key: "bank", label: back, onPick: () => place(name, from, null, "menu") });
    return out;
  };
  /** The Events page's Move to… into a full group: one entry per person there — the same drop onto that person a drag
   *  makes (a swap, or from Not placed a substitute), named for both in its words and its aria-label. */
  const swapWith = (name: string, from: From, other: string, ri: number, gi: number, oi: number): MoveTarget => ({
    key: `${ri}:${gi}:${oi}`, label: `Swap with ${other}`, hint: groupName(ri, gi), aria: `Swap ${name} with ${other} in ${groupName(ri, gi)}`,
    onPick: () => place(name, from, { r: ri, g: gi, i: oi }, "menu"),
  });
  const onDragStart = (name: string, from: Drag["from"]) => (ev: React.DragEvent) => { drag.current = { name, from }; ev.dataTransfer.effectAllowed = "move"; live.dragging = true; };
  /** Each roster's open seats as the server sends them (`open`: a role per empty seat a minimum still wants; null before the
   *  lock = none drawn), put in that roster's drawn empty slots in order — the Events page's locked board only: an open
   *  board never draws them, whatever a write's answer carries. */
  const openAt = (ri: number): Record<string, string> => {
    const r = board.rosters[ri];
    const roles = event && locked && r.open ? [...r.open] : [];
    const out: Record<string, string> = {};
    r.groups.forEach((g, gi) => {
      if (locked && g.length === 0 && r.groups.findIndex((x) => x.length === 0) !== gi) return;
      for (let i = g.length; i < board.group_size && roles.length; i++) out[`${gi}:${i}`] = roles.shift()!;
    });
    return out;
  };
  useEffect(() => { const end = () => { live.dragging = false; }; window.addEventListener("dragend", end); window.addEventListener("drop", end); return () => { window.removeEventListener("dragend", end); window.removeEventListener("drop", end); }; }, []);
  const zone = (id: string, onDrop: () => void) => (!drags || readOnly ? {} : {
    onDragOver: (ev: React.DragEvent) => { ev.preventDefault(); if (over !== id) setOver(id); },
    onDragLeave: () => setOver((o) => (o === id ? null : o)),
    onDrop: (ev: React.DragEvent) => { ev.preventDefault(); onDrop(); },
  });
  const chip = (s: Seat, from: Drag["from"], mark?: string | null) => (
    <Box key={s.display_name} draggable={drags && !readOnly} onDragStart={drags && !readOnly ? onDragStart(s.display_name, from) : undefined} style={{ cursor: drags && !readOnly ? "grab" : undefined, opacity: moving === s.display_name ? 0.5 : undefined, padding: "2px 6px", borderRadius: 6, background: from ? "transparent" : "var(--mantine-color-slate-6)", border: from ? "1px solid transparent" : "1px solid var(--mantine-color-slate-5)" }}>
      <SeatLine meta={meta} s={s} mark={locked && !readOnly && (!event || e.confirms !== false) ? (mark ?? null) : undefined} right={<>{event && !locked && !from && pins[s.display_name] === "out" && <Text span size="xs" c="red.4" style={{ whiteSpace: "nowrap" }}>{meta.page_words.kept_off}</Text>}<PinMark meta={meta} pin={pins[s.display_name]} />{!readOnly && <Box ml="auto" onClick={(ev) => ev.stopPropagation()}><MemberMenu run={e.key} uid={s.uid} name={s.display_name} onDone={onReload || (() => undefined)} moves={movesFor(s.display_name, from)} meta={event ? meta : undefined} /></Box>}</>} />
    </Box>
  );
  const groupSummary = (sm: GroupSummary, gi: number) => sm.groups[gi];

  return (
    <Box mt={event ? 0 : "lg"}>
      <Group justify="space-between" wrap="wrap" mb="xs">
        <Group gap="sm" wrap="wrap">
          {!event && <Text fw={700}>Board</Text>}
          {!event && !readOnly && <Legend meta={meta} locked={locked} />}
          {canSplit && multi && e.split && <Text size="xs" c="dimmed">{e.split.runs} rosters this slot · {SPLIT_LABEL[e.split.strategy] || e.split.strategy}{board.balance ? ` · ${board.balance}` : ""}</Text>}
          {canSplit && reason && <SplitReasonLine meta={meta} r={reason} />}
          {placing && <Text size="xs" c="dimmed">placing the rest…</Text>}
        </Group>
        {!locked && !event && (
          <Group gap="xs" wrap="wrap">
            <Button size="xs" variant="default" leftSection={<IconUsers size={13} />} loading={placing} disabled={saving && !placing} onClick={placeRest}>{meta.run_labels.place_rest}</Button>
            {canSplit && <Button size="xs" variant="default" leftSection={<IconScale size={13} />} onClick={() => setSplitOpen(true)}>{multi ? "Propose splits" : "Propose roster"}</Button>}
            <Button size="xs" variant="subtle" color="red" leftSection={<IconRefresh size={13} />} disabled={saving} onClick={clear}>Clear</Button>
            {onHide && <Button size="xs" variant="subtle" color="gray" onClick={onHide}>Hide the board</Button>}
          </Group>
        )}
      </Group>
      <Box className={readOnly ? undefined : css.board}>
        {!readOnly && <Box>
          <Group justify="space-between" gap="xs" wrap="wrap">
            <Eyebrow plain>{bankWord} · {board.bank.length}</Eyebrow>
            <SegmentedControl size="xs" value={bankSort} onChange={pickSort} data={[{ value: "signup", label: "signup" }, { value: "spec", label: "spec" }, { value: "role", label: "role" }]} />
          </Group>
          {event && !locked && bankAction}
          <Stack gap={4} mt={6} p={8} style={{ minHeight: 120, border: `1px dashed ${over === "bank" ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, background: "var(--mantine-color-slate-7)" }} {...zone("bank", () => drop("bank"))}>
            {board.bank.length === 0 && !event && <Text size="xs" c="dimmed">{!placed ? "Nobody has joined yet — the sheet is in Discord." : locked ? "Everyone who joined is rostered. A name dragged here frees their seat." : "Everyone who joined is placed. A name dragged here goes back to not placed — still joined, the lock seats them."}</Text>}
            {board.bank.length === 0 && event && <Text size="xs" c="dimmed">{meta.page_words.bank_empty[!placed ? "nobody" : locked ? "rostered" : "placed"]}</Text>}
            {left.length > 0 && <Text size="xs" fw={700}>{words.leftovers} ({left.length})</Text>}
            {left.map((s) => chip(s, null))}
            {left.length > 0 && locked && reason && <SplitReasonLine meta={meta} r={reason} />}
            {left.length > 0 && board.another && <Text size="xs" c="dimmed">{board.another}</Text>}
            {left.length > 0 && rest.length > 0 && <Text size="xs" fw={700} mt={6}>{words.bank.bench}</Text>}
            {rest.map((s) => chip(s, null))}
          </Stack>
          {bench.length > 0 && <>
            <Box mt="sm"><Eyebrow plain>{words.bank.bench} · {bench.length}</Eyebrow></Box>
            <Stack gap={4} mt={6} p={8} style={{ border: `1px dashed ${over === "bench" ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, background: "var(--mantine-color-slate-7)" }} {...zone("bench", () => drop("bank"))}>
              {bench.map((s) => chip(s, null))}
            </Stack>
          </>}
        </Box>}
        <Stack gap="md" style={{ minWidth: 0 }}>
          {board.rosters.map((r, ri) => { const opens = openAt(ri); return (
            <Box key={r.n}>
              {board.rosters.length > 1 && <RosterHeader meta={meta} r={r} />}
              <Box className={css.groups}>
                {r.groups.map((g, gi) => {
                  // after the lock: the groups with someone in them, plus the first empty one to drop into
                  if (locked && g.length === 0 && r.groups.findIndex((x) => x.length === 0) !== gi) return null;
                  const sm = groupSummary(r.summary, gi);
                  const overCap = Object.entries(caps).filter(([role, cap]) => g.filter((s) => s.role === role).length > cap).map(([role]) => role);  // icon + count/cap, the cap in the tooltip
                  return (
                    <Box key={gi} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
                      <Group justify="space-between" mb={4}><Text size="sm" fw={700}>Group {gi + 1}</Text>
                        <Group gap={8} wrap="nowrap">
                          {overCap.map((role) => (
                            <Tooltip key={role} label={`at most ${caps[role]} per group`}>
                              <Group gap={3} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={14} title={role} /><Text size="xs" c="red" fw={700} style={{ fontVariantNumeric: "tabular-nums" }}>{g.filter((s) => s.role === role).length}/{caps[role]}</Text></Group>
                            </Tooltip>
                          ))}
                          <Text size="xs" c="dimmed">{g.length}/{board.group_size}</Text>
                        </Group>
                      </Group>
                      {Array.from({ length: board.group_size }, (_, i) => {
                        const id = `${ri}:${gi}:${i}`;
                        const s = g[i];
                        const want = s ? undefined : opens[`${gi}:${i}`];  // a seat a minimum still wants: role icon + the words
                        if (readOnly && !s && !want) return null;
                        return (
                          <Box key={i} {...zone(id, () => drop({ r: ri, g: gi, i }))} style={{ minHeight: 30, margin: "2px 0", borderRadius: 6, border: `1px dashed ${over === id ? "var(--mantine-color-teal-4)" : s ? "transparent" : want ? "var(--mantine-color-red-8)" : "var(--mantine-color-slate-5)"}`, display: "flex", alignItems: "center", paddingLeft: s ? 0 : 8 }}>
                            {s ? chip(s, { r: ri, g: gi }, conf[s.display_name])
                              : want ? <Group gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={want} size={14} title={want} /><Text size="sm" c="red.4">{meta.page_words.open_seat}</Text></Group>
                              : <Tooltip label="open seat"><Box style={{ alignSelf: "stretch", flex: 1 }} aria-label="open seat" /></Tooltip>}
                          </Box>
                        );
                      })}
                      {sm && (
                        <Group gap={4} mt={8} style={{ minHeight: 30 }}>
                          {sm.present.map((a) => <Aura key={a.abbr} a={a} who={a.who} />)}
                          {sm.missing.map((a) => <Aura key={`m${a.abbr}`} a={a} missing />)}
                        </Group>
                      )}
                    </Box>
                  );
                })}
              </Box>
              <RaidWide sm={r.summary} />
              {!event && r.advisories.length > 0 && <Text size="xs" c="dimmed" mt={4}>{r.advisories.join(" · ")}</Text>}
            </Box>
          ); })}
        </Stack>
      </Box>
      {canSplit && <SplitModal e={e} meta={meta} multi={multi} opened={splitOpen} onClose={() => setSplitOpen(false)} onUse={async (layout, strategy) => { await commit(layout.map(() => []).length ? chunk(layout, board.n_groups) : [], strategy); }} />}
      {confirmDialog}
      {boxDialog}
    </Box>
  );
}

/** The board's one legend (ⓘ): what its marks mean and how a drag works — said once, not under every card. */
function Legend({ meta, locked }: { meta: Meta; locked: boolean }) {
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
          {row(sq({ border: "2px solid var(--mantine-color-slate-5)", opacity: 0.6 }), "plain grey: nobody in the raid brings it (hover for its name)")}
          {row(sq({ border: "2px solid var(--mantine-color-teal-4)", opacity: 0.8 }), "teal edge: covered by a raid-wide buff of the same family")}
          {row(<PinMark meta={meta} pin="in" />, meta.run_words.pins.in)}
          {row(<PinMark meta={meta} pin="out" />, meta.run_words.pins.out)}
          {locked && row(<Group gap={4} wrap="nowrap">{["yes", "waiting", "no"].map((k) => <Text key={k} span size="xs">{markOf(meta, k === "waiting" ? null : k).mark}</Text>)}</Group>,
            ["yes", "waiting", "no", "expired"].map((k) => `${meta.marks[k].mark} ${meta.marks[k].words}`).join(" · ") + " — the Discord card's marks")}
          <Text size="xs" c="dimmed">Totems count one per element.</Text>
          <Text size="xs" c="dimmed">{locked
            ? "The board is the roster. Drag a name, or use its ⋮ menu's Move to… — the same move either way, and the confirm says what it does for the person."
            : "Drag a name into a group, or use its ⋮ menu's Move to…; back to not placed takes it out again, and dropping on someone swaps the two."}</Text>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
