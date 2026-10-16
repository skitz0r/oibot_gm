import { useEffect, useState } from "react";
import { Card, Group, Stack, Switch, Table, Text, TextInput } from "@mantine/core";
import { api, type LogRow, type Meta } from "../api";
import { CardHeader, PageTitle, fail } from "../components/Page";
import { useLive } from "../hooks/useLive";
import { usePoll } from "../hooks/usePoll";
import { SourceBadge, Who } from "../components/loot/shared";

/** The guild's award log, for everyone logged in to this guild (D2; nothing of it is public). Officers can switch on
 *  the scores — tier, source, the bot's pick, who recorded it, the precedent's reason — which the server sends only to
 *  them (never hidden in the browser). */
export function LootLogPage({ meta }: { meta: Meta }) {
  const officer = meta.viewer.officer;
  const [scores, setScores] = useState(false);
  const [mine, setMine] = useState(false);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<LogRow[] | null>(null);
  const load = () => api.get<{ rows: LogRow[]; scores: boolean }>(`/api/loot/log?limit=500${scores ? "&scores=true" : ""}`).then((d) => setRows(d.rows)).catch(fail);
  useEffect(() => { load(); }, [scores]);
  usePoll(load);
  useLive(load, ["loot:"]);
  const shown = (rows || []).filter((r) => (!mine || r.mine) && (!q.trim() || `${r.item} ${r.character} ${r.boss}`.toLowerCase().includes(q.trim().toLowerCase())));
  return (
    <Stack gap="lg">
      <PageTitle title="Loot log" intro="Every award the council recorded, newest first." />
      <Card>
        <CardHeader title={`${shown.length} award${shown.length === 1 ? "" : "s"}`} action={
          <Group gap="md">
            <TextInput size="xs" placeholder="item, character or boss" value={q} onChange={(e) => setQ(e.currentTarget.value)} w={200} />
            <Switch size="xs" label="Mine" checked={mine} onChange={(e) => setMine(e.currentTarget.checked)} />
            {officer && <Switch size="xs" label="Scores" checked={scores} onChange={(e) => setScores(e.currentTarget.checked)} />}
          </Group>} />
        <Table.ScrollContainer minWidth={scores ? 860 : 560}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Date</Table.Th><Table.Th>Item</Table.Th><Table.Th>Character</Table.Th><Table.Th>Boss</Table.Th>
              {scores && <><Table.Th>Tier</Table.Th><Table.Th>Source</Table.Th><Table.Th>Bot picked</Table.Th><Table.Th>Recorded by</Table.Th><Table.Th>Reason</Table.Th></>}</Table.Tr></Table.Thead>
            <Table.Tbody>
              {rows === null && <Table.Tr><Table.Td colSpan={9}><Text size="sm" c="dimmed">Loading…</Text></Table.Td></Table.Tr>}
              {rows !== null && shown.length === 0 && <Table.Tr><Table.Td colSpan={9}><Text size="sm" c="dimmed">No awards yet.</Text></Table.Td></Table.Tr>}
              {shown.map((r, i) => (
                <Table.Tr key={i} style={{ background: r.mine ? "var(--mantine-color-slate-6)" : undefined }}>
                  <Table.Td><Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{r.date_label}</Text></Table.Td>
                  <Table.Td><Text size="sm">{r.item}</Text></Table.Td>
                  <Table.Td><Group gap={4} wrap="nowrap"><Who meta={meta} name={r.character} cls={r.cls} spec={r.spec} />{r.offspec && <Text size="xs" c="dimmed">OS</Text>}</Group></Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{r.boss}</Text></Table.Td>
                  {scores && <>
                    <Table.Td><Text size="xs">{r.tier} · {r.weight?.toFixed(2)}</Text></Table.Td>
                    <Table.Td><SourceBadge source={r.source} /></Table.Td>
                    <Table.Td><Text size="xs">{r.bot || "—"}</Text></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{r.by || "—"}</Text></Table.Td>
                    <Table.Td><Text size="xs">{r.reason || ""}</Text></Table.Td>
                  </>}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
    </Stack>
  );
}
