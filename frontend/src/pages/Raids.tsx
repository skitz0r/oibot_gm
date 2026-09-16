import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Group, NumberInput, Stack, Switch, Text, TextInput, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconPencil } from "@tabler/icons-react";
import { api, type RaidRule, type Raids } from "../api";
import { RaidHeader } from "../components/RaidHeader";

const ok = (message: string) => notifications.show({ message, color: "teal" });
const fail = (e: unknown) => notifications.show({ message: (e as Error).message || "Something went wrong", color: "red" });

export function RaidsPage() {
  const [data, setData] = useState<Raids | null>(null);
  const load = () => api.get<Raids>("/api/raids").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <Box><Title order={1} size="h2">Raids</Title><Text c="dimmed" size="sm" mt={4}>Each raid's rules: when it first opens, lockout, length, desired comp. Amber marks your override of the game defaults. Runs live on Rosters. Times are {data.tz}.</Text></Box>
      {data.raids.map((r) => <RuleCard key={r.id} r={r} owner={data.owner} onSaved={load} />)}
    </Stack>
  );
}

type Draft = { first_open: string; lockout_days: number | string; duration_hours: number | string; notes: string; auto: boolean; comp: Record<string, { min: number | string; max: number | string }> };

function RuleCard({ r, owner, onSaved }: { r: RaidRule; owner: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [d, setD] = useState<Draft>(() => toDraft(r));
  const [busy, setBusy] = useState(false);
  useEffect(() => setD(toDraft(r)), [r]);
  const over = (k: string) => r.overridden.includes(k);
  const oc = (k: string) => (over(k) ? "yellow" : undefined);

  async function save() {
    setBusy(true);
    try { const x = await api.post<{ message: string }>("/api/admin/raid", { instance: r.id, ...d }); ok(x.message); setEditing(false); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function reset() {
    if (!confirm(`Reset ${r.name} to the game defaults?`)) return;
    setBusy(true);
    try { const x = await api.post<{ message: string }>("/api/admin/raid/reset", { instance: r.id }); ok(x.message); setEditing(false); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const metaLine = [`${r.size}-player`, `${r.runs} run${r.runs === 1 ? "" : "s"}`, r.open ? `${r.open} proposal waiting` : null, r.opened ? `window ${r.window[0]} → ${r.window[1]}` : r.first_open_local ? `opens ${r.window[0]}` : "no opening date set"].filter(Boolean).join(" · ");

  return (
    <Card>
      <RaidHeader id={r.id} name={r.name} meta={metaLine} right={owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={() => setEditing(true)}>Edit rules</Button> : null} />
      <Box p="md">
        {!editing ? (
          <Group gap="xl" wrap="wrap">
            <Fact label="first opens" value={r.first_open_local ? r.first_open_local.replace("T", " ") : "—"} c={oc("first_open")} />
            <Fact label="lockout" value={`${r.lockout_days} days`} c={oc("lockout_days")} />
            <Fact label="duration" value={`${r.duration_hours} h`} c={oc("duration_hours")} />
            {(["tank", "healer", "dps"] as const).map((role) => <Fact key={role} label={role} value={`${r.comp[role]?.min ?? "?"}–${r.comp[role]?.max ?? "?"}`} c={over(`${role}_min`) || over(`${role}_max`) ? "yellow" : undefined} />)}
            <Fact label="auto-propose" value={r.auto ? "daily" : "off"} c={oc("auto")} />
            {r.notes && <Fact label="notes" value={r.notes} c={oc("notes")} />}
          </Group>
        ) : (
          <Stack gap="sm">
            <Group gap="md" wrap="wrap" align="flex-end">
              <TextInput label="first opens" description={`local, ${r.window ? "" : ""}guild time`} type="datetime-local" value={d.first_open} onChange={(e) => setD({ ...d, first_open: e.currentTarget.value })} w={230} />
              <NumberInput label="lockout days" min={1} step={1} value={d.lockout_days} onChange={(v) => setD({ ...d, lockout_days: v })} w={130} />
              <NumberInput label="duration h" min={0.5} step={0.5} value={d.duration_hours} onChange={(v) => setD({ ...d, duration_hours: v })} w={130} />
            </Group>
            <Group gap="md" wrap="wrap" align="flex-end">
              {(["tank", "healer", "dps"] as const).map((role) => (
                <Group key={role} gap={6} align="flex-end">
                  <NumberInput label={`${role} min`} min={0} value={d.comp[role].min} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], min: v } } })} w={100} />
                  <NumberInput label="max" min={0} value={d.comp[role].max} onChange={(v) => setD({ ...d, comp: { ...d.comp, [role]: { ...d.comp[role], max: v } } })} w={100} />
                </Group>
              ))}
            </Group>
            <TextInput label="notes" value={d.notes} onChange={(e) => setD({ ...d, notes: e.currentTarget.value })} />
            <Switch label="auto-propose runs daily" checked={d.auto} onChange={(e) => setD({ ...d, auto: e.currentTarget.checked })} />
            <Group gap="sm" justify="flex-end">
              {r.overridden.length > 0 && <Button variant="subtle" color="red" size="sm" onClick={reset} disabled={busy} mr="auto">Reset to defaults</Button>}
              <Button variant="default" size="sm" onClick={() => { setD(toDraft(r)); setEditing(false); }}>Cancel</Button>
              <Button size="sm" loading={busy} onClick={save}>Save rules</Button>
            </Group>
          </Stack>
        )}
        {(Object.keys(r.comp_targets).length > 0 || r.comp_groups.length > 0) && (
          <Text size="xs" c="dimmed" mt="sm">Comp targets: {Object.keys(r.comp_targets).length ? JSON.stringify(r.comp_targets) : "—"} · group layout: {r.comp_groups.join(", ") || "default"} (set in plain text in the analytics channel)</Text>
        )}
        {!owner && <Text size="xs" c="dimmed" mt="sm">The owner edits raid rules.</Text>}
      </Box>
    </Card>
  );
}

function toDraft(r: RaidRule): Draft {
  return { first_open: r.first_open_local, lockout_days: r.lockout_days, duration_hours: r.duration_hours, notes: r.notes, auto: r.auto,
    comp: Object.fromEntries(["tank", "healer", "dps"].map((role) => [role, { min: r.comp[role]?.min ?? "", max: r.comp[role]?.max ?? "" }])) };
}

function Fact({ label, value, c }: { label: string; value: string; c?: string }) {
  return (
    <Box>
      <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".08em" }}>{label}{c && <Badge size="xs" color={c} variant="light" ml={6}>override</Badge>}</Text>
      <Text size="sm" fw={600} c={c}>{value}</Text>
    </Box>
  );
}
