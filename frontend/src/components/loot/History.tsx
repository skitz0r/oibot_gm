import { useEffect, useState } from "react";
import { Badge, Button, Card, Group, Modal, Select, Stack, Table, Text } from "@mantine/core";
import { api, type LootHistoryRow, type Precedent } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { useLive } from "../../hooks/useLive";
import { ReasonModal, SourceBadge } from "./shared";
import { DistilModal } from "./Distil";

interface Council { id: string; run: string; label: string | null; date_label: string; test: boolean; summary: string;
  drops: { drop_id: string; item: string; boss: string; tiered: boolean }[];
  awards: { drop_id: string; item: string; to: string; source: string | null; bot: string | null; by: string | null; at_label: string; void: boolean; tier: string; offspec: boolean }[];
  overrides: { item: string; bot: string; human: string; reason: string; by: string; at_label: string }[] }

/** Loot › History: every ended council, newest first; a row opens the council as it ended (drops, awards with who
 *  recorded them, overrides with their reasons, and the summary — what was never confirmed is inside it). */
export function History() {
  const [rows, setRows] = useState<LootHistoryRow[] | null>(null);
  const [one, setOne] = useState<Council | null>(null);
  const load = () => api.get<{ rows: LootHistoryRow[] }>("/api/loot/history").then((d) => setRows(d.rows)).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["loot:"]);
  if (!rows) return <Text c="dimmed">Loading…</Text>;
  return (
    <Card>
      <CardHeader title="Ended councils" hint="newest first" />
      <Table.ScrollContainer minWidth={620}>
        <Table highlightOnHover>
          <Table.Thead><Table.Tr><Table.Th>Run</Table.Th><Table.Th>Awards</Table.Th><Table.Th>Overrides</Table.Th><Table.Th>Left undecided</Table.Th><Table.Th>Ended</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {rows.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">No council has ended yet.</Text></Table.Td></Table.Tr>}
            {rows.map((r) => (
              <Table.Tr key={r.file} style={{ cursor: "pointer" }} onClick={() => api.get<Council>(`/api/loot/history/${r.file}`).then(setOne).catch(fail)}>
                <Table.Td><Text size="sm" fw={600}>{r.run}{r.label ? ` · ${r.label}` : ""}</Text><Text size="xs" c="dimmed">{r.date_label}</Text>{r.test && <Badge size="xs" color="violet" variant="light">rehearsal</Badge>}</Table.Td>
                <Table.Td>{r.awards}</Table.Td><Table.Td>{r.overrides}</Table.Td>
                <Table.Td><Text size="xs">{[r.never_confirmed && `${r.never_confirmed} never confirmed`, r.undistributed && `${r.undistributed} never distributed`, r.reasons_pending && `${r.reasons_pending} waiting for a reason`].filter(Boolean).join(" · ") || "—"}</Text></Table.Td>
                <Table.Td><Text size="xs" c="dimmed">{r.ended_label}</Text></Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      <Modal opened={!!one} onClose={() => setOne(null)} title={one ? `${one.run}${one.label ? ` · ${one.label}` : ""} · ${one.date_label}` : ""} size="lg">
        {one && (
          <Stack gap="sm">
            <Text size="sm" style={{ whiteSpace: "pre-line" }}>{one.summary}</Text>
            <Table verticalSpacing={2} fz="sm">
              <Table.Thead><Table.Tr><Table.Th>Item</Table.Th><Table.Th>To</Table.Th><Table.Th>Source</Table.Th><Table.Th>Recorded by</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {one.awards.map((a, i) => (
                  <Table.Tr key={i} style={{ opacity: a.void ? 0.45 : 1, textDecoration: a.void ? "line-through" : undefined }}>
                    <Table.Td>{a.item}</Table.Td><Table.Td>{a.to}{a.offspec ? " (OS)" : ""}</Table.Td><Table.Td><SourceBadge source={a.source} /></Table.Td><Table.Td><Text size="xs" c="dimmed">{a.by} · {a.at_label}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
            {one.overrides.length > 0 && <Text size="sm" fw={600}>Overrides</Text>}
            {one.overrides.map((o, i) => <Text key={i} size="sm">{o.item}: the bot picked {o.bot}, the council gave it to {o.human} — “{o.reason}” <Text span size="xs" c="dimmed">({o.by})</Text></Text>)}
          </Stack>
        )}
      </Modal>
    </Card>
  );
}

/** Loot › Precedents: council overrides with their reasons — what the bot cites next time. Retire one (the bot stops
 *  citing it; append-only, the reason stays on it) or restore it; "needs a reason" lists awards the companion recorded
 *  against the bot's pick that still have none (Loot › Tonight gives the reason). Distil… (phase 4, L29): Claude drafts up to
 *  five policy sentences from the active precedents; an officer picks which to append (DistilModal). */
export function Precedents() {
  const [data, setData] = useState<{ rows: Precedent[]; needs_reason: { import_id: string; item: string; recipient: string; bot: string | null; received_label: string }[] } | null>(null);
  const [status, setStatus] = useState<string | null>("active");
  const [target, setTarget] = useState<{ p: Precedent; restore: boolean } | null>(null);
  const [distil, setDistil] = useState(false);
  const load = () => api.get<typeof data & object>(`/api/loot/precedents${status ? `?status=${status}` : ""}`).then(setData).catch(fail);
  useEffect(() => { load(); }, [status]);
  useLive(load, ["loot:"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      {data.needs_reason.length > 0 && (
        <Card>
          <CardHeader title={`Needs a reason (${data.needs_reason.length})`} hint="recorded in game against the bot's pick — give the reason on Tonight" />
          <Stack p="md" gap={4}>{data.needs_reason.map((r) => <Text key={r.import_id} size="sm"><b>{r.item}</b> → {r.recipient} (the bot picked {r.bot || "?"}) · {r.received_label}</Text>)}</Stack>
        </Card>
      )}
      <Card>
        <CardHeader title="Precedents" hint="the bot cites active ones" action={
          <Group gap="xs">
            <Button size="xs" variant="default" onClick={() => setDistil(true)}>Distil…</Button>
            <Select size="xs" w={150} value={status} onChange={setStatus} clearable placeholder="all" data={["active", "retired", "superseded"]} />
          </Group>} />
        <Table.ScrollContainer minWidth={720}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Date</Table.Th><Table.Th>Item</Table.Th><Table.Th>Bot picked</Table.Th><Table.Th>Council awarded</Table.Th><Table.Th>Reason</Table.Th><Table.Th /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.rows.length === 0 && <Table.Tr><Table.Td colSpan={6}><Text size="sm" c="dimmed">{status ? `No ${status} precedents — clear the filter to see them all.` : "No precedents yet — an award the council makes against the bot's pick, with its reason, becomes one."}</Text></Table.Td></Table.Tr>}
              {data.rows.map((p) => (
                <Table.Tr key={p.ref} style={{ opacity: p.status === "active" ? 1 : 0.6 }}>
                  <Table.Td><Text size="xs" c="dimmed">{p.date_label}</Text></Table.Td><Table.Td>{p.item}</Table.Td><Table.Td>{p.bot}</Table.Td><Table.Td>{p.human}</Table.Td>
                  <Table.Td><Text size="sm">{p.reason}</Text>{p.status !== "active" && <Text size="xs" c="dimmed">{p.status}{p.status_by ? ` by ${p.status_by}` : ""}{p.status_reason ? ` — “${p.status_reason}”` : ""}</Text>}</Table.Td>
                  <Table.Td>
                    {p.status === "active" && <Button size="compact-xs" variant="subtle" color="red" onClick={() => setTarget({ p, restore: false })}>Retire</Button>}
                    {p.status === "retired" && <Button size="compact-xs" variant="subtle" onClick={() => setTarget({ p, restore: true })}>Restore</Button>}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      {distil && <DistilModal onClose={() => setDistil(false)} />}
      <ReasonModal opened={!!target} title={target ? `${target.restore ? "Restore" : "Retire"} the precedent on ${target.p.item}` : ""}
        message={target ? `${target.p.item}: the bot picked ${target.p.bot}, the council gave it to ${target.p.human} — “${target.p.reason}”. ${target.restore ? "The bot cites it again." : "The bot stops citing it; it stays in the record."}` : ""}
        label="Why (kept on the precedent)" onClose={() => setTarget(null)}
        onSubmit={async (reason) => {
          try { const r = await api.post<{ message: string }>("/api/loot/precedents/retire", { ref: target!.p.ref, reason, restore: target!.restore }); ok(r.message); load(); return true; } catch (e) { fail(e); return false; }
        }} />
    </Stack>
  );
}
