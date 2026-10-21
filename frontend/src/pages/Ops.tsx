import { useEffect, useState } from "react";
import { Badge, Box, Card, Group, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { api, type Ops as OpsData } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail } from "../components/Page";
import { PlainPermissions } from "../components/PlainPermissions";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

const LEVEL: Record<string, string> = { error: "red", warn: "yellow", info: "teal" };

const uptime = (secs: number) => (secs >= 3600 ? `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m` : `${Math.floor(secs / 60)}m`);

export function OpsPage() {
  const [data, setData] = useState<OpsData | null>(null);
  const load = () => api.get<OpsData>("/api/ops").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(load);
  useLive(load, ["ops"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const up = uptime(data.up);
  const bot = data.scope === "bot" ? " · whole bot" : "";  // several guilds: these are the process's, not this guild's
  return (
    <Stack gap="lg">
      <PageTitle title="Ops" intro="What the bot has been doing, and where its data lives. The page refreshes itself while open. Precedents and the award log are on the Loot pages."
        right={(data.suppressed || data.ops_failing) ? <Group gap="xs">
          {data.suppressed ? <Badge color="red" variant="light" title="errors folded into an earlier alert instead of a DM of their own (one DM per distinct error per 30 minutes)">{data.suppressed} more errors, not DMed</Badge> : null}
          {data.ops_failing ? <Badge color="red" variant="light" title="the bot couldn't post its last lines in the ops channel">ops channel failing</Badge> : null}
        </Group> : undefined} />
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
        <Stat label={`data repo${bot}`} value={data.head} sub={`push ${data.push ? "on" : "off"}`} />
        <Stat label={`model${bot}`} value={data.llm} />
        <Stat label="loot feed" value={data.feed} />
        <Stat label={`uptime${bot}`} value={up} />
      </SimpleGrid>
      <PlainPermissions />
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
    </Stack>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return <Card><Box p="md"><Eyebrow>{label}</Eyebrow><Text fw={600} mt={4} style={{ wordBreak: "break-word" }}>{value}</Text>{sub && <Text size="xs" c="dimmed">{sub}</Text>}</Box></Card>;
}
