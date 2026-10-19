import { useEffect, useState } from "react";
import { Badge, Button, Card, Switch, Table, Text } from "@mantine/core";
import { api, type AbsenceRow } from "../api";
import { CardHeader, fail } from "./Page";
import { useLive } from "../hooks/useLive";
import { useImBack } from "./AbsencesCard";

/** Officers' Absences table on the Members page (GET /api/absences, chronological): who · the days in words · the
 *  reason · who recorded it · I'm back. Test puppets only for the officer running the rehearsal (the server filters). */
export function AbsencesTable({ reloadKey }: { reloadKey?: number }) {
  const [rows, setRows] = useState<AbsenceRow[] | null>(null);
  const [past, setPast] = useState(false);
  const load = () => api.get<{ rows: AbsenceRow[] }>(`/api/absences${past ? "?all=true" : ""}`).then((d) => setRows(d.rows)).catch(fail);
  useEffect(() => { load(); }, [past, reloadKey]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["member"]);
  const [back, dialog, busy] = useImBack(load);
  return (
    <Card>
      <CardHeader title="Absences" hint="reasons are officers' only" action={<Switch size="xs" label="past ones too" checked={past} onChange={(e) => setPast(e.currentTarget.checked)} />} />
      <Table.ScrollContainer minWidth={560}>
        <Table>
          <Table.Thead><Table.Tr><Table.Th w="22%">Member</Table.Th><Table.Th w="26%">When</Table.Th><Table.Th>Reason</Table.Th><Table.Th w="16%">Recorded by</Table.Th><Table.Th w={110} /></Table.Tr></Table.Thead>
          <Table.Tbody>
            {rows === null && <Table.Tr><Table.Td colSpan={5}><Text c="dimmed" size="sm">Loading…</Text></Table.Td></Table.Tr>}
            {rows?.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text c="dimmed" size="sm">Nobody is away.</Text></Table.Td></Table.Tr>}
            {rows?.map((r) => (
              <Table.Tr key={`${r.uid}${r.start}`}>
                <Table.Td><Text size="sm" fw={600}>{r.display_name}</Text>{r.test && <Badge size="xs" variant="outline" color="violet">test</Badge>}</Table.Td>
                <Table.Td><Text size="sm">{r.span}</Text>{r.current && <Badge size="xs" variant="light" color="yellow">away now</Badge>}</Table.Td>
                <Table.Td><Text size="sm" c={r.reason ? undefined : "dimmed"}>{r.reason || "—"}</Text></Table.Td>
                <Table.Td><Text size="sm" c="dimmed">{r.by === r.display_name ? "themselves" : r.by}</Text></Table.Td>
                <Table.Td>{!r.end || r.end >= new Date().toISOString().slice(0, 10)
                  ? <Button size="compact-xs" variant="subtle" loading={busy === `${r.uid}${r.start}`} onClick={() => back({ start: r.start, end: r.end, reason: r.reason, span: r.span }, { uid: r.uid, display_name: r.display_name })}>I'm back</Button>
                  : null}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {dialog}
    </Card>
  );
}
