import { useEffect, useState } from "react";
import { Badge, Card, Group, Stack, Table, Text } from "@mantine/core";
import { api, type Companion } from "../../api";
import { CardHeader, fail } from "../Page";
import { usePoll } from "../../hooks/usePoll";
import { COMPANION_COLOUR } from "./shared";

/** GET /api/loot/companions: presence for officers; `tokens` (the owner's only) names the feed-token variables and
 *  whether each is set — never a value (secrets live in .env on the host). */
interface CompanionsCard {
  companions: Companion[]; listening: boolean; many: boolean; owner: boolean;
  tokens: { shared: boolean; shared_feeds: boolean; var: string; set: boolean; listener: boolean; bind_set: boolean } | null;
}

const YES = (on: boolean) => <Badge size="sm" variant="light" color={on ? "teal" : "gray"}>{on ? "set" : "not set"}</Badge>;

/** Loot › Companions (design §3.1 L22 + the tokens card, phase 3): which companions feed this guild (live · quiet ·
 *  disconnected, last seen, events) and, for the owner, which token variables the host has — names only. */
export function Companions() {
  const [d, setD] = useState<CompanionsCard | null>(null);
  const load = () => api.get<CompanionsCard>("/api/loot/companions").then(setD).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(load);
  if (!d) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title="Companions" hint={d.listening ? "the loot feed is listening" : "the loot feed is off on this host"} />
        <Table.ScrollContainer minWidth={420}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Character</Table.Th><Table.Th>State</Table.Th><Table.Th>Last seen</Table.Th><Table.Th>Events</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {d.companions.length === 0 && <Table.Tr><Table.Td colSpan={4}><Text size="sm" c="dimmed">No companion has connected for this guild.</Text></Table.Td></Table.Tr>}
              {d.companions.map((c) => (
                <Table.Tr key={`${c.character}-${c.client}`}>
                  <Table.Td><Text size="sm" fw={600}>{c.character}</Text><Text size="xs" c="dimmed">{c.client}</Text></Table.Td>
                  <Table.Td><Badge variant="light" color={COMPANION_COLOUR[c.state] || "gray"}>{c.state}</Badge></Table.Td>
                  <Table.Td><Text size="sm">{c.last_seen_label || "—"}</Text></Table.Td>
                  <Table.Td>{c.events ?? 0}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
      {d.tokens && (
        <Card>
          <CardHeader title="Feed tokens" hint="owner · names only, never the values" />
          <Stack p="md" gap="xs">
            <Group gap="sm"><Text size="sm" ff="monospace">{d.tokens.var}</Text>{YES(d.tokens.set)}<Text size="xs" c="dimmed">this guild's own token: a companion using it feeds this guild only</Text></Group>
            <Group gap="sm"><Text size="sm" ff="monospace">OIBOT_FEED_TOKEN</Text>{YES(d.tokens.shared)}
              <Text size="xs" c="dimmed">{d.tokens.shared ? (d.tokens.shared_feeds ? "the shared token feeds this guild (the bot serves one guild)" : "the shared token feeds no guild's raids while the bot serves several") : "no shared token"}</Text></Group>
            <Group gap="sm"><Text size="sm" ff="monospace">OIBOT_FEED_BIND</Text>{YES(d.tokens.bind_set)}<Text size="xs" c="dimmed">the tailnet address the feed listens on (not shown)</Text></Group>
            <Text size="xs" c="dimmed">Tokens are set in .env on the host and read when the bot starts. Give the raider running the companion the token, never post it in a channel. A companion older than the one in the repo is refused on its first kill ("companion too old") — the ops channel says so once.</Text>
          </Stack>
        </Card>
      )}
    </Stack>
  );
}
