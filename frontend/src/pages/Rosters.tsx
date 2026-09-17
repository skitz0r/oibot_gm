import { useEffect, useRef, useState } from "react";
import { Accordion, ActionIcon, Badge, Box, Button, Card, Group, Menu, Stack, Tabs, Text, TextInput, Tooltip } from "@mantine/core";
import { IconDotsVertical, IconLock, IconPin, IconPinnedOff, IconRefresh, IconSparkles, IconUserOff } from "@tabler/icons-react";
import { api, type Board, type GroupSummary, type Meta, type RaidRuns, type Rosters, type Seat, type Sheet, type Signup } from "../api";
import { GameIcon } from "../components/Icons";
import { RaidWide } from "../components/GroupsBlock";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { CLASS_COLOURS } from "../theme";
import css from "./rosters.module.css";

const ANSWER: Record<string, { mark: string; colour: string; label: string }> = { yes: { mark: "✓", colour: "var(--mantine-color-teal-4)", label: "confirmed" }, no: { mark: "✗", colour: "var(--mantine-color-red-5)", label: "can't make it" }, expired: { mark: "⌛", colour: "var(--mantine-color-slate-3)", label: "no answer" } };

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<Rosters>("/api/rosters").then(setData).catch(fail);
  useEffect(() => { load(); }, []);

  async function act(key: string, path: string, payload?: unknown) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload ?? {}); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  /** Board edits answer with the new board; patch it into the sheet without a full reload. */
  function patchBoard(key: string, board: Board, needs?: Sheet["needs"]) {
    setData((d) => d && { ...d, raids: d.raids.map((r) => ({ ...r, open: r.open.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs, has_layout: true } : e)), locked: r.locked.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs } : e)) })) });
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Rosters" intro={`One sheet per raid slot, opened on cadence. Shape the roster on the board before it locks; after lock, the board is the roster. Times are ${data.tz}.`} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} busy={busy} onAct={act} onBoard={patchBoard} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{e.key} · {e.state} · {e.when}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

type Act = (k: string, p: string, b?: unknown) => Promise<void>;
type OnBoard = (key: string, board: Board, needs?: Sheet["needs"]) => void;

