import { useEffect, useState } from "react";
import { Badge, Card, Group, Stack, Table, Text } from "@mantine/core";
import { api, type Bank as BankData, type Meta } from "../api";
import { GameIcon } from "../components/Icons";
import { PageTitle, fail } from "../components/Page";
import { CLASS_COLOURS } from "../theme";

export function BankPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<BankData | null>(null);
  useEffect(() => { api.get<BankData>("/api/bank").then(setData).catch(fail); }, []);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Character bank" intro={`${data.rows.length} members with characters · ${data.members} members total · sorted tanks, healers, melee, ranged`} />
      <Card>
        <Table.ScrollContainer minWidth={760}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Member</Table.Th><Table.Th>Main</Table.Th><Table.Th>Spec</Table.Th><Table.Th>Role</Table.Th><Table.Th>Rank</Table.Th><Table.Th>Rosters</Table.Th><Table.Th>Status</Table.Th><Table.Th>Alts</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.rows.map((r) => (
                <Table.Tr key={r.member}>
                  <Table.Td><Text size="sm" fw={600}>{r.member}</Text></Table.Td>
                  {r.main ? (
                    <>
                      <Table.Td><Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={r.main.cls} size={26} /><Text size="sm" c={CLASS_COLOURS[r.main.cls]} style={{ whiteSpace: "nowrap" }}>{r.main.name || `${r.main.cls} (unnamed)`}</Text></Group></Table.Td>
                      <Table.Td><Text size="sm">{r.main.spec}{r.main.offspec ? <Text span c="dimmed"> / {r.main.offspec}</Text> : null}</Text></Table.Td>
                      <Table.Td>{r.role && <Group gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={r.role} size={18} /><Text size="sm">{r.role}</Text></Group>}</Table.Td>
                      <Table.Td><Badge variant="outline" color="gray">{r.main.rank}</Badge></Table.Td>
                      <Table.Td><Text size="sm" c={r.main.rosters.length ? undefined : "dimmed"}>{r.main.rosters.join(", ") || "—"}</Text></Table.Td>
                      <Table.Td><Badge variant="light" color={r.main.status === "active" ? "teal" : "gray"}>{r.main.status}</Badge></Table.Td>
                    </>
                  ) : <Table.Td colSpan={6}><Text size="sm" c="dimmed">no main</Text></Table.Td>}
                  <Table.Td>
                    <Group gap="sm">{r.alts.map((a, i) => <Group key={i} gap={6} wrap="nowrap"><GameIcon meta={meta} kind="class" id={a.cls} size={18} /><Text size="sm" c={CLASS_COLOURS[a.cls]}>{a.name || a.cls}</Text><Text size="xs" c="dimmed">{a.spec}</Text></Group>)}</Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Card>
    </Stack>
  );
}
