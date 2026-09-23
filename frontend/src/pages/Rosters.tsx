import { useEffect, useState } from "react";
import { DateTimePicker } from "@mantine/dates";
import { Badge, Box, Button, Card, Group, Select, Stack, Tabs, Text, Tooltip } from "@mantine/core";
import { IconSparkles } from "@tabler/icons-react";
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
  const load = (): Promise<void> => api.get<Rosters>("/api/rosters").then(setData).catch((e) => { fail(e); });
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load, ["run:", "member"]);  // a save anywhere (another officer, Discord, the scheduler) refreshes this page within a second
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

  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Rosters" intro={`One sheet per raid slot, opened on cadence. Shape the roster on the board before it locks; after lock, the board is the roster. Times are ${data.tz}; the page refreshes itself while open.`} />
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} busy={busy} onAct={act} onBoard={patchBoard} onReload={load} />)}
      {data.orphans.length > 0 && (
        <Card><Box p="md"><Eyebrow>Sheets not tied to a raid</Eyebrow>{data.orphans.map((e) => <Text key={e.key} size="sm">{runName(e)} · {e.state}</Text>)}</Box></Card>
      )}
    </Stack>
  );
}

/** The picker hands back "YYYY-MM-DD HH:mm:ss"; show it the way every other time on the site is shown. */
function fmt12(v: string): string {
  const d = new Date(v.replace(" ", "T"));
  return isNaN(d.getTime()) ? v : d.toLocaleString(undefined, { weekday: "short", day: "2-digit", month: "short", hour: "numeric", minute: "2-digit", hour12: true });
}

/** Open a run: pick a date and time, or take the raid's next scheduled run — of any schedule, or of the one picked; a
 *  pickup template opens at the time picked. Nobody types a timestamp, and a raid with no schedule yet is a prompt to
 *  pick a time rather than an error after the fact. */
function OpenRaid({ r, busy, onAct, when, setWhen }: { r: RaidRuns; busy: string | null; onAct: Act; when: string | null; setWhen: (v: string | null) => void }) {
  const [sched, setSched] = useState<string>("");  // "" = the next run of any schedule (or the raid's own cadence at a picked time)
  const scheds = (r.schedules || []).filter((s) => s.active);
  const chosen = scheds.find((s) => s.id === sched);
  const next = r.upcoming.find((u) => !sched || u.schedule === sched);
  const picked = (when || "").trim();
  const needsTime = chosen?.kind === "pickup";
  const hint = picked ? `Opens ${chosen ? chosen.name : "a run"} starting ${fmt12(picked)}` : needsTime ? `${chosen?.name} is a template: pick when it starts`
    : next ? `The next run${chosen ? ` of ${chosen.name}` : ""}: ${next.start}` : "Pick a date and time, or add a schedule on the Raids page";
  return (
    <Group gap="xs" align="center" wrap="wrap">
      {(scheds.length > 1 || scheds.some((s) => s.kind === "pickup")) && (
        <Select size="sm" w={200} allowDeselect={false} aria-label="which schedule" value={sched} onChange={(v) => setSched(v || "")}
          data={[{ value: "", label: "Any schedule" }, ...scheds.map((s) => ({ value: s.id, label: s.kind === "pickup" ? `${s.name} (template)` : s.name }))]} />
      )}
      <DateTimePicker
        size="sm" w={240} clearable value={when} onChange={setWhen}
        placeholder={next && !needsTime ? `next: ${next.start}` : "pick a date and time"}
        minDate={new Date().toISOString().slice(0, 10)}
        valueFormat="ddd DD MMM h:mm A" timePickerProps={{ format: "12h", withDropdown: true }}
        popoverProps={{ withinPortal: true }} aria-label="when the run starts"
      />
      <Tooltip label={hint} withinPortal>
        <Button size="sm" leftSection={<IconSparkles size={15} />} loading={busy === `open${r.id}`} disabled={!picked && (!next || needsTime)}
          onClick={() => onAct(`open${r.id}`, `/api/raid/${r.id}/open`, { when: picked, schedule: sched })}>
          {picked ? "Open that run" : next && !needsTime ? "Open next slot" : "Open a run"}
        </Button>
      </Tooltip>
    </Group>
  );
}

function RaidCard({ r, meta, busy, onAct, onBoard, onReload }: { r: RaidRuns; meta: Meta; busy: string | null; onAct: Act; onBoard: OnBoard; onReload: () => Promise<void> }) {
  const [when, setWhen] = useState<string | null>(null);  // "YYYY-MM-DD HH:mm:ss" from the picker; empty = take the next slot
  const scheds = (r.schedules || []).filter((s) => s.active && s.kind !== "pickup");
  const metaLine = [scheds.length ? scheds.map((s) => (scheds.length > 1 ? `${s.name}: ${s.label}` : s.label)).join(" · ") : "no schedule set", r.opened ? null : r.first_open ? `opens ${r.first_open}` : "no opening date", `${r.open.length} open · ${r.locked.length} locked`].filter(Boolean).join(" · ");
  const target = window.location.hash.startsWith("#run-") ? window.location.hash.slice(5) : "";
  const first = r.locked.some((e) => e.key === target) ? "locked" : r.open.length ? "open" : r.locked.length ? "locked" : "upcoming";
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={<OpenRaid r={r} busy={busy} onAct={onAct} when={when} setWhen={setWhen} />} />
      <Tabs defaultValue={first} px="md" pb="md">
        <Tabs.List>
          <Tabs.Tab value="open" rightSection={r.open.length ? <Badge size="xs" color="teal" variant="light">{r.open.length}</Badge> : null}>Open for signup</Tabs.Tab>
          <Tabs.Tab value="locked" rightSection={r.locked.length ? <Badge size="xs" color="yellow" variant="light">{r.locked.length}</Badge> : null}>Locked</Tabs.Tab>
          <Tabs.Tab value="upcoming">Upcoming</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="open" pt="md">
          {r.open.length === 0 && <Text size="sm" c="dimmed">No sheet open. The next slot opens on its own {r.upcoming[0] ? `${r.upcoming[0].opens}` : "once a schedule is set on Raids"}, or open one now.</Text>}
          <Stack gap="md">{r.open.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="locked" pt="md">
          {r.locked.length === 0 && <Text size="sm" c="dimmed">Nothing locked.</Text>}
          <Stack gap="md">{r.locked.map((e) => <SheetCard key={e.key} e={e} meta={meta} busy={busy} onAct={onAct} onBoard={onBoard} onReload={onReload} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="upcoming" pt="md">
          {r.upcoming.length === 0 && <Text size="sm" c="dimmed">No upcoming runs{scheds.length ? " before the raid opens" : " — add a schedule on Raids"}.</Text>}
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
