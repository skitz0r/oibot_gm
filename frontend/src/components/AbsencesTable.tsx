import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Switch, Text } from "@mantine/core";
import { api, type AbsenceRow } from "../api";
import { CardHeader, fail } from "./Page";
import { useLive } from "../hooks/useLive";
import { useImBack } from "./AbsencesCard";
import css from "../pages/members.module.css";

/** Officers' Absences tab on the Members page (GET /api/absences, chronological): who · the days in words · the reason ·
 *  who recorded it · I'm back. One row per absence on a grid; on a phone each is a card with When and Reason stacked
 *  under the name and I'm back a full-size button beside it (design §5.58 phase 3). Test puppets only for the officer
 *  running the rehearsal (the server filters). */
export function AbsencesTable({ reloadKey }: { reloadKey?: number }) {
  const [rows, setRows] = useState<AbsenceRow[] | null>(null);
  const [past, setPast] = useState(false);
  // "today" is the GUILD's local date from the server: a browser's UTC date runs a day ahead on a US evening and would
  // hide I'm back on an absence that ends today
  const [today, setToday] = useState<string>("");
  const load = () => api.get<{ rows: AbsenceRow[]; today: string }>(`/api/absences${past ? "?all=true" : ""}`).then((d) => { setRows(d.rows); setToday(d.today); }).catch(fail);
  useEffect(() => { load(); }, [past, reloadKey]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["member"]);
  const [back, dialog, busy] = useImBack(load);
  return (
    <Card className={css.touch}>
      <CardHeader title="Absences" hint="reasons are officers' only" action={<Switch size="xs" label="past ones too" checked={past} onChange={(e) => setPast(e.currentTarget.checked)} />} />
      <div className={css.absHead}>
        {["Member", "When", "Reason", "Recorded by", ""].map((h, i) => <Text key={i} size="xs" fw={700} c="dimmed">{h}</Text>)}
      </div>
      {rows === null && <Text c="dimmed" size="sm" p="md">Loading…</Text>}
      {rows?.length === 0 && (
        <Text c="dimmed" size="sm" p="md">{past ? "No absences on file. " : "Nobody is away. "}Record an absence from a member's ⋮ when someone will miss a raid.</Text>
      )}
      {rows?.map((r) => (
        <div key={`${r.uid}${r.start}`} className={css.absRow}>
          <Box className={css.absWho}>
            <Text size="sm" fw={600}>{r.display_name}</Text>
            {r.test && <Badge size="xs" variant="outline" color="violet" tt="none">puppet</Badge>}
          </Box>
          <Box className={css.absWhen}>
            <Text size="sm">{r.span}</Text>
            {r.current && <Text size="xs" c="dimmed">away now</Text>}
          </Box>
          <Box className={css.absReason}><Text size="sm" c={r.reason ? undefined : "dimmed"}>{r.reason || "no reason given"}</Text></Box>
          <Box className={css.absRecorded}><Text size="sm" c="dimmed"><span className={css.absBy}>recorded by </span>{r.by === r.display_name ? "themselves" : r.by}</Text></Box>
          <Box className={css.absBack}>{!r.end || !today || r.end >= today
            ? <Button size="sm" variant="light" loading={busy === `${r.uid}${r.start}`} onClick={() => back({ start: r.start, end: r.end, reason: r.reason, span: r.span }, { uid: r.uid, display_name: r.display_name })}>I'm back</Button>
            : null}</Box>
        </div>
      ))}
      {dialog}
    </Card>
  );
}
