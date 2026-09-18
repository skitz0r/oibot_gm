import { useEffect, useState } from "react";
import { ActionIcon, Badge, Box, Card, SimpleGrid, Stack, Table, Text, Tooltip } from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { api, type Ops as OpsData } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail } from "../components/Page";
import { usePoll } from "../hooks/usePoll";

const LEVEL: Record<string, string> = { error: "red", warn: "yellow", info: "teal" };

const uptime = (secs: number) => (secs >= 3600 ? `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m` : `${Math.floor(secs / 60)}m`);

export function OpsPage() {
  const [data, setData] = useState<OpsData | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = () => api.get<OpsData>("/api/ops").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(load);
  async function refresh() { setRefreshing(true); try { await load(); } finally { setRefreshing(false); } }
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const up = uptime(data.up);
  return (
    <Stack gap="lg">
      <PageTitle title="Ops" intro="What the bot has been doing, and where its data lives. The page refreshes itself while open."
        right={<Tooltip label="Refresh"><ActionIcon variant="default" size="lg" aria-label="refresh" loading={refreshing} onClick={refresh}><IconRefresh size={16} /></ActionIcon></Tooltip>} />
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
        <Stat label="data repo" value={data.head} sub={`push ${data.push ? "on" : "off"}`} />
        <Stat label="model" value={data.llm} />
        <Stat label="loot feed" value={data.feed} />
        <Stat label="uptime" value={up} />
      </SimpleGrid>
      <Card>
        <CardHeader title="Recent actions" hint="newest first" />
        <Table.ScrollContainer minWidth={520}>
          <Table>
            <Table.Tbody>
              {data.rows.length === 0 && <Table.Tr><Table.Td><Text c="dimmed" size="sm">Nothing since the bot started {up} ago.</Text></Table.Td></Table.Tr>}
              {data.rows.map((r, i) => <Table.Tr key={i}><Table.Td w={140}><Text size="xs" c="dimmed" ff="monospace">{r.time}</Text></Table.Td><Table.Td w={80}><Badge size="xs" variant="light" color={LEVEL[r.level] || "gray"}>{r.level}</Badge></Table.Td><Table.Td><Text size="sm">{r.text}</Text></Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      <Card>
        <CardHeader title={`Precedents (${data.precedents.length} newest)`} hint="loot decisions where the council overrode the bot" />
        <Table.ScrollContainer minWidth={640}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Date</Table.Th><Table.Th>Item</Table.Th><Table.Th>Bot picked</Table.Th><Table.Th>Council awarded</Table.Th><Table.Th>Reason</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.precedents.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text c="dimmed" size="sm">none yet</Text></Table.Td></Table.Tr>}
              {data.precedents.map((p, i) => <Table.Tr key={i}><Table.Td><Text size="xs" c="dimmed">{p.date}</Text></Table.Td><Table.Td>{p.item}</Table.Td><Table.Td>{p.bot}</Table.Td><Table.Td>{p.human}</Table.Td><Table.Td>{p.reason}</Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      <Card>
        <CardHeader title={`Ledger (${data.ledger.length} newest)`} />
        <Table.ScrollContainer minWidth={560}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Received</Table.Th><Table.Th>Raider</Table.Th><Table.Th>Item</Table.Th><Table.Th>Source</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.ledger.length === 0 && <Table.Tr><Table.Td colSpan={4}><Text c="dimmed" size="sm">none yet</Text></Table.Td></Table.Tr>}
              {data.ledger.map((a, i) => <Table.Tr key={i}><Table.Td><Text size="xs" c="dimmed">{a.received}</Text></Table.Td><Table.Td>{a.raider}</Table.Td><Table.Td>{a.item_name || a.item_id}</Table.Td><Table.Td><Text size="xs" c="dimmed">{a.source}</Text></Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
    </Stack>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return <Card><Box p="md"><Eyebrow>{label}</Eyebrow><Text fw={600} mt={4} style={{ wordBreak: "break-word" }}>{value}</Text>{sub && <Text size="xs" c="dimmed">{sub}</Text>}</Box></Card>;
}
