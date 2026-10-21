import { useEffect, useState } from "react";
import { Badge, Box, Card, Group, Stack, Text, Tooltip } from "@mantine/core";
import { api, type Meta } from "../api";
import { CardHeader, fail } from "./Page";
import { GameIcon } from "./Icons";
import { useLive } from "../hooks/useLive";

/** GET /api/me/attendance (design §3.5 L35/M43, D49). */
interface Attendance {
  sentence: string; window_days: number; attended: number; total: number; tracked: number;
  runs: { key: string; when: string; raid: string; kind: "recurring" | "adhoc"; outcome: string; outcome_label: string; counted: boolean; character: string; cls: string; spec: string }[];
}

const COLOUR: Record<string, string> = { rostered: "teal", benched: "blue", unconfirmed: "yellow", away: "gray", called_out: "red", no_thanks: "red", no_answer: "red" };

/** Me › Attendance: "5 of 6 in the last 30 days" and one line per run of the window — day · raid · the spec icon
 *  (alone: role + spec) · what happened. Every character of mine counts; the bench counts here; an away day is shown
 *  and never counted. Computed by the bot (Registry.attendance) — the page only renders it. */
export function AttendanceCard({ meta }: { meta: Meta }) {
  const [d, setD] = useState<Attendance | null>(null);
  const load = () => api.get<Attendance>("/api/me/attendance").then(setD).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["run:"]);
  if (!d) return null;
  return (
    <Card>
      <CardHeader title="Attendance" hint={d.sentence} />
      <Stack p="md" gap={6}>
        {d.runs.length === 0 && <Text size="sm" c="dimmed">{d.tracked ? `No runs for you in the last ${d.window_days} days.` : "Counts from the first run that ends."}</Text>}
        {d.runs.map((r) => (
          <Group key={r.key} justify="space-between" wrap="nowrap" style={{ opacity: r.counted ? 1 : 0.6 }}>
            <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
              <Text size="xs" c="dimmed" w={78} style={{ flex: "none" }}>{r.when}</Text>
              {r.cls && r.spec && <Tooltip label={`${r.character} · ${r.spec} ${r.cls}`}><Box style={{ display: "inline-flex", flex: "none" }}><GameIcon meta={meta} kind="spec" id={`${r.cls}:${r.spec}`} size={18} title={`${r.spec} ${r.cls}`} /></Box></Tooltip>}
              <Text size="sm" truncate>{r.raid}{r.kind === "adhoc" ? " · ad hoc" : ""}</Text>
            </Group>
            <Badge size="sm" variant="light" color={COLOUR[r.outcome] || "gray"} style={{ flex: "none" }}>{r.outcome_label}</Badge>
          </Group>
        ))}
        {d.runs.length > 0 && <Text size="xs" c="dimmed">The last {d.window_days} days. Rostered and benched runs count as attended here; an away day isn't counted.</Text>}
      </Stack>
    </Card>
  );
}
