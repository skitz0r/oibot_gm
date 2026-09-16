import { useEffect, useState } from "react";
import { Accordion, Badge, Box, Button, Card, Group, SimpleGrid, Stack, Tabs, Text, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconSparkles } from "@tabler/icons-react";
import { api, type Meta, type Proposal, type RaidRuns, type Rosters, type Sheet } from "../api";
import { GameIcon } from "../components/Icons";
import { GroupsBlock } from "../components/GroupsBlock";
import { RaidHeader } from "../components/RaidHeader";
import { CLASS_COLOURS } from "../theme";

const ok = (message: string) => notifications.show({ message, color: "teal" });
const fail = (e: unknown) => notifications.show({ message: (e as Error).message || "Something went wrong", color: "red" });

export function RostersPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Rosters | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<Rosters>("/api/rosters").then(setData).catch(fail);
  useEffect(() => { load(); }, []);

  async function plan(id: string) {
    setBusy(id);
    try { const r = await api.post<{ message: string }>("/api/admin/plan", { instance: id }); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function decide(pid: string, answer: "accept" | "reject") {
    setBusy(pid);
    try { const r = await api.post<{ message: string }>("/api/admin/proposal", { pid, answer }); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Box><Title order={1} size="h2">Rosters</Title><Text c="dimmed" size="sm" mt={4}>Runs for the current lockout, per raid: tentative proposals to accept, then accepted runs with their sheets. Times are {data.tz}.</Text></Box>
      {data.raids.map((r) => <RaidCard key={r.id} r={r} meta={meta} busy={busy} onPlan={() => plan(r.id)} onDecide={decide} />)}
      {data.orphans.length > 0 && (
        <Card>
          <Box p="md"><Title order={2} size="h5">Sheets not tied to a raid</Title><Text size="xs" c="dimmed">These rosters have no instance — set one on Admin so they inherit a raid's rules.</Text></Box>
          <Stack gap={4} px="md" pb="md">{data.orphans.map((e) => <Text key={e.key} size="sm">{e.key} · {e.name} · {e.state} · starts {e.when} · in {e.by.in.length} · out {e.by.out.length}</Text>)}</Stack>
        </Card>
      )}
    </Stack>
  );
}

function RaidCard({ r, meta, busy, onPlan, onDecide }: { r: RaidRuns; meta: Meta; busy: string | null; onPlan: () => void; onDecide: (pid: string, a: "accept" | "reject") => void }) {
  const metaLine = [r.opened ? `window ${r.window[0]} → ${r.window[1]}` : r.first_open ? `opens ${r.first_open}` : "no opening date", `${r.current.length} run${r.current.length === 1 ? "" : "s"}`, r.open.length ? `${r.open.length} tentative` : null].filter(Boolean).join(" · ");
  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={<Button variant="default" size="sm" leftSection={<IconSparkles size={15} />} loading={busy === r.id} onClick={onPlan}>Plan the next {r.lockout_days} days</Button>} />
      <Tabs defaultValue={r.open.length ? "tentative" : "runs"} px="md" pb="md">
        <Tabs.List>
          <Tabs.Tab value="tentative" rightSection={r.open.length ? <Badge size="xs" color="yellow" variant="light">{r.open.length}</Badge> : null}>Tentative</Tabs.Tab>
          <Tabs.Tab value="runs" rightSection={r.current.length ? <Badge size="xs" color="teal" variant="light">{r.current.length}</Badge> : null}>Runs</Tabs.Tab>
          <Tabs.Tab value="history">History</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="tentative" pt="md">
          {r.open.length === 0 && <Text size="sm" c="dimmed">Nothing proposed. Press <b>Plan</b>, or turn on auto-propose on Raids.</Text>}
          <Stack gap="md">{r.open.map((p) => <ProposalCard key={p.id} p={p} meta={meta} busy={busy === p.id} onDecide={(a) => onDecide(p.id, a)} />)}</Stack>
        </Tabs.Panel>
        <Tabs.Panel value="runs" pt="md">
          {r.current.length === 0 && <Text size="sm" c="dimmed">No accepted runs in this window.</Text>}
          <Stack gap="md">{r.current.map((e) => <SheetCard key={e.key} e={e} meta={meta} />)}</Stack>
          {r.standing.length > 0 && <Text size="xs" c="dimmed" mt="sm">Standing rosters of this raid: {r.standing.map((t) => `${t.key}${t.schedule ? ` (${t.schedule})` : ""}`).join(", ")} — managed on Admin.</Text>}
        </Tabs.Panel>
        <Tabs.Panel value="history" pt="md">
          {r.history.length === 0 && r.past.length === 0 && <Text size="sm" c="dimmed">Nothing decided yet.</Text>}
          <Stack gap={4}>
            {r.history.map((p) => <Text key={p.id} size="sm"><Badge size="xs" variant="outline" color={p.state === "accepted" ? "teal" : "gray"} mr={6}>{p.state}</Badge>{p.id} · {p.runs.length} run(s){p.decided_by ? ` · by ${p.decided_by}` : ""}</Text>)}
            {r.past.map((e) => <Text key={e.key} size="sm"><Badge size="xs" variant="outline" color="gray" mr={6}>{e.state}</Badge>{e.key} · {e.when} · in {e.by.in.length} · out {e.by.out.length}</Text>)}
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Card>
  );
}

function ProposalCard({ p, meta, busy, onDecide }: { p: Proposal; meta: Meta; busy: boolean; onDecide: (a: "accept" | "reject") => void }) {
  return (
    <Box p="md" style={{ border: `1px solid ${p.viable ? "var(--mantine-color-teal-4)" : "var(--mantine-color-yellow-5)"}`, borderRadius: 8 }}>
      <Group justify="space-between" wrap="wrap">
        <Text size="sm"><b>{p.viable ? "Tentative" : "Best effort — not viable yet"}</b> · window {p.window[0]} → {p.window[1]}{p.notes.length ? ` · ${p.notes.join(" · ")}` : ""}</Text>
        <Group gap="xs">
          <Button size="xs" loading={busy} onClick={() => onDecide("accept")}>{p.viable ? "Accept — open the sheets" : "Open the sheets anyway"}</Button>
          <Button size="xs" variant="default" color="red" disabled={busy} onClick={() => onDecide("reject")}>Reject</Button>
        </Group>
      </Group>
      {p.problems.length > 0 && <Stack gap={2} mt={6}>{p.problems.map((x) => <Text key={x} size="xs" c="yellow">⚠ {x}</Text>)}</Stack>}
      <Stack gap="sm" mt="sm">
        {p.runs.map((run) => (
          <Box key={run.key}>
            <Text size="sm" fw={600}>{run.name} <Text span size="xs" c="dimmed">{run.seats}/{run.size} · {run.when}</Text></Text>
            <GroupsBlock meta={meta} sm={run.summary} />
          </Box>
        ))}
      </Stack>
      {p.unplaced.length > 0 && <Text size="xs" c="dimmed" mt="sm">Not seated: {p.unplaced.map(([n, why]) => `${n} — ${why}`).join(" · ")}</Text>}
    </Box>
  );
}

function SheetCard({ e, meta }: { e: Sheet; meta: Meta }) {
  const n = e.needs;
  return (
    <Box p="md" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
      <Group gap="xs" wrap="wrap">
        <Text size="sm" fw={700}>{e.name}</Text>
        <Text size="sm" c="dimmed">starts {e.when} · {e.state}</Text>
        <Badge variant="light" color="teal">{e.by.in.length} in</Badge>
        <Text size="xs" c="dimmed">{e.by.tentative.length} tentative · {e.by.sub.length} sub · {e.by.out.length} out</Text>
        {n && (n.headcount ? <Badge variant="light" color="red">short {n.headcount}</Badge> : <Badge variant="light" color="teal">full</Badge>)}
        {n && Object.entries(n.roles).map(([role, k]) => <Badge key={role} variant="light" color="red">{k} {role} short</Badge>)}
        {e.double_booked > 0 && <Badge variant="light" color="yellow">double-booked {e.double_booked}</Badge>}
        {n && <Text size="xs" c="dimmed">fill {e.fill_state}</Text>}
      </Group>
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md" mt="sm">
        {(["in", "tentative", "sub", "out"] as const).map((st) => (
          <Box key={st} style={{ minWidth: 0 }}>
            <Text size="xs" fw={700} tt="uppercase" c="dimmed" style={{ letterSpacing: ".08em" }} mb={4}>{st} ({e.by[st].length})</Text>
            {e.by[st].length === 0 && <Text size="sm" c="dimmed">—</Text>}
            {e.by[st].map((s) => (
              <Group key={s.uid} gap={6} wrap="nowrap" style={{ lineHeight: 1.7 }}>
                <GameIcon meta={meta} kind="role" id={s.role} size={18} title={s.role} />
                <GameIcon meta={meta} kind="spec" id={`${s.cls}:${s.spec}`} size={18} title={`${s.cls} ${s.spec}`} />
                <Text size="sm" c={CLASS_COLOURS[s.cls]} truncate>{s.character}</Text>
                <Text size="xs" c="dimmed" truncate>{s.display_name}{s.note ? ` — ${s.note}` : ""}</Text>
              </Group>
            ))}
          </Box>
        ))}
      </SimpleGrid>
      <GroupsBlock meta={meta} sm={e.summary} />
      <Accordion variant="contained" mt="sm" chevronPosition="left">
        <Accordion.Item value="detail">
          <Accordion.Control><Text size="xs" c="dimmed">fill asks ({e.fill_asks.length}) · callouts ({e.callouts.length}) · log ({e.log.length})</Text></Accordion.Control>
          <Accordion.Panel>
            {e.fill_asks.length > 0 && <Stack gap={2} mb="sm">{e.fill_asks.map((a, i) => <Text key={i} size="xs">{a.display_name} · {a.kind} · {a.character} ({a.spec}, {a.role}) · {a.reason} · <Text span c={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "yellow"}>{a.answer || "waiting"}</Text></Text>)}</Stack>}
            {e.callouts.length > 0 && <Text size="xs" c="dimmed" mb="sm">{e.callouts.map((c) => `${c.display_name} ${c.hours_before}h before${c.late ? " (late)" : ""}`).join(" · ")}</Text>}
            <Box component="pre" style={{ fontSize: 12, whiteSpace: "pre-wrap", margin: 0, fontFamily: "var(--mantine-font-family-monospace)", color: "var(--mantine-color-slate-2)" }}>{e.log.join("\n")}</Box>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Box>
  );
}
