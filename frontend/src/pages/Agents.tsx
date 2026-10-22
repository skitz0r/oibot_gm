import { useEffect, useState } from "react";
import { Alert, Anchor, Badge, Box, Button, Card, Code, Collapse, Group, SimpleGrid, Stack, Table, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconChevronDown, IconChevronRight, IconPlayerPlay } from "@tabler/icons-react";
import { api, type AgentJob, type AgentRun, type AgentStep, type Agents, type NewsItem, type Proposal } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

const LIGHT: Record<string, string> = { green: "var(--mantine-color-teal-4)", amber: "#E0A526", red: "#D9534F" };
const STATE_COLOUR: Record<string, string> = { proposed: "teal", approved: "blue", dismissed: "gray", applying: "yellow", applied: "green", failed: "red", reverted: "red" };
const KIND: Record<string, string> = { guild_setting: "guild setting", profile: "game data", needs_developer: "needs a developer" };
const KICK_POLL_MS = 1000;
const KICK_WAIT_MS = 10000;

function Dot({ colour, label }: { colour: string; label: string }) {
  return <Tooltip label={label}><Box component="span" aria-label={label} style={{ display: "inline-block", width: 11, height: 11, borderRadius: "50%", background: colour, flex: "none" }} /></Tooltip>;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The monitor for the local jobs on the Mac mini: are they loaded, running, failing; what the current run is doing; the
 *  proposals officers decide (the consequence comes from the server — the page never writes its own promise). */
export function AgentsPage() {
  const [data, setData] = useState<Agents | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [newsItems, setNews] = useState<NewsItem[]>([]);
  const [open, setOpen] = useState<string | null>(null); // the run shown below the jobs (null = the live one, else the latest)
  const [run, setRun] = useState<AgentRun | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, confirmDialog] = useConfirm();
  const phone = useMediaQuery("(max-width: 48em)");
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

  /** Kick a job, then follow it: until it leaves idle (started) or finishes, at most 10 s — then the job's own words. */
  async function kick(job: "review" | "apply") {
    setBusy(job);
    try {
      const r = await api.post<{ message: string; before?: { started: string | null; finished: string | null } }>("/api/agents/run", { job });
      setOpen(null);
      const start = Date.now();
      let said = false;
      while (Date.now() - start < KICK_WAIT_MS) {
        await sleep(KICK_POLL_MS);
        const d = await api.get<Agents>("/api/agents");
        setData(d);
        const j = d.jobs.find((x) => x.job === job);
        if (!j) break;
        if (j.state === "running") { ok(`${j.name} is running${j.step ? `: ${j.step}` : ""}`); said = true; break; }
        if ((j.finished ?? null) !== (r.before?.finished ?? null)) {  // a quick run (a guild setting) started and finished between two polls
          if (j.state === "failed") fail(new Error(j.result || `${j.name} failed`)); else ok(j.result || `${j.name} finished`);
          said = true;
          break;
        }
      }
      if (!said) ok(`${r.message} — it hasn't started within ${KICK_WAIT_MS / 1000} s; this page follows it when it does.`);
      load();
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  async function decide(p: Proposal, state: Verdict) {
    if (state === "interrupted") {  // the owner's recovery press: the apply run that held it is gone
      const yes = await ask({ title: `Mark as interrupted: ${p.title}`, message: (p.consequence?.interrupted || []).join("\n"), confirmLabel: "Mark as interrupted", color: "red" });
      if (!yes) return;
      try { ok((await api.post<{ message: string }>(`/api/proposals/${p.id}/interrupted`, {})).message); load(); } catch (e) { fail(e); load(); }
      return;
    }
    const verb = state === "approved" ? "Approve" : p.state === "approved" ? "Withdraw approval" : "Dismiss";
    const res = await ask({
      title: `${verb}: ${p.title}`,
      message: (p.consequence?.[state] || []).join("\n") || (state === "approved" ? "The apply job picks it up at its next run." : "Nothing changes."),
      confirmLabel: verb, color: state === "approved" ? "teal" : p.state === "approved" ? "red" : "gray",
      note: { label: "Note (optional)", maxLength: 300, placeholder: "kept on the proposal and its card" },
    });
    if (!res) return;
    const note = typeof res === "object" && res.note ? res.note : undefined;
    try { ok((await api.post<{ message: string }>(`/api/proposals/${p.id}/resolve`, { state, note })).message); load(); } catch (e) { fail(e); load(); }
  }
  const botUp = !!data.bot.pid;
  const apply = data.jobs.find((j) => j.job === "apply");
  const recover = data.owner && apply?.state !== "running";  // Mark as interrupted: the owner, while the apply job isn't running (and the server offers it: never for a commit whose restart waits)

  return (
    <Stack gap="lg">
      {confirmDialog}
      <PageTitle title="Agents" intro="The jobs on the Mac mini that read the game news and apply approved corrections. The page follows a running job step by step."
        right={data.owner && <Group gap="xs">
          {(data.approved > 0 || data.restart_pending) && <Button variant="default" onClick={() => kick("apply")} loading={busy === "apply"} disabled={apply?.state === "running"}>Apply now{data.approved > 0 ? ` (${data.approved})` : ""}</Button>}
          <Button leftSection={<IconPlayerPlay size={16} />} onClick={() => kick("review")} loading={busy === "review"} disabled={data.jobs.some((j) => j.job === "review" && j.state === "running")}>Run review now</Button>
        </Group>} />

      {data.restart_pending && <Alert color="yellow" variant="light" title={`Restart owed: ${data.restart_pending.title || data.restart_pending.id}`}>{data.restart_pending.text}</Alert>}
      {data.ops_channel === false && <Alert color="red" variant="light" title="No ops channel">Proposal cards are not posted in Discord — set the ops channel on the Config page. Officers can still decide here.</Alert>}
      {data.busy?.raiding && <Alert color="yellow" variant="light">Raid time: {data.busy.why} — until {data.busy.until_label}. A game-data change waits; guild settings still apply.</Alert>}

      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
        <Card><Box p="md">
          <Group gap="xs"><Dot colour={botUp ? LIGHT.green : LIGHT.red} label={botUp ? "running" : "not running"} /><Eyebrow>Bot</Eyebrow></Group>
          <Text fw={600} mt={4}>{botUp ? "Running" : data.bot.loaded ? "Not running" : "Not loaded in launchd"}</Text>
          <Text size="xs" c="dimmed">{botUp ? `process ${data.bot.pid}` : data.bot.last_exit ? `last exit ${data.bot.last_exit}` : ""}</Text>
          <Text size="xs" c="dimmed" mt={6}>{data.waiting} waiting for an officer · {data.approved} approved, not applied yet</Text>
        </Box></Card>
        {data.jobs.map((j) => <JobCard key={j.job} j={j} mode={j.job === "apply" ? data.apply_mode?.mode : undefined} onOpen={() => setOpen(j.run_id)} />)}
      </SimpleGrid>

      {data.owner && (data.mcp || data.llm) && (
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
          {data.mcp && <Card><Box p="md">
            <Group gap="xs"><Dot colour={data.mcp.configured ? LIGHT.green : LIGHT.amber} label={data.mcp.configured ? "configured" : "not configured"} /><Eyebrow>MCP server</Eyebrow></Group>
            <Text fw={600} mt={4}>{data.mcp.configured ? "Configured" : "No token in the bot's .env"}</Text>
            <Text size="xs" c="dimmed">{data.mcp.local_only ? "local only: the token works from this Mac, never through the tunnel" : "⚠ the token also works through the tunnel (OIBOT_MCP_LOCAL_ONLY is off)"}</Text>
            <Text size="xs" c="dimmed">{data.mcp.last_used_label ? `last write ${data.mcp.last_used_label}` : "no write through MCP in the recent log"}{data.mcp.config_file ? " · .mcp.json registers it for Claude Code" : ""}</Text>
          </Box></Card>}
          {data.llm && <Card><Box p="md">
            <Eyebrow>Claude spend this month</Eyebrow>
            <Text fw={600} mt={4}>${data.llm.spent.toFixed(2)} of ${data.llm.cap.toFixed(2)} <Text span size="xs" c="dimmed">one cap shared by every server</Text></Text>
            {data.llm.by_guild.map((g) => <Text key={g.key} size="xs" c="dimmed">{g.name}: ${g.spent.toFixed(2)}</Text>)}
            {data.llm.unattributed > 0 && <Text size="xs" c="dimmed">not tied to a server: ${data.llm.unattributed.toFixed(2)}</Text>}
          </Box></Card>}
        </SimpleGrid>
      )}

      <Card>
        <CardHeader title={run?.live ? "Live run" : "Run"} hint={shown ? `${shown}${run ? ` · ${run.outcome}` : ""}` : "no runs yet"}
          action={open && <Button size="xs" variant="subtle" onClick={() => setOpen(null)}>Back to the latest</Button>} />
        <Box p="md">
          {!run || run.steps.length === 0 ? <Text size="sm" c="dimmed">{shown ? "Nothing recorded in this run yet." : "The jobs haven't run on this Mac yet."}</Text>
            : <Stack gap={4}>{run.steps.map((s, i) => <StepRow key={i} s={s} />)}</Stack>}
        </Box>
      </Card>

      <Card>
        <CardHeader title={`Proposals (${proposals.length})`} hint="officers decide here or on the card in the ops channel; a game-data change is the owner's" />
        {proposals.length === 0 && <Text size="sm" c="dimmed" p="md">No proposals yet: the review proposes only when the news contradicts what the bot believes.</Text>}
        {phone ? (
          <Stack gap="sm" p="sm">{proposals.map((p) => <ProposalCard key={p.id} p={p} owner={data.owner} recover={recover} onDecide={decide} />)}</Stack>
        ) : proposals.length > 0 && (
          <Table.ScrollContainer minWidth={720}>
            <Table>
              <Table.Thead><Table.Tr><Table.Th>Proposed</Table.Th><Table.Th>Change</Table.Th><Table.Th>Kind</Table.Th><Table.Th>State</Table.Th><Table.Th /></Table.Tr></Table.Thead>
              <Table.Tbody>
                {proposals.map((p) => (
                  <Table.Tr key={p.id}>
                    <Table.Td w={150}><Text size="xs" c="dimmed">{p.created_label}</Text></Table.Td>
                    <Table.Td><ProposalBody p={p} /></Table.Td>
                    <Table.Td w={130}><Text size="xs">{KIND[p.kind] || p.kind}</Text><Text size="xs" c="dimmed">{p.confidence} confidence</Text></Table.Td>
                    <Table.Td w={200}><ProposalState p={p} /></Table.Td>
                    <Table.Td w={190}><Decide p={p} owner={data.owner} recover={recover} onDecide={decide} /></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
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

function ProposalBody({ p }: { p: Proposal }) {
  return (
    <>
      <Text size="sm" fw={600}>{p.title}</Text>
      <Text size="xs" c="dimmed">{p.change}</Text>
      {p.evidence && <Text size="xs" c="dimmed" fs="italic" mt={2}>“{p.evidence}”</Text>}
      {p.kind === "profile" && (p.shared_with?.length ?? 0) > 0 && <Text size="xs" c="yellow" mt={2}>Game data shared with: {p.shared_with!.join(", ")}</Text>}
      <Group gap={6} mt={2}>{p.news.map((u, i) => <Anchor key={u} href={u} target="_blank" rel="noreferrer" size="xs">source {i + 1}</Anchor>)}</Group>
    </>
  );
}

function ProposalState({ p }: { p: Proposal }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Badge size="sm" variant="light" color={STATE_COLOUR[p.state] || "gray"}>{p.state}</Badge>
      {p.decided_by && <Text size="xs" c="dimmed">{p.state === "dismissed" ? "dismissed" : "approved"} by {p.decided_by} · {p.decided_label}</Text>}
      {p.status_note && !["dismissed", "applied", "reverted"].includes(p.state) && <Text size="xs" c="yellow">⏳ {p.status_note}</Text>}
      {p.commit && <Text size="xs" c="dimmed" ff="monospace">{p.commit.slice(0, 10)}</Text>}
      {p.note && <Text size="xs" c="dimmed">{p.note}</Text>}
      {p.history.length > 1 && <Button size="compact-xs" variant="subtle" px={0} onClick={() => setOpen((o) => !o)}>{open ? "Hide history" : `History (${p.history.length})`}</Button>}
      <Collapse expanded={open}>
        <Stack gap={2} mt={4}>
          {p.history.map((h, i) => (
            <Text key={i} size="xs" c="dimmed"><Text span size="xs" c="dimmed">{h.at_label}</Text> · {h.annotation || h.state === null ? "note" : h.state} · {h.by}{h.note ? ` — ${h.note}` : ""}</Text>
          ))}
        </Stack>
      </Collapse>
    </>
  );
}

type Verdict = "approved" | "dismissed" | "interrupted";
type DecideProps = { p: Proposal; owner: boolean; recover: boolean; onDecide: (p: Proposal, s: Verdict) => void };

function Decide({ p, owner, recover, onDecide }: DecideProps) {
  const canApprove = (p.state === "proposed" || p.state === "failed") && p.kind !== "needs_developer";
  const ownerOnly = p.kind === "profile" && !owner;
  return (
    <Group gap={6} wrap="wrap">
      {canApprove && (ownerOnly
        ? <Tooltip label="A game-data change restarts the bot for every server: the owner approves it"><Button size="xs" disabled>Approve</Button></Tooltip>
        : <Button size="xs" onClick={() => onDecide(p, "approved")}>Approve</Button>)}
      {["proposed", "approved", "failed"].includes(p.state) && <Button size="xs" variant="default" color={p.state === "approved" ? "red" : undefined} onClick={() => onDecide(p, "dismissed")}>{p.state === "approved" ? "Withdraw approval" : "Dismiss"}</Button>}
      {p.kind === "needs_developer" && p.state === "proposed" && <Text size="xs" c="dimmed">needs a developer: dismiss it once handled</Text>}
      {recover && p.state === "applying" && p.consequence?.interrupted && <Button size="xs" variant="default" color="red" onClick={() => onDecide(p, "interrupted")}>Mark as interrupted</Button>}
    </Group>
  );
}

/** A proposal as one stacked card (phones: the table's buttons would sit off screen). */
function ProposalCard({ p, owner, recover, onDecide }: DecideProps) {
  return (
    <Card withBorder p="sm">
      <Group justify="space-between" align="flex-start" wrap="nowrap" mb={4}>
        <Text size="xs" c="dimmed">{p.created_label} · {KIND[p.kind] || p.kind} · {p.confidence} confidence</Text>
      </Group>
      <ProposalBody p={p} />
      <Box mt="xs"><ProposalState p={p} /></Box>
      <Box mt="xs"><Decide p={p} owner={owner} recover={recover} onDecide={onDecide} /></Box>
    </Card>
  );
}

function JobCard({ j, mode, onOpen }: { j: AgentJob; mode?: "main" | "pr" | "unknown"; onOpen: () => void }) {
  const stateText = j.state === "running" ? (j.elsewhere ? "Busy with another server's proposal" : "Running") : j.state === "failed" ? "Last run failed" : "Idle";
  return (
    <Card><Box p="md">
      <Group gap="xs"><Dot colour={LIGHT[j.light]} label={j.launchd.loaded ? stateText : "not installed"} /><Eyebrow>{j.name}</Eyebrow>
        {mode && mode !== "unknown" && <Badge size="xs" variant="outline" color={mode === "pr" ? "grape" : "teal"}>{mode === "pr" ? "opens a pull request" : "applies to main"}</Badge>}</Group>
      <Text fw={600} mt={4}>{stateText}{!j.launchd.loaded && <Text span size="xs" c="yellow" ml={8}>not installed</Text>}</Text>
      {j.state === "running" && j.step && !j.elsewhere && <Text size="sm">{j.step}</Text>}
      <Text size="xs" c="dimmed" mt={4}>{j.schedule}{j.next_label ? ` · next ${j.next_label}` : ""}</Text>
      <Text size="xs" c="dimmed">{j.started_label ? `last ${j.finished_label || j.started_label}` : "never run"}{j.result ? ` · ${j.result}` : ""}</Text>
      {j.install && <Text size="xs" c="dimmed" mt={4}>Install on the Mac: <Code>{j.install}</Code></Text>}
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