function RaidCard({ r, meta, busy, onAct, onBoard }: { r: RaidRuns; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard }) {
  const [when, setWhen] = useState("");
  const metaLine = [r.slots.length ? `slots ${r.slots.join(", ")}` : "no slots set", r.opened ? null : r.first_open ? `opens ${r.first_open}` : "no opening date", `${r.open.length} open · ${r.locked.length} locked`].filter(Boolean).join(" · ");
  const first = r.open.length ? "open" : r.locked.length ? "locked" : "upcoming";
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={
        <Group gap="xs">
          <Button variant="default" size="sm" leftSection={<IconSparkles size={15} />} loading={busy === `open${r.id}`} onClick={() => onAct(`open${r.id}`, `/api/raid/${r.id}/open`, { when })}>Open {when ? "at that time" : "next slot"} now</Button>
          <TextInput size="sm" w={170} placeholder="2026-12-10 19:30" value={when} onChange={(e) => setWhen(e.currentTarget.value)} />
        </Group>} />
      <Tabs defaultValue={first} px="md" pb="md">
        <Tabs.List>
          <Tabs.Tab value="open" rightSection={r.open.length ? <Badge size="xs" color="teal" variant="light">{r.open.length}</Badge> : null}>Open for signup</Tabs.Tab>
          <Tabs.Tab value="locked" rightSection={r.locked.length ? <Badge size="xs" color="yellow" variant="light">{r.locked.length}</Badge> : null}>Locked</Tabs.Tab>
          <Tabs.Tab value="upcoming">Upcoming</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="open" pt="md">
          {r.open.length === 0 && <Text size="sm" c="dimmed">No sheet open. The next slot opens on its own {r.upcoming[0] ? `${r.upcoming[0].opens}` : "once slots are set on Raids"}, or open one now.</Text>}
          <Stack gap="md">{r.open.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {r.locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{r.locked.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="upcoming" pt="md">
          {r.upcoming.length === 0 && <Text size="sm" c="dimmed">No upcoming slots{r.slots.length ? " before the raid opens" : " — set slots on Raids"}.</Text>}
          <Stack gap={4}>{r.upcoming.map((u) => <Text key={u.start} size="sm"><b>{u.start}</b> <Text span c="dimmed">· {u.slot} · sheet opens {u.opens}</Text></Text>)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          {r.past.length === 0 && <Text size="sm" c="dimmed">No past runs.</Text>}
          <Stack gap={4}>{r.past.map((e) => <Text key={e.key} size="sm"><Badge size="xs" variant="outline" color={e.state === "done" ? "teal" : "gray"} mr={6}>{e.state}</Badge>{e.when} · {e.counts.in} joined · {e.seated} seated{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>)}</Stack>
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}

// ---------------------------------------------------------------- one sheet: header, signups by class, the board

function SheetCard({ e, meta, busy, onAct, onBoard }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard }) {
  const locked = e.state !== "open";
  const n = e.needs;
  const signups = e.signups || [];
  const joined = signups.filter((s) => s.status === "in");
  const roles = { tank: 0, healer: 0, melee: 0, ranged: 0 } as Record<string, number>;
  joined.forEach((s) => { roles[s.role] = (roles[s.role] || 0) + 1; });
  const byClass = Object.entries(joined.reduce((m, s) => { (m[s.cls] ||= []).push(s); return m; }, {} as Record<string, Signup[]>)).sort((a, b) => b[1].length - a[1].length);
  const conf = e.confirmations || [];
  const counts = { yes: conf.filter((c) => c.answer === "yes").length, pending: conf.filter((c) => c.answer === null).length, no: conf.filter((c) => c.answer === "no" || c.answer === "expired").length };
  const short = n ? [n.headcount ? `${n.headcount} seat${n.headcount === 1 ? "" : "s"}` : null, ...Object.entries(n.roles).map(([role, k]) => `${k} ${role}`)].filter(Boolean).join(", ") : "";

  const pin = (s: Signup, p: "in" | "out" | null) => onAct(`pin${s.uid}`, `/api/run/${e.key}/pin`, { uid: s.uid, pin: p });
  const set = (uid: string, status: string) => onAct(`set${uid}`, `/api/run/${e.key}/set`, { uid, status });
  const menu = (s: Signup) => (
    <Menu shadow="md" width={200} position="bottom-end">
      <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`actions for ${s.display_name}`}><IconDotsVertical size={14} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{s.display_name}</Menu.Label>
        {!locked && s.pin !== "in" && s.status === "in" && <Menu.Item leftSection={<IconPin size={14} />} onClick={() => pin(s, "in")}>Pin to roster</Menu.Item>}
        {!locked && s.pin !== "out" && s.status !== "out" && <Menu.Item leftSection={<IconUserOff size={14} />} onClick={() => pin(s, "out")}>Keep on bench</Menu.Item>}
        {!locked && s.pin && <Menu.Item leftSection={<IconPinnedOff size={14} />} onClick={() => pin(s, null)}>Unpin</Menu.Item>}
        {!locked && <Menu.Divider />}
        {(["in", "sub", "out"] as const).filter((st) => st !== s.status).map((st) => <Menu.Item key={st} onClick={() => set(s.uid, st)}>Set {meta.labels[st]}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );

  return (
    <Box p="md" style={{ border: `1px solid ${locked ? "var(--mantine-color-yellow-5)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap" align="flex-start">
        <Box>
          <Group gap="sm"><Text size="lg" fw={700}>{e.when}</Text>{locked && <Badge variant="light" color="yellow">locked</Badge>}</Group>
          {e.timeline && (
            <Text size="xs" c="dimmed" mt={2}>
              🕒 {e.rel}{!locked && <> · ❗ nudges {e.timeline.nudge}</>} · 🔒 {locked ? "locked" : `locks ${e.timeline.lock}`} · ✓ confirm by {e.timeline.confirm}
            </Text>
          )}
        </Box>
        <Group gap="xs">
          {!locked && <Button size="xs" leftSection={<IconLock size={13} />} loading={busy === `lock${e.key}`} onClick={() => confirm(`Lock ${e.key} now? The board becomes the roster and everyone seated gets a confirmation DM.`) && onAct(`lock${e.key}`, `/api/run/${e.key}/lock`)}>Lock now</Button>}
          {locked && <Button size="xs" variant="default" loading={busy === `fill${e.key}`} onClick={() => onAct(`fill${e.key}`, `/api/run/${e.key}/fill`)}>Ask the bench</Button>}
          <Button size="xs" variant="subtle" color="red" loading={busy === `cancel${e.key}`} onClick={() => confirm(`Cancel ${e.key}?`) && onAct(`cancel${e.key}`, `/api/run/${e.key}/cancel`)}>Cancel run</Button>
        </Group>
      </Group>

      <Group gap="xs" mt="sm" align="baseline">
        <Text fw={700} style={{ fontSize: 22, fontVariantNumeric: "tabular-nums" }}>{locked ? e.seated : joined.length}<Text span c="dimmed" fw={500} size="sm"> / {e.size}{e.n_rosters > 1 ? ` × ${e.n_rosters}` : ""}</Text></Text>
        {locked && <Group gap={6} ml="sm"><Badge variant="light" color="teal">✓ {counts.yes}</Badge><Badge variant="light" color="gray">⏳ {counts.pending}</Badge><Badge variant="light" color="red">✗ {counts.no}</Badge><Text size="xs" c="dimmed">fill {e.fill_state}</Text></Group>}
      </Group>
      <Group gap="md" mt={4}>
        {Object.entries(roles).map(([role, k]) => <Group key={role} gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={18} /><Text size="sm">{k} {role}</Text></Group>)}
        {(e.double_booked || []).length > 0 && <Tooltip label={e.double_booked!.join(", ")}><Badge variant="light" color="yellow">double-booked {e.double_booked!.length}</Badge></Tooltip>}
      </Group>

      <Group gap="xl" mt="md" align="flex-start" wrap="wrap">
        {byClass.length === 0 && <Text size="sm" c="dimmed">Nobody has joined yet.</Text>}
        {byClass.map(([cls, ss]) => (
          <Box key={cls} style={{ minWidth: 0 }}>
            <Group gap={6} mb={2}><GameIcon meta={meta} kind="class" id={cls} size={20} /><Text size="sm" fw={600}>{cls}</Text><Text size="sm" c="dimmed">({ss.length})</Text></Group>
            {ss.map((s) => <SeatLine key={s.uid} meta={meta} s={s} right={<>{s.pin && <Badge size="xs" variant="light" color={s.pin === "in" ? "teal" : "red"}>{s.pin === "in" ? "pinned" : "bench"}</Badge>}{menu(s)}</>} />)}
          </Box>
        ))}
      </Group>
      <ChipRow label={`${meta.labels.sub} (${signups.filter((s) => s.status === "sub").length})`} items={signups.filter((s) => s.status === "sub")} meta={meta} menu={menu} />
      <ChipRow label={`${meta.labels.out} (${signups.filter((s) => s.status === "out").length})`} items={signups.filter((s) => s.status === "out")} meta={meta} menu={menu} />
      <Group gap="xs" mt={8} justify="space-between" wrap="wrap">
        <Group gap="xs"><Eyebrow>Away that day</Eyebrow>{(e.absences || []).length === 0 ? <Text size="sm" c="dimmed">nobody</Text> : e.absences!.map((a) => <Text key={a.display_name} size="sm">{a.display_name}{a.reason ? <Text span c="dimmed"> ({a.reason})</Text> : null}</Text>)}</Group>
        {short ? <Badge variant="filled" color="red">short: {short}</Badge> : <Badge variant="light" color="teal">full</Badge>}
      </Group>

      {e.board && <BoardView e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} />}
      <Detail e={e} />
    </Box>
  );
}

function SeatLine({ meta, s, right, mark }: { meta: Meta; s: Seat | Signup; right?: React.ReactNode; mark?: string | null }) {
  const a = mark !== undefined && mark !== null ? ANSWER[mark] : null;
  return (
    <Group gap={6} wrap="nowrap" style={{ lineHeight: 1.8, minWidth: 0 }}>
      {mark !== undefined && <Tooltip label={a ? a.label : "waiting for confirmation"}><Text span size="xs" style={{ width: 14, textAlign: "center", color: a ? a.colour : "var(--mantine-color-slate-3)" }}>{a ? a.mark : "⏳"}</Text></Tooltip>}
      <GameIcon meta={meta} kind="role" id={s.role} size={16} title={s.role} />
      <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={16} title={`${s.cls} ${s.spec}`} />
      <Text size="sm" fw={600} c={CLASS_COLOURS[s.cls]} truncate>{s.character}</Text>
      <Text size="xs" c="dimmed" truncate>{s.display_name}</Text>
      {right}
    </Group>
  );
}

function ChipRow({ label, items, meta, menu }: { label: string; items: Signup[]; meta: Meta; menu: (s: Signup) => React.ReactNode }) {
  return (
    <Group gap="xs" mt={8} align="center">
      <Eyebrow>{label}</Eyebrow>
      {items.length === 0 && <Text size="sm" c="dimmed">—</Text>}
      {items.map((s) => (
        <Group key={s.uid} gap={4} wrap="nowrap" px={6} py={2} style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 7, background: "var(--mantine-color-slate-6)" }}>
          <SeatLine meta={meta} s={s} />{menu(s)}
        </Group>
      ))}
    </Group>
  );
}

// ---------------------------------------------------------------- the board: bank + groups, drag and drop

type Drag = { name: string; from: { r: number; g: number } | null };

function BoardView({ e, meta, busy, onAct, onBoard }: { e: Sheet; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard }) {
  const board = e.board!;
  const locked = e.state !== "open";
  const conf = Object.fromEntries((e.confirmations || []).map((c) => [c.display_name, c.answer]));
  const drag = useRef<Drag | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const layout = () => board.rosters.map((r) => r.groups.map((g) => g.map((s) => s.display_name)));

  async function commit(groups: string[][][]) {
    setSaving(true);
    try {
      const r = await api.post<{ board: Board; needs: Sheet["needs"]; message?: string }>(`/api/run/${e.key}/layout`, { groups: groups.flat() });
      onBoard(e.key, r.board, r.needs);
      if (r.message) ok(r.message);
    } catch (err) { fail(err); } finally { setSaving(false); }
  }

  function drop(target: { r: number; g: number; i: number } | "bank") {
    const d = drag.current; drag.current = null; setOver(null);
    if (!d) return;
    const L = layout();
    if (d.from) L[d.from.r][d.from.g] = L[d.from.r][d.from.g].filter((n) => n !== d.name);
    if (target === "bank") {
      if (!d.from) return;
      if (locked && !confirm(`Take ${d.name} off the roster? The seat is freed and the bench is asked.`)) return;
      commit(L); return;
    }
    const g = L[target.r][target.g];
    const displaced = g[target.i];
    if (displaced && displaced !== d.name) {
      g.splice(target.i, 1, d.name);
      if (d.from) L[d.from.r][d.from.g].push(displaced); // swap
      else if (!locked) { /* from bank: displaced goes back to bank */ }
      else if (!confirm(`Swap ${displaced} out for ${d.name}? ${displaced}'s seat is freed and ${d.name} is asked to confirm.`)) return;
    } else if (g.length >= board.group_size) {
      if (d.from) L[d.from.r][d.from.g].push(d.name);
      return;
    } else {
      g.splice(Math.min(target.i, g.length), 0, d.name);
      if (locked && !d.from && !confirm(`Seat ${d.name}? They'll be asked to confirm by DM.`)) return;
    }
    commit(L);
  }
  const onDragStart = (name: string, from: Drag["from"]) => (ev: React.DragEvent) => { drag.current = { name, from }; ev.dataTransfer.effectAllowed = "move"; };
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
        <Group gap="sm"><Eyebrow>Roster builder</Eyebrow>{saving && <Text size="xs" c="dimmed">saving…</Text>}{locked && <Text size="xs" c="dimmed">the board is the roster: group moves are free, dragging in from the bench asks that person to confirm, dragging out frees the seat</Text>}</Group>
        {!locked && (
          <Group gap="xs">
            <Button size="xs" variant="default" leftSection={<IconSparkles size={13} />} loading={busy === `auto${e.key}`} onClick={() => onAct(`auto${e.key}`, `/api/run/${e.key}/autofill`)}>Auto-fill empty seats</Button>
            <Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} disabled={saving} onClick={() => commit(board.rosters.map((r) => r.groups.map(() => [])))}>Clear</Button>
          </Group>
        )}
      </Group>
      <Box className={css.board}>
        <Box>
          <Eyebrow>Bank · {board.bank.length} unplaced</Eyebrow>
          <Stack gap={4} mt={6} p={8} style={{ minHeight: 120, border: `1px dashed ${over === "bank" ? "var(--mantine-color-teal-4)" : "var(--mantine-color-slate-5)"}`, borderRadius: 8, background: "var(--mantine-color-slate-7)" }} {...zone("bank", () => drop("bank"))}>
            {board.bank.length === 0 && <Text size="xs" c="dimmed">everyone who joined is placed</Text>}
            {board.bank.map((s) => chip(s, null))}
          </Stack>
          <Text size="xs" c="dimmed" mt={6}>Drag into a group. Drag a placed name back here to bench them. Dropping on someone swaps.</Text>
        </Box>
        <Stack gap="md" style={{ minWidth: 0 }}>
          {board.rosters.map((r, ri) => (
            <Box key={r.n}>
              {board.rosters.length > 1 && <Group gap="sm" mb={4}><Text size="sm" fw={700}>Roster {r.n}</Text><Text size="xs" c="dimmed">{r.seated} seated</Text></Group>}
              <Box className={css.groups}>
                {r.groups.map((g, gi) => {
                  const sm = groupSummary(r.summary, gi);
                  const tanks = g.filter((s) => s.role === "tank").length, heals = g.filter((s) => s.role === "healer").length;
                  return (
                    <Box key={gi} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
                      <Group justify="space-between" mb={4}><Text size="sm" fw={700}>Group {gi + 1}</Text><Text size="xs" c={tanks > 2 || heals > 3 ? "red" : "dimmed"}>{g.length}/{board.group_size}{tanks > 2 ? ` · ${tanks} tanks` : ""}{heals > 3 ? ` · ${heals} healers` : ""}</Text></Group>
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
      <Text size="xs" c="dimmed" mt={6}>Coloured = aura present in that group · greyed with red edge = someone in the group wants it and nobody brings it · totems one per element.</Text>
    </Box>
  );
}

function Aura({ a, missing, who }: { a: { abbr: string; colour: string; art: string | null; name: string }; missing?: boolean; who?: string }) {
  const tip = `${a.name}${missing ? " — wanted here, nobody brings it" : who ? ` — ${who}` : ""}`;
  return (
    <Tooltip label={tip}>
      {a.art ? <Box component="img" src={`/img/icon/${a.art}.jpg`} alt={a.abbr} style={{ width: 26, height: 26, borderRadius: 5, border: `2px solid ${missing ? "var(--mantine-color-red-5)" : "transparent"}`, opacity: missing ? 0.35 : 1, filter: missing ? "grayscale(1)" : undefined }} />
        : <Box style={{ minWidth: 30, textAlign: "center", padding: "2px 5px", borderRadius: 6, fontSize: 11, fontWeight: 700, color: missing ? "var(--mantine-color-red-5)" : "#14181F", background: missing ? "transparent" : a.colour, border: missing ? "2px solid var(--mantine-color-red-5)" : undefined }}>{a.abbr}</Box>}
    </Tooltip>
  );
}

function Detail({ e }: { e: Sheet }) {
  const fa = e.fill_asks || [], co = e.callouts || [], log = e.log || [];
  return (
    <Accordion variant="contained" mt="md" chevronPosition="left">
      <Accordion.Item value="detail">
        <Accordion.Control><Text size="xs" c="dimmed">fill asks ({fa.length}) · callouts ({co.length}) · log ({log.length})</Text></Accordion.Control>
        <Accordion.Panel>
          {fa.length > 0 && <Stack gap={2} mb="sm">{fa.map((a, i) => <Text key={i} size="xs">{a.display_name} · {a.kind} · {a.character} ({a.spec}, {a.role}) · {a.reason} · <Text span c={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "yellow"}>{a.answer || "waiting"}</Text></Text>)}</Stack>}
          {co.length > 0 && <Text size="xs" c="dimmed" mb="sm">{co.map((c) => `${c.display_name} ${c.hours_before}h before${c.late ? " (after lock)" : ""}`).join(" · ")}</Text>}
          <Box component="pre" style={{ fontSize: 12, whiteSpace: "pre-wrap", margin: 0, fontFamily: "var(--mantine-font-family-monospace)", color: "var(--mantine-color-slate-2)" }}>{log.join("\n")}</Box>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
