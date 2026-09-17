import { useEffect, useState } from "react";
import { Accordion, ActionIcon, Badge, Box, Button, Card, Group, Menu, SimpleGrid, Stack, Table, Tabs, Text, TextInput, Tooltip } from "@mantine/core";
import { IconDotsVertical, IconLock, IconPin, IconPinnedOff, IconRefresh, IconSparkles, IconUserOff } from "@tabler/icons-react";
import { api, type Draft, type Meta, type RaidRuns, type RosterOut, type Rosters, type Seat, type Sheet, type Signup } from "../api";
import { GameIcon } from "../components/Icons";
import { GroupsBlock } from "../components/GroupsBlock";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { CLASS_COLOURS } from "../theme";

const ANSWER: Record<string, { label: string; colour: string }> = { yes: { label: "confirmed", colour: "teal" }, no: { label: "can't", colour: "red" }, expired: { label: "no answer", colour: "gray" } };

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const load = () => api.get<Rosters>("/api/rosters").then((d) => { setData(d); d.raids.forEach((r) => r.open.forEach((e) => { if (e.draft) setDrafts((x) => ({ ...x, [e.key]: e.draft! })); })); }).catch(fail);
  useEffect(() => { load(); }, []);

  async function act(key: string, path: string, payload?: unknown) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload ?? {}); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function draft(key: string) {
    setBusy(`draft${key}`);
    try { const r = await api.post<{ draft: Draft }>(`/api/run/${key}/draft`, {}); setDrafts((x) => ({ ...x, [key]: r.draft })); } catch (e) { fail(e); } finally { setBusy(null); }
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Rosters" intro={`One sheet per raid slot, opened on cadence. Shape the roster here before it locks; after lock, watch confirmations and freed seats. Times are ${data.tz}.`} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} busy={busy} drafts={drafts} onAct={act} onDraft={draft} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{e.key} · {e.state} · {e.when}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

function RaidCard({ r, meta, busy, drafts, onAct, onDraft }: { r: RaidRuns; meta: Meta; busy: string | null; drafts: Record<string, Draft>; onAct: (k: string, p: string, b?: unknown) => Promise<void>; onDraft: (k: string) => Promise<void> }) {
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
          <Stack gap="md">{r.open.map((e) => <OpenSheet key={e.key} e={e} meta={meta} busy={busy} draft={drafts[e.key]} onAct={onAct} onDraft={() => onDraft(e.key)} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {r.locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{r.locked.map((e) => <LockedSheet key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} />)}</Stack>
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

function SeatLine({ meta, s, right }: { meta: Meta; s: Seat | Signup; right?: React.ReactNode }) {
  return (
    <Group gap={6} wrap="nowrap" style={{ lineHeight: 1.7 }}>
      <GameIcon meta={meta} kind="role" id={s.role} size={18} title={s.role} />
      <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={`${s.cls} ${s.spec}`} />
      <Text size="sm" c={CLASS_COLOURS[s.cls]} truncate>{s.character}</Text>
      <Text size="xs" c="dimmed" truncate>{s.display_name}</Text>
      {right}
    </Group>
  );
}

function OpenSheet({ e, meta, busy, draft, onAct, onDraft }: { e: Sheet; meta: Meta; busy: string | null; draft?: Draft; onAct: (k: string, p: string, b?: unknown) => Promise<void>; onDraft: () => Promise<void> }) {
  const n = e.needs;
  const signups = e.signups || [];
  const by = (st: string) => signups.filter((s) => s.status === st);
  const pin = (s: Signup, p: "in" | "out" | null) => onAct(`pin${s.uid}`, `/api/run/${e.key}/pin`, { uid: s.uid, pin: p });
  const set = (uid: string, status: string) => onAct(`set${uid}`, `/api/run/${e.key}/set`, { uid, status });
  const actions = (s: Signup) => (
    <Menu shadow="md" width={200} position="bottom-end">
      <Menu.Target><ActionIcon variant="subtle" color="gray" size="sm" aria-label={`actions for ${s.display_name}`}><IconDotsVertical size={14} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{s.display_name}</Menu.Label>
        {s.pin !== "in" && s.status === "in" && <Menu.Item leftSection={<IconPin size={14} />} onClick={() => pin(s, "in")}>Pin to roster</Menu.Item>}
        {s.pin !== "out" && s.status !== "out" && <Menu.Item leftSection={<IconUserOff size={14} />} onClick={() => pin(s, "out")}>Keep on bench</Menu.Item>}
        {s.pin && <Menu.Item leftSection={<IconPinnedOff size={14} />} onClick={() => pin(s, null)}>Unpin</Menu.Item>}
        <Menu.Divider />
        {(["in", "sub", "out"] as const).filter((st) => st !== s.status).map((st) => <Menu.Item key={st} onClick={() => set(s.uid, st)}>Set {meta.labels[st]}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );
  const pinBadge = (s: Signup) => s.pin ? <Badge size="xs" variant="light" color={s.pin === "in" ? "teal" : "red"} ml={4}>pinned {s.pin}</Badge> : null;
  return (
    <Box p="md" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap" mb="xs">
        <Group gap="xs" wrap="wrap">
          <Text size="sm" fw={700}>{e.when}</Text>
          <Badge variant="light" color="teal">{e.counts.in} joined</Badge>
          <Text size="xs" c="dimmed">{e.counts.sub} bench · {e.counts.out} out · {(e.not_answered || []).length} not answered</Text>
          {n && (n.headcount ? <Badge variant="light" color="red">short {n.headcount}</Badge> : <Badge variant="light" color="teal">full</Badge>)}
          {n && Object.entries(n.roles).map(([role, k]) => <Badge key={role} variant="light" color="red">{k} {role} short</Badge>)}
          {(e.double_booked || []).length > 0 && <Tooltip label={e.double_booked!.join(", ")}><Badge variant="light" color="yellow">double-booked {e.double_booked!.length}</Badge></Tooltip>}
        </Group>
        <Group gap="xs">
          <Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} loading={busy === `draft${e.key}`} onClick={onDraft}>{draft ? "Rebuild draft" : "Build draft"}</Button>
          <Button size="xs" leftSection={<IconLock size={13} />} loading={busy === `lock${e.key}`} onClick={() => confirm(`Lock ${e.key} now? Everyone seated gets a confirmation DM.`) && onAct(`lock${e.key}`, `/api/run/${e.key}/lock`)}>Lock now</Button>
        </Group>
      </Group>
      {e.timeline && <Text size="xs" c="dimmed" mb="sm">nudges {e.timeline.nudge} · locks {e.timeline.lock} · confirm by {e.timeline.confirm}</Text>}
      {(e.absences || []).length > 0 && <Text size="xs" c="yellow" mb="sm">Away that day: {e.absences!.map((a) => `${a.display_name}${a.reason ? ` (${a.reason})` : ""}`).join(", ")}</Text>}
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
        {(["in", "sub", "out"] as const).map((st) => (
          <Box key={st} style={{ minWidth: 0 }}>
            <Eyebrow>{meta.labels[st]} ({by(st).length})</Eyebrow>
            {by(st).length === 0 && <Text size="sm" c="dimmed">—</Text>}
            {by(st).map((s) => <SeatLine key={s.uid} meta={meta} s={s} right={<>{pinBadge(s)}<Box ml="auto">{actions(s)}</Box></>} />)}
          </Box>
        ))}
        <Box style={{ minWidth: 0 }}>
          <Eyebrow>Not answered ({(e.not_answered || []).length})</Eyebrow>
          {(e.not_answered || []).length === 0 && <Text size="sm" c="dimmed">—</Text>}
          {(e.not_answered || []).map((s) => <SeatLine key={s.uid!} meta={meta} s={s} right={<Box ml="auto"><Menu shadow="md" width={180} position="bottom-end"><Menu.Target><ActionIcon variant="subtle" color="gray" size="sm"><IconDotsVertical size={14} /></ActionIcon></Menu.Target><Menu.Dropdown>{(["in", "sub", "out"] as const).map((st) => <Menu.Item key={st} onClick={() => set(s.uid!, st)}>Set {meta.labels[st]}</Menu.Item>)}</Menu.Dropdown></Menu></Box>} />)}
        </Box>
      </SimpleGrid>
      <Box mt="md">
        <Group gap="sm"><Eyebrow>Draft roster</Eyebrow>{e.draft_stale && <Badge size="xs" variant="light" color="yellow">signups changed — rebuild</Badge>}</Group>
        {!draft && <Text size="sm" c="dimmed" mt={4}>Press Build draft to see who the solver would seat from the current signups. Pins are honoured. This is what Lock uses.</Text>}
        {draft && <RosterBlocks meta={meta} rosters={draft.rosters} bench={draft.bench} />}
      </Box>
      <Detail e={e} />
    </Box>
  );
}

function LockedSheet({ e, meta, busy, onAct }: { e: Sheet; meta: Meta; busy: string | null; onAct: (k: string, p: string, b?: unknown) => Promise<void> }) {
  const n = e.needs;
  const conf = e.confirmations || [];
  const counts = { yes: conf.filter((c) => c.answer === "yes").length, pending: conf.filter((c) => c.answer === null).length, no: conf.filter((c) => c.answer === "no" || c.answer === "expired").length };
  return (
    <Box p="md" style={{ border: "1px solid var(--mantine-color-yellow-5)", borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap" mb="xs">
        <Group gap="xs" wrap="wrap">
          <Text size="sm" fw={700}>{e.when}</Text>
          <Badge variant="light" color="yellow">locked</Badge>
          <Text size="xs" c="dimmed">{e.seated} seated{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>
          <Badge variant="light" color="teal">✓ {counts.yes}</Badge><Badge variant="light" color="gray">⏳ {counts.pending}</Badge><Badge variant="light" color="red">✗ {counts.no}</Badge>
          {n && (n.headcount ? <Badge variant="light" color="red">{n.headcount} seat{n.headcount === 1 ? "" : "s"} open</Badge> : <Badge variant="light" color="teal">full</Badge>)}
          {n && Object.entries(n.roles).map(([role, k]) => <Badge key={role} variant="light" color="red">{k} {role} short</Badge>)}
          <Text size="xs" c="dimmed">fill {e.fill_state}</Text>
        </Group>
        <Group gap="xs">
          <Button size="xs" variant="default" loading={busy === `fill${e.key}`} onClick={() => onAct(`fill${e.key}`, `/api/run/${e.key}/fill`)}>Ask the bench</Button>
          <Button size="xs" variant="subtle" color="red" loading={busy === `cancel${e.key}`} onClick={() => confirm(`Cancel ${e.key}?`) && onAct(`cancel${e.key}`, `/api/run/${e.key}/cancel`)}>Cancel run</Button>
        </Group>
      </Group>
      {e.timeline && <Text size="xs" c="dimmed" mb="sm">confirm by {e.timeline.confirm} · unanswered then counts as out</Text>}
      {(e.absences || []).length > 0 && <Text size="xs" c="yellow" mb="sm">Away that day: {e.absences!.map((a) => a.display_name).join(", ")}</Text>}
      <RosterBlocks meta={meta} rosters={e.rosters || []} bench={e.bench || []} onDrop={(s) => s.uid && confirm(`Take ${s.display_name} off the roster? The seat is freed and the bench is asked.`) && onAct(`drop${s.uid}`, `/api/run/${e.key}/set`, { uid: s.uid, status: "out" })} />
      <Detail e={e} />
    </Box>
  );
}

function RosterBlocks({ meta, rosters, bench, onDrop }: { meta: Meta; rosters: RosterOut[]; bench: Seat[]; onDrop?: (s: Seat) => void }) {
  return (
    <Stack gap="md" mt="xs">
      {rosters.map((r) => (
        <Box key={r.n}>
          <Group gap="sm"><Text size="sm" fw={600}>{rosters.length > 1 ? `Roster ${r.n}` : "Roster"} · {r.size} seated</Text><Text size="xs" c="dimmed">synergy {r.synergy}</Text></Group>
          <Table.ScrollContainer minWidth={480}>
            <Table verticalSpacing={2} horizontalSpacing="xs">
              <Table.Tbody>
                {r.seats.map((s) => (
                  <Table.Tr key={s.display_name}>
                    <Table.Td><SeatLine meta={meta} s={s} /></Table.Td>
                    <Table.Td w={120}>{s.answer !== undefined && s.answer !== null ? <Badge size="xs" variant="light" color={ANSWER[s.answer]?.colour || "gray"}>{ANSWER[s.answer]?.label || s.answer}</Badge> : onDrop ? <Badge size="xs" variant="light" color="gray">waiting</Badge> : null}</Table.Td>
                    <Table.Td w={40}>{onDrop && s.answer !== "no" && s.answer !== "expired" && <Tooltip label="free this seat"><ActionIcon size="sm" variant="subtle" color="red" onClick={() => onDrop(s)}><IconUserOff size={14} /></ActionIcon></Tooltip>}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          <GroupsBlock meta={meta} sm={r.summary} />
          {r.advisories.length > 0 && <Text size="xs" c="dimmed" mt={4}>{r.advisories.join(" · ")}</Text>}
        </Box>
      ))}
      {bench.length > 0 && <Text size="xs" c="dimmed">Bench: {bench.map((b) => b.character).join(", ")}</Text>}
    </Stack>
  );
}

function Detail({ e }: { e: Sheet }) {
  const fa = e.fill_asks || [], co = e.callouts || [], log = e.log || [];
  return (
    <Accordion variant="contained" mt="sm" chevronPosition="left">
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
