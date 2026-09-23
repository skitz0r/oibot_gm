import { useEffect, useState } from "react";
import { Anchor, Badge, Box, Button, Card, Collapse, Group, SimpleGrid, Stack, Table, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight, IconPlayerPlay } from "@tabler/icons-react";
import { api, type AgentJob, type AgentRun, type AgentStep, type Agents, type NewsItem, type Proposal } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

const LIGHT: Record<string, string> = { green: "var(--mantine-color-teal-4)", amber: "#E0A526", red: "#D9534F" };
const STATE_COLOUR: Record<string, string> = { proposed: "teal", approved: "blue", dismissed: "gray", applying: "yellow", applied: "green", failed: "red", reverted: "red" };
const KIND: Record<string, string> = { guild_setting: "guild setting", profile: "game data", needs_developer: "needs a developer" };

function Dot({ colour, label }: { colour: string; label: string }) {
  return <Tooltip label={label}><Box component="span" aria-label={label} style={{ display: "inline-block", width: 11, height: 11, borderRadius: "50%", background: colour, flex: "none" }} /></Tooltip>;
}

/** The monitor for the local jobs on the Mac mini: are they loaded, running, failing; what the current run is doing. */
export function AgentsPage() {
  const [data, setData] = useState<Agents | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [newsItems, setNews] = useState<NewsItem[]>([]);
  const [open, setOpen] = useState<string | null>(null); // the run shown below the jobs (null = the live one, else the latest)
  const [run, setRun] = useState<AgentRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const running = data?.jobs.find((j) => j.state === "running" && j.run_id);
  const shown = open || running?.run_id || data?.runs[0]?.id || null;

  const load = () => {
    api.get<Agents>("/api/agents").then(setData).catch(fail);
    api.get<{ proposals: Proposal[] }>("/api/proposals").then((d) => setProposals(d.proposals)).catch(fail);
    api.get<{ items: NewsItem[] }>("/api/news").then((d) => setNews([...d.items].reverse())).catch(fail);
  };
  const loadRun = () => { if (shown) api.get<AgentRun>(`/api/agents/run/${shown}`).then(setRun).catch(() => setRun(null)); else setRun(null); };
  useEffect(() => { load(); }, []);
  useEffect(() => { loadRun(); }, [shown]);
  // a running job's transcript grows line by line: poll fast while one runs, slowly otherwise
  usePoll(() => { load(); loadRun(); }, running ? 3000 : 30000);
  useLive(load, ["agents", "ops"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;

  async function runReview() {
    setBusy(true);
    try { ok((await api.post<{ message: string }>("/api/agents/run", { job: "review" })).message); setOpen(null); load(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function decide(p: Proposal, state: "approved" | "dismissed") {
    const yes = await ask({
      title: state === "approved" ? "Approve this change?" : "Dismiss this proposal?",
      message: state === "approved" ? `${p.change} — the apply job makes the change within 15 minutes${p.kind === "profile" ? " (a commit and a bot restart)" : ""}.` : "Nothing changes; the proposal is kept as dismissed.",
      confirmLabel: state === "approved" ? "Approve" : "Dismiss", color: state === "approved" ? "teal" : "gray",
    });
    if (!yes) return;
    try { ok((await api.post<{ message: string }>(`/api/proposals/${p.id}/resolve`, { state })).message); load(); } catch (e) { fail(e); }
  }
  const botUp = !!data.bot.pid;

  return (
    <Stack gap="lg">
      {confirmDialog}
      <PageTitle title="Agents" intro="The jobs on the Mac mini that read the game news and apply approved corrections. The page follows a running job step by step."
        right={data.owner && <Button leftSection={<IconPlayerPlay size={16} />} onClick={runReview} loading={busy} disabled={data.jobs.some((j) => j.job === "review" && j.state === "running")}>Run review now</Button>} />

      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
        <Card><Box p="md">
          <Group gap="xs"><Dot colour={botUp ? LIGHT.green : LIGHT.red} label={botUp ? "running" : "not running"} /><Eyebrow>Bot</Eyebrow></Group>
          <Text fw={600} mt={4}>{botUp ? "Running" : data.bot.loaded ? "Not running" : "Not loaded in launchd"}</Text>
          <Text size="xs" c="dimmed">{botUp ? `process ${data.bot.pid}` : data.bot.last_exit ? `last exit ${data.bot.last_exit}` : ""}</Text>
          <Text size="xs" c="dimmed" mt={6}>{data.waiting} waiting for an officer · {data.approved} approved, not applied yet</Text>
        </Box></Card>
        {data.jobs.map((j) => <JobCard key={j.job} j={j} onOpen={() => setOpen(j.run_id)} />)}
      </SimpleGrid>

      <Card>
        <CardHeader title={run?.live ? "Live run" : "Run"} hint={shown ? `${shown}${run ? ` · ${run.outcome}` : ""}` : "no runs yet"}
          action={open && <Button size="xs" variant="subtle" onClick={() => setOpen(null)}>Back to the latest</Button>} />
        <Box p="md">
          {!run || run.steps.length === 0 ? <Text size="sm" c="dimmed">{shown ? "Nothing recorded in this run yet." : "The jobs haven't run on this Mac yet."}</Text>
            : <Stack gap={4}>{run.steps.map((s, i) => <StepRow key={i} s={s} />)}</Stack>}
        </Box>
      </Card>

      <Card>
        <CardHeader title={`Proposals (${proposals.length})`} hint="officers approve here or on the card in the ops channel" />
        <Table.ScrollContainer minWidth={720}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Proposed</Table.Th><Table.Th>Change</Table.Th><Table.Th>Kind</Table.Th><Table.Th>State</Table.Th><Table.Th /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {proposals.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">No proposals yet: the review proposes only when the news contradicts what the bot believes.</Text></Table.Td></Table.Tr>}
              {proposals.map((p) => (
                <Table.Tr key={p.id}>
                  <Table.Td w={150}><Text size="xs" c="dimmed">{p.created_label}</Text></Table.Td>
                  <Table.Td>
                    <Text size="sm" fw={600}>{p.title}</Text>
                    <Text size="xs" c="dimmed">{p.change}</Text>
                    {p.evidence && <Text size="xs" c="dimmed" fs="italic" mt={2}>“{p.evidence}”</Text>}
                    <Group gap={6} mt={2}>{p.news.map((u, i) => <Anchor key={u} href={u} target="_blank" rel="noreferrer" size="xs">source {i + 1}</Anchor>)}</Group>
                  </Table.Td>
                  <Table.Td w={130}><Text size="xs">{KIND[p.kind] || p.kind}</Text><Text size="xs" c="dimmed">{p.confidence} confidence</Text></Table.Td>
                  <Table.Td w={190}>
                    <Badge size="sm" variant="light" color={STATE_COLOUR[p.state] || "gray"}>{p.state}</Badge>
                    {p.decided_by && <Text size="xs" c="dimmed">{p.state === "dismissed" ? "dismissed" : "approved"} by {p.decided_by} · {p.decided_label}</Text>}
                    {p.commit && <Text size="xs" c="dimmed" ff="monospace">{p.commit.slice(0, 10)}</Text>}
                    {p.note && <Text size="xs" c="dimmed">{p.note}</Text>}
                  </Table.Td>
                  <Table.Td w={170}>
                    <Group gap={6} wrap="nowrap">
                      {(p.state === "proposed" || p.state === "failed") && p.kind !== "needs_developer" && <Button size="xs" onClick={() => decide(p, "approved")}>Approve</Button>}
                      {["proposed", "approved", "failed"].includes(p.state) && <Button size="xs" variant="default" onClick={() => decide(p, "dismissed")}>Dismiss</Button>}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>

      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
        <Card>
          <CardHeader title="Past runs" hint={`the last ${data.runs.length}`} />
          <Table.ScrollContainer minWidth={420}>
            <Table highlightOnHover>
              <Table.Tbody>
                {data.runs.length === 0 && <Table.Tr><Table.Td><Text size="sm" c="dimmed">none yet</Text></Table.Td></Table.Tr>}
                {data.runs.map((r) => (
                  <Table.Tr key={r.id} onClick={() => setOpen(r.id)} style={{ cursor: "pointer", background: r.id === shown ? "var(--mantine-color-slate-6)" : undefined }}>
                    <Table.Td w={150}><Text size="xs" c="dimmed">{r.started_label}</Text></Table.Td>
                    <Table.Td w={90}><Text size="xs">{r.job === "review" ? "review" : "apply"}</Text></Table.Td>
                    <Table.Td><Text size="sm">{r.outcome}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Card>
        <Card>
          <CardHeader title={`News kept (${newsItems.length})`} hint="from the news channel, filtered by our game version and raids" />
          <Table.ScrollContainer minWidth={420}>
            <Table>
              <Table.Tbody>
                {newsItems.length === 0 && <Table.Tr><Table.Td><Text size="sm" c="dimmed">Nothing yet. Set the news channel on the Config page.</Text></Table.Td></Table.Tr>}
                {newsItems.slice(0, 60).map((n) => (
                  <Table.Tr key={n.url || n.title}>
                    <Table.Td w={150}><Text size="xs" c="dimmed">{n.posted_label}</Text></Table.Td>
                    <Table.Td>
                      {n.url ? <Anchor href={n.url} target="_blank" rel="noreferrer" size="sm">{n.title}</Anchor> : <Text size="sm">{n.title}</Text>}
                      <Group gap={4} mt={2}>{n.matched.map((k) => <Badge key={k} size="xs" variant="outline" color="gray">{k}</Badge>)}</Group>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Card>
      </SimpleGrid>
    </Stack>
  );
}

function JobCard({ j, onOpen }: { j: AgentJob; onOpen: () => void }) {
  const stateText = j.state === "running" ? "Running" : j.state === "failed" ? "Last run failed" : "Idle";
  return (
    <Card><Box p="md">
      <Group gap="xs"><Dot colour={LIGHT[j.light]} label={j.launchd.loaded ? stateText : "not installed"} /><Eyebrow>{j.name}</Eyebrow></Group>
      <Text fw={600} mt={4}>{stateText}{!j.launchd.loaded && <Text span size="xs" c="yellow" ml={8}>not installed</Text>}</Text>
      {j.state === "running" && j.step && <Text size="sm">{j.step}</Text>}
      <Text size="xs" c="dimmed" mt={4}>{j.schedule}{j.next_label ? ` · next ${j.next_label}` : ""}</Text>
      <Text size="xs" c="dimmed">{j.started_label ? `last ${j.finished_label || j.started_label}` : "never run"}{j.result ? ` · ${j.result}` : ""}</Text>
      {j.run_id && <Button size="compact-xs" variant="subtle" mt={6} px={0} onClick={onOpen}>{j.state === "running" ? "Follow this run" : "Open the last run"}</Button>}
    </Box></Card>
  );
}

function StepRow({ s }: { s: AgentStep }) {
  const [open, setOpen] = useState(false);
  const more = s.detail || s.result;
  const colour = s.ok === false ? "red" : s.kind === "runner" ? "dimmed" : s.kind === "text" ? undefined : s.kind === "result" ? "teal" : undefined;
  const mark = s.kind === "tool" ? (s.ok === null ? "…" : s.ok ? "✓" : "✕") : s.kind === "runner" ? "›" : s.kind === "result" ? "■" : s.kind === "start" ? "▶" : "·";
  return (
    <Box>
      <UnstyledButton onClick={() => more && setOpen((o) => !o)} style={{ display: "flex", gap: 8, alignItems: "flex-start", width: "100%", cursor: more ? "pointer" : "default" }}>
        <Text span size="sm" ff="monospace" c={colour} w={14} ta="center">{mark}</Text>
        <Text span size="sm" c={colour} fs={s.kind === "text" ? "italic" : undefined} style={{ flex: 1, minWidth: 0, wordBreak: "break-word" }}>{s.title}</Text>
        {more && (open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />)}
      </UnstyledButton>
      {more && (
        <Collapse expanded={open}>
          <Box ml={22} mt={4} p="xs" style={{ background: "var(--mantine-color-slate-7)", borderRadius: 6 }}>
            {s.detail && <Text size="xs" ff="monospace" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{s.detail}</Text>}
            {s.result && <Text size="xs" ff="monospace" c="dimmed" mt={s.detail ? 6 : 0} style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{s.result}</Text>}
          </Box>
        </Collapse>
      )}
    </Box>
  );
}
