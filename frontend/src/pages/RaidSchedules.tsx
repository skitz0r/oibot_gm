import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Box, Card, Stack, Text } from "@mantine/core";
import { api, type Meta, type Raids, type Rosters } from "../api";
import { RaidHeader } from "../components/RaidHeader";
import { Eyebrow, PageTitle, fail } from "../components/Page";
import { Schedules } from "../components/ScheduleCard";
import { RegularsSection } from "../components/RegularsBlock";
import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

/** The schedules of every raid, each with its editor (ScheduleCard) — what the old Raids page showed under each raid —
 *  and, under them, the raid's regulars with their next four weeks (the Rosters page's Regulars tab, moved here when
 *  that page became the Events page). It keeps schedule editing and the regulars reachable while the Raids page shows
 *  only a raid's rules (phase 4, Task 6) and is replaced by the Schedules page (Task 11). `/schedules/<id>` scrolls to
 *  that schedule's raid. */
export function RaidSchedulesPage({ meta }: { meta: Meta }) {
  const { id } = useParams();
  const [data, setData] = useState<Raids | null>(null);
  const [runs, setRuns] = useState<Rosters | null>(null);  // the regulars and their forecast (GET /api/rosters)
  const editing = useRef(0);  // schedule editors open: live reloads wait for them (each editor registers its own draft)
  const onEditing = (on: boolean) => { editing.current = Math.max(0, editing.current + (on ? 1 : -1)); };
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  const loadRuns = () => api.get<Rosters>("/api/rosters").then(setRuns).catch(fail);
  useEffect(() => { load(); loadRuns(); }, []);
  useRefresh(() => { load(); loadRuns(); });
  useLive(() => { if (!editing.current) load(); }, ["config", "run"]);
  useLive(loadRuns, ["member", "config"]);
  useEffect(() => {
    if (!data || !id) return;
    const r = data.raids.find((x) => x.schedules.some((s) => s.id === id));
    if (r) document.getElementById(`raid-${r.id}`)?.scrollIntoView({ block: "start" });
  }, [data, id]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Schedules" intro="Each raid's schedules: when its events start and the timing of their signup sheets. Under them, the raid's regulars and the next four weeks." />
      {data.raids.map((r) => {
        const rr = runs?.raids.find((x) => x.id === r.id);
        return (
          <Card key={r.id} id={`raid-${r.id}`} style={{ scrollMarginTop: 16 }}>
            <RaidHeader id={r.id} name={r.name} meta={r.page?.details.find((f) => f.label === "Size")?.value} />
            <Schedules r={r} meta={meta} editable policies={data.split_policies} maxRosters={data.max_rosters}
              others={data.raids.filter((x) => x.id !== r.id).map((x) => ({ id: x.id, name: x.name, duration: x.duration_hours }))}
              taken={data.raids.flatMap((x) => x.schedules.map((sc) => sc.id))} onSaved={load} onEditing={onEditing} />
            {rr && (
              <Box p="md" pt={0} id={`regulars-${r.id}`}>
                <Box mb={6}><Eyebrow plain>Regulars</Eyebrow></Box>
                <RegularsSection r={rr} meta={meta} people={runs?.people || []} onReload={loadRuns} />
              </Box>
            )}
          </Card>
        );
      })}
    </Stack>
  );
}
