import { useEffect, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, Group, Stack, Tabs, Text, TextInput, Tooltip } from "@mantine/core";
import { IconRefresh, IconSparkles } from "@tabler/icons-react";
import { api, type Board, type Meta, type RaidRuns, type Rosters, type Sheet } from "../api";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { SheetCard } from "../components/board/SheetCard";
import { runName, type Act, type OnBoard } from "../components/board/shared";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = (): Promise<void> => api.get<Rosters>("/api/rosters").then(setData).catch((e) => { fail(e); });
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load);  // a save anywhere (another officer, Discord, the scheduler) refreshes this page within a second
  // deep link from Discord cards: /app/rosters#run-<key> scrolls to that run once it has rendered
  useEffect(() => {
    if (!data || !window.location.hash.startsWith("#run-")) return;
    const t = setTimeout(() => document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
    return () => clearTimeout(t);
  }, [data]);

  async function act(key: string, path: string, payload?: unknown) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload ?? {}); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  /** Board edits answer with the new board; patch it into the sheet without a full reload. */
  function patchBoard(key: string, board: Board, needs?: Sheet["needs"], rev?: string) {
    setData((d) => d && { ...d, raids: d.raids.map((r) => ({ ...r, open: r.open.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs, rev: rev ?? e.rev, has_layout: true } : e)), locked: r.locked.map((e) => (e.key === key ? { ...e, board, needs: needs ?? e.needs, rev: rev ?? e.rev } : e)) })) });
  }
  async function refresh() { setRefreshing(true); try { await load(); } finally { setRefreshing(false); } }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Rosters" intro={`One sheet per raid slot, opened on cadence. Shape the roster on the board before it locks; after lock, the board is the roster. Times are ${data.tz}; the page refreshes itself while open.`}
        right={<Tooltip label="Refresh"><ActionIcon variant="default" size="lg" aria-label="refresh" loading={refreshing} onClick={refresh}><IconRefresh size={16} /></ActionIcon></Tooltip>} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} busy={busy} onAct={act} onBoard={patchBoard} onReload={load} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{runName(e)} · {e.state}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

function RaidCard({ r, meta, busy, onAct, onBoard, onReload }: { r: RaidRuns; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void> }) {
  const [when, setWhen] = useState("");
  const metaLine = [r.slots.length ? `slots ${r.slots.join(", ")}` : "no slots set", r.opened ? null : r.first_open ? `opens ${r.first_open}` : "no opening date", `${r.open.length} open · ${r.locked.length} locked`].filter(Boolean).join(" · ");
  const target = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
  const first = r.locked.some((e) => e.key === target) ? "locked" : r.open.length ? "open" : r.locked.length ? "locked" : "upcoming";
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
          <Stack gap="md">{r.open.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {r.locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{r.locked.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="upcoming" pt="md">
          {r.upcoming.length === 0 && <Text size="sm" c="dimmed">No upcoming slots{r.slots.length ? " before the raid opens" : " — set slots on Raids"}.</Text>}
          <Stack gap={4}>{r.upcoming.map((u) => <Text key={u.start} size="sm"><b>{u.start}</b> <Text span c="dimmed">· {u.slot} · sheet opens {u.opens}</Text></Text>)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          {r.past.length === 0 && <Text size="sm" c="dimmed">No past runs.</Text>}
          <Stack gap={4}>{r.past.map((e) => <Text key={e.key} size="sm"><Badge size="xs" variant="outline" color={e.state === "done" ? "teal" : "gray"} mr={6}>{e.state}</Badge>{e.when} · {e.counts.in} joined · {e.rostered} rostered{e.n_rosters > 1 ? ` in ${e.n_rosters} rosters` : ""}</Text>)}</Stack>
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}
