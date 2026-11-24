import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Card, Stack, Text } from "@mantine/core";
import { api, type Meta, type Raids } from "../api";
import { RaidHeader } from "../components/RaidHeader";
import { PageTitle, fail } from "../components/Page";
import { Schedules } from "../components/ScheduleCard";
import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

/** The schedules of every raid, each with its editor (ScheduleCard) — what the old Raids page showed under each raid.
 *  It keeps schedule editing reachable while the Raids page shows only a raid's rules (phase 4, Task 6) and is replaced
 *  by the Schedules page (Task 11). `/schedules/<id>` scrolls to that schedule's raid. */
export function RaidSchedulesPage({ meta }: { meta: Meta }) {
  const { id } = useParams();
  const [data, setData] = useState<Raids | null>(null);
  const editing = useRef(0);  // schedule editors open: live reloads wait for them (each editor registers its own draft)
  const onEditing = (on: boolean) => { editing.current = Math.max(0, editing.current + (on ? 1 : -1)); };
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  useLive(() => { if (!editing.current) load(); }, ["config", "run"]);
  useEffect(() => {
    if (!data || !id) return;
    const r = data.raids.find((x) => x.schedules.some((s) => s.id === id));
    if (r) document.getElementById(`raid-${r.id}`)?.scrollIntoView({ block: "start" });
  }, [data, id]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Schedules" intro="Each raid's schedules: when its events start and the timing of their signup sheets." />
      {data.raids.map((r) => (
        <Card key={r.id} id={`raid-${r.id}`} style={{ scrollMarginTop: 16 }}>
          <RaidHeader id={r.id} name={r.name} meta={r.page?.details.find((f) => f.label === "Size")?.value} />
          <Schedules r={r} meta={meta} editable policies={data.split_policies} maxRosters={data.max_rosters}
            others={data.raids.filter((x) => x.id !== r.id).map((x) => ({ id: x.id, name: x.name, duration: x.duration_hours }))}
            taken={data.raids.flatMap((x) => x.schedules.map((sc) => sc.id))} onSaved={load} onEditing={onEditing} />
        </Card>
      ))}
    </Stack>
  );
}
