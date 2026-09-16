import { useEffect, useMemo, useState } from "react";
import { Accordion, Badge, Box, Button, Card, Checkbox, Group, Modal, NumberInput, Select, Stack, Switch, Table, Text, TextInput, Tooltip } from "@mantine/core";
import { IconHammer, IconPencil, IconPlus } from "@tabler/icons-react";
import { api, type Admin as AdminData, type AdminRow, type Build, type Meta, type RosterCfg } from "../api";
import { GameIcon } from "../components/Icons";
import { CardHeader, Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { CLASS_COLOURS } from "../theme";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const hourLabel = (h: number) => (h === 0 ? "12a" : h < 12 ? `${h}a` : h === 12 ? "12p" : `${h - 12}p`);

type Edit = Record<string, { rank: string; placed: Record<string, boolean> }>;

export function AdminPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<AdminData | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [build, setBuild] = useState<Build | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<AdminData>("/api/admin").then(setData).catch(fail);
  useEffect(() => { load(); }, []);

  function startEdit() {
    if (!data) return;
    setEdit(Object.fromEntries(data.rows.filter((r) => r.main).map((r) => [r.uid, { rank: r.main!.rank, placed: Object.fromEntries(Object.entries(r.placed).map(([k, v]) => [k, !!v])) }])));
  }
  async function saveEdit() {
    if (!edit) return;
    setBusy("save");
    try { const r = await api.post<{ message: string }>("/api/admin/members", { rows: Object.entries(edit).map(([uid, e]) => ({ uid, ...e })) }); ok(r.message); setEdit(null); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function act(key: string, path: string, payload: unknown, then?: () => void) {
    setBusy(key);
    try { const r = await api.post<{ message: string }>(path, payload); ok(r.message); then?.(); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  async function runBuild() {
    setBusy("build");
    try { setBuild(await api.post<Build>("/api/admin/build", {})); } catch (e) { fail(e); } finally { setBusy(null); }
  }

  if (!data) return <Text c="dimmed">Loading…</Text>;
  const rosters = data.rosters.filter((t) => !t.ephemeral);
  return (
    <Stack gap="lg">
      <PageTitle title="Roster admin" intro="Everyone with a character. Set ranks and confirm named characters here; runs are planned from availability on Rosters."
        right={<Group gap="sm">{rosters.length > 0 && <Button variant="default" leftSection={<IconHammer size={15} />} loading={busy === "build"} onClick={runBuild}>Build standing rosters</Button>}{!edit && <Button variant="default" leftSection={<IconPencil size={15} />} onClick={startEdit}>Edit ranks & placements</Button>}</Group>} />

      <Card>
        <CardHeader title="Pool" hint={`${data.rows.length} members`} />
        <Table.ScrollContainer minWidth={820}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Member</Table.Th><Table.Th>Main</Table.Th><Table.Th>Role</Table.Th><Table.Th>Rank</Table.Th><Table.Th>Confirmed</Table.Th>{rosters.map((t) => <Table.Th key={t.key}>{t.name || t.key} ({t.size})</Table.Th>)}<Table.Th>Alts</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.rows.map((r) => <Row key={r.uid} r={r} meta={meta} rosters={rosters} ranks={data.ranks} edit={edit} setEdit={setEdit} busy={busy} onConfirm={() => act(`c${r.uid}`, "/api/admin/confirm", { label: r.main!.label })} />)}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {edit && (
          <Group p="sm" px="md" justify="flex-end" gap="sm" style={{ borderTop: "1px solid var(--mantine-color-slate-5)", background: "var(--mantine-color-slate-6)" }}>
            <Text size="sm" c="dimmed" mr="auto">Rank and placement changes apply on save</Text>
            <Button variant="default" size="sm" onClick={() => setEdit(null)}>Cancel</Button>
            <Button size="sm" loading={busy === "save"} onClick={saveEdit}>Save changes</Button>
          </Group>
        )}
      </Card>

      <Card>
        <CardHeader title="Raid times" hint={`${data.grid_members} mains have filled the grid · ${data.tz}`} />
        <Box p="md">
          <SlotsForm slots={data.slots} owner={data.owner} busy={busy === "slots"} onSave={(value) => act("slots", "/api/admin/slots", { value })} />
          <HeatMap heat={data.week_heat} n={data.grid_members} />
          {data.heat.length > 0 && (
            <Table.ScrollContainer minWidth={720} mt="md">
              <Table>
                <Table.Thead><Table.Tr><Table.Th>Slot</Table.Th><Table.Th>Yes</Table.Th><Table.Th>Maybe</Table.Th><Table.Th>Tanks</Table.Th><Table.Th>Healers</Table.Th><Table.Th>Melee</Table.Th><Table.Th>Ranged</Table.Th><Table.Th>No</Table.Th><Table.Th>Not answered</Table.Th></Table.Tr></Table.Thead>
                <Table.Tbody>
                  {data.heat.map((h) => (
                    <Table.Tr key={h.slot}>
                      <Table.Td><Text fw={600} size="sm">{h.slot}</Text></Table.Td>
                      <Table.Td><Text c="teal" fw={700}>{h.yes.length}</Text><Text size="xs" c="dimmed">{h.yes.join(", ")}</Text></Table.Td>
                      <Table.Td><Text c="yellow" fw={700}>{h.maybe.length}</Text><Text size="xs" c="dimmed">{h.maybe.join(", ")}</Text></Table.Td>
                      <Table.Td>{h.roles.tank}</Table.Td><Table.Td>{h.roles.healer}</Table.Td><Table.Td>{h.roles.melee}</Table.Td><Table.Td>{h.roles.ranged}</Table.Td>
                      <Table.Td><Text c="dimmed">{h.no.length}</Text></Table.Td>
                      <Table.Td><Text c="dimmed">{h.unset.length}</Text><Text size="xs" c="dimmed">{h.unset.slice(0, 8).join(", ")}{h.unset.length > 8 ? "…" : ""}</Text></Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Box>
      </Card>

      <Card>
        <CardHeader title="Standing rosters" hint="optional — a fixed weekly team; the planner doesn't need these" />
        <Box p="md">
          {rosters.length === 0 && <Text size="sm" c="dimmed">None. Runs come from the planner on Rosters; add a standing roster only if you want a fixed weekly team.</Text>}
          <Accordion variant="separated">
            {rosters.map((t) => (
              <Accordion.Item key={t.key} value={t.key}>
                <Accordion.Control><Group gap="sm"><Text fw={600}>{t.name || t.key}</Text><Text size="xs" c="dimmed">{t.key} · {t.size} · {t.schedule || "no schedule"} · {t.instance || "no instance"}</Text></Group></Accordion.Control>
                <Accordion.Panel>
                  <RosterSettings t={t} instances={data.instances} owner={data.owner} busy={busy} act={act} />
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
          {data.owner && <NewRoster busy={busy === "new"} onCreate={(d) => act("new", "/api/admin/roster/settings", { ...d, autofill: true })} />}
        </Box>
      </Card>

      <BuildModal build={build} meta={meta} busy={busy === "apply"} onClose={() => setBuild(null)} onApply={() => build && act("apply", "/api/admin/build/apply", { token: build.token }, () => setBuild(null))} />
    </Stack>
  );
}

function Row({ r, meta, rosters, ranks, edit, setEdit, busy, onConfirm }: { r: AdminRow; meta: Meta; rosters: RosterCfg[]; ranks: string[]; edit: Edit | null; setEdit: (e: Edit) => void; busy: string | null; onConfirm: () => void }) {
  const e = edit?.[r.uid];
  const upd = (patch: Partial<{ rank: string; placed: Record<string, boolean> }>) => edit && e && setEdit({ ...edit, [r.uid]: { ...e, ...patch } });
  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm" fw={600}>{r.display_name}</Text>
        <Text size="xs" c="dimmed">{r.verification}</Text>
        <Group gap={4} mt={2}>{r.asks.map((a, i) => <Badge key={i} size="xs" variant="light" color={a.answer === "yes" ? "teal" : a.answer === "no" ? "red" : "yellow"}>{a.roster} {a.answer || "asked"}</Badge>)}</Group>
      </Table.Td>
      <Table.Td>
        {r.main ? <Group gap="sm" wrap="nowrap"><GameIcon meta={meta} kind="class" id={r.main.cls} size={26} /><Box><Text size="sm" c={CLASS_COLOURS[r.main.cls]} style={{ whiteSpace: "nowrap" }}>{r.main.label}</Text><Text size="xs" c="dimmed">{r.main.spec}{r.main.offspec ? ` / ${r.main.offspec}` : ""}</Text></Box></Group> : <Text size="sm" c="dimmed">no main</Text>}
      </Table.Td>
      <Table.Td>{r.role && <Group gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={r.role} size={18} /><Text size="sm">{r.role}</Text></Group>}{r.flex.length > 0 && <Text size="xs" c="dimmed">+{r.flex.join(", ")}</Text>}</Table.Td>
      <Table.Td>{r.main && (e ? <Select size="xs" w={100} data={ranks} value={e.rank} onChange={(v) => upd({ rank: v || e.rank })} /> : <Badge variant="outline" color="gray">{r.main.rank}</Badge>)}</Table.Td>
      <Table.Td>
        {r.main && r.main.name ? (r.main.confirmed ? <Badge variant="light" color="teal">confirmed</Badge> : <Button size="xs" variant="default" loading={busy === `c${r.uid}`} onClick={onConfirm}>Confirm</Button>) : <Text size="xs" c="dimmed">unnamed</Text>}
      </Table.Td>
      {rosters.map((t) => (
        <Table.Td key={t.key}>
          {e ? <Checkbox size="sm" checked={!!e.placed[t.key]} label={r.placed[t.key] || (r.main ? "place" : "")} disabled={!r.main} onChange={(ev) => upd({ placed: { ...e.placed, [t.key]: ev.currentTarget.checked } })} />
            : r.placed[t.key] ? <Badge variant="light" color="teal">{r.placed[t.key]}</Badge> : <Text size="xs" c="dimmed">—</Text>}
        </Table.Td>
      ))}
      <Table.Td><Group gap="sm">{r.alts.map((a) => <Group key={a.label} gap={6} wrap="nowrap"><GameIcon meta={meta} kind="class" id={a.cls} size={18} /><Text size="sm" c={CLASS_COLOURS[a.cls]}>{a.label}</Text><Text size="xs" c="dimmed">{a.spec}</Text></Group>)}</Group></Table.Td>
    </Table.Tr>
  );
}

function SlotsForm({ slots, owner, busy, onSave }: { slots: string[]; owner: boolean; busy: boolean; onSave: (v: string) => void }) {
  const [v, setV] = useState(slots.join(", "));
  useEffect(() => setV(slots.join(", ")), [slots]);
  return (
    <Group gap="sm" align="flex-end" wrap="wrap" mb="md">
      <TextInput label="Candidate raid times members rate" description="Tue 19:30, Thu 20:00, Sun 18:00" value={v} onChange={(e) => setV(e.currentTarget.value)} style={{ flex: "1 1 320px" }} disabled={!owner} />
      {owner ? <Button size="sm" variant="default" loading={busy} disabled={v === slots.join(", ")} onClick={() => onSave(v)}>Save</Button> : <Text size="xs" c="dimmed">the owner sets these</Text>}
    </Group>
  );
}

function HeatMap({ heat, n }: { heat: [number, number][][]; n: number }) {
  const cells = useMemo(() => heat.map((row, d) => row.map(([p, a], i) => {
    const tot = p + a;
    const rgb = p >= a ? "46,158,107" : "184,137,42";
    return <Tooltip key={`${d}:${i}`} label={`${DAYS[d]} ${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"} — ${p} preferred, ${a} available`} openDelay={300}>
      <Box style={{ height: 18, borderRadius: 2, background: tot ? `rgba(${rgb}, ${(0.15 + 0.85 * (tot / (n || 1))).toFixed(2)})` : "var(--mantine-color-slate-6)" }} />
    </Tooltip>;
  })), [heat, n]);
  return (
    <Box>
      <Eyebrow>Availability heat-map</Eyebrow>
      <Text size="xs" c="dimmed" mb={6}>darker = more people, green = mostly preferred</Text>
      <Box style={{ overflowX: "auto" }}>
        <Box style={{ display: "grid", gridTemplateColumns: "44px repeat(48, minmax(0, 1fr))", gap: 1, minWidth: 560 }}>
          <Box />
          {Array.from({ length: 24 }, (_, h) => <Text key={h} size="xs" c="dimmed" ff="monospace" style={{ gridColumn: "span 2", fontSize: 10 }}>{h % 2 === 0 ? hourLabel(h) : ""}</Text>)}
          {cells.map((row, d) => [<Text key={`l${d}`} size="xs" c="dimmed" style={{ alignSelf: "center" }}>{DAYS[d]}</Text>, ...row])}
        </Box>
      </Box>
    </Box>
  );
}

function RosterSettings({ t, instances, owner, busy, act }: { t: RosterCfg; instances: string[]; owner: boolean; busy: string | null; act: (key: string, path: string, payload: unknown) => Promise<void> }) {
  const [d, setD] = useState({ name: t.name || t.key, size: t.size || 10, schedule: t.schedule || "", instance: t.instance || "", cutoff_soft_hours: t.cutoff_soft_hours ?? 48, cutoff_hard_hours: t.cutoff_hard_hours ?? 24, open_days_before: t.open_days_before ?? 6, open_dm: !!t.open_dm, autofill: t.autofill !== false });
  const [target, setTarget] = useState({ slot: "", value: "", reason: "" });
  const [groups, setGroups] = useState((t.comp_groups || []).join(", "));
  const targets = Object.entries(t.comp_targets || {});
  return (
    <Stack gap="md">
      <Group gap="sm" align="flex-end" wrap="wrap">
        <TextInput label="name" value={d.name} onChange={(e) => setD({ ...d, name: e.currentTarget.value })} w={140} disabled={!owner} />
        <NumberInput label="size" min={5} max={40} value={d.size} onChange={(v) => setD({ ...d, size: Number(v) || d.size })} w={90} disabled={!owner} />
        <TextInput label="schedule" placeholder="Tue 19:30" value={d.schedule} onChange={(e) => setD({ ...d, schedule: e.currentTarget.value })} w={120} disabled={!owner} />
        <Select label="instance" data={[{ value: "", label: "—" }, ...instances.map((i) => ({ value: i, label: i }))]} value={d.instance} onChange={(v) => setD({ ...d, instance: v || "" })} w={160} disabled={!owner} />
        <NumberInput label="soft cutoff h" value={d.cutoff_soft_hours} onChange={(v) => setD({ ...d, cutoff_soft_hours: Number(v) })} w={110} disabled={!owner} />
        <NumberInput label="hard cutoff h" value={d.cutoff_hard_hours} onChange={(v) => setD({ ...d, cutoff_hard_hours: Number(v) })} w={110} disabled={!owner} />
        <NumberInput label="open days before" value={d.open_days_before} onChange={(v) => setD({ ...d, open_days_before: Number(v) })} w={130} disabled={!owner} />
      </Group>
      <Group gap="lg">
        <Switch label="DM on open" checked={d.open_dm} onChange={(e) => setD({ ...d, open_dm: e.currentTarget.checked })} disabled={!owner} />
        <Switch label="autofill" checked={d.autofill} onChange={(e) => setD({ ...d, autofill: e.currentTarget.checked })} disabled={!owner} />
        {owner ? <Button size="xs" loading={busy === `rs${t.key}`} onClick={() => act(`rs${t.key}`, "/api/admin/roster/settings", { key: t.key, ...d })}>Save settings</Button> : <Text size="xs" c="dimmed">the owner saves settings</Text>}
      </Group>
      <Group gap="xl" align="flex-start" wrap="wrap">
        <Box style={{ flex: "1 1 320px" }}>
          <Eyebrow>Comp targets</Eyebrow>
          <Group gap={6} my={6}>
            {targets.length === 0 && <Text size="xs" c="dimmed">none — derived values apply</Text>}
            {targets.map(([slot, tg]) => <Badge key={slot} variant="outline" color="gray" style={{ cursor: "pointer" }} title="click to clear" onClick={() => act(`ct${slot}`, "/api/admin/comp/target/clear", { key: t.key, slot })}>{slot}: {tg.min}{tg.max != null ? `–${tg.max}` : ""}{tg.note ? ` · ${tg.note}` : ""} ×</Badge>)}
          </Group>
          <Group gap={6} align="flex-end" wrap="wrap">
            <TextInput size="xs" placeholder="healer / Paladin / Shaman:Enhancement" value={target.slot} onChange={(e) => setTarget({ ...target, slot: e.currentTarget.value })} w={220} />
            <TextInput size="xs" placeholder="3, 3-5 or -2" value={target.value} onChange={(e) => setTarget({ ...target, value: e.currentTarget.value })} w={100} />
            <TextInput size="xs" placeholder="why" value={target.reason} onChange={(e) => setTarget({ ...target, reason: e.currentTarget.value })} w={160} />
            <Button size="xs" variant="default" disabled={!target.slot || !target.value} loading={busy === `t${t.key}`} onClick={() => act(`t${t.key}`, "/api/admin/comp/target", { key: t.key, ...target }).then(() => setTarget({ slot: "", value: "", reason: "" }))}>Set</Button>
          </Group>
        </Box>
        <Box style={{ flex: "1 1 320px" }}>
          <Eyebrow>Group layout</Eyebrow>
          <Group gap={6} align="flex-end" mt={6}>
            <TextInput size="xs" placeholder="tank/heal, melee, ranged, casters (blank = default)" value={groups} onChange={(e) => setGroups(e.currentTarget.value)} style={{ flex: 1 }} />
            <Button size="xs" variant="default" loading={busy === `g${t.key}`} onClick={() => act(`g${t.key}`, "/api/admin/comp/groups", { key: t.key, value: groups })}>Set</Button>
          </Group>
        </Box>
      </Group>
      <Group gap="sm">
        <Button size="xs" variant="default" loading={busy === `pa${t.key}`} onClick={() => act(`pa${t.key}`, "/api/admin/place-all", { roster: t.key })}>Place every main on {t.name || t.key}</Button>
        {owner && <Button size="xs" variant="subtle" color="red" loading={busy === `rm${t.key}`} onClick={() => confirm(`Remove roster ${t.key}? Members keep their characters.`) && act(`rm${t.key}`, "/api/admin/roster/remove", { key: t.key })}>Remove roster</Button>}
      </Group>
    </Stack>
  );
}

function NewRoster({ busy, onCreate }: { busy: boolean; onCreate: (d: { key: string; name: string; size: number; schedule: string }) => void }) {
  const [d, setD] = useState({ key: "", name: "", size: 10, schedule: "" });
  return (
    <Group gap="sm" align="flex-end" wrap="wrap" mt="md">
      <TextInput label="new roster key" placeholder="b" value={d.key} onChange={(e) => setD({ ...d, key: e.currentTarget.value })} w={110} />
      <TextInput label="name" placeholder="10-man B" value={d.name} onChange={(e) => setD({ ...d, name: e.currentTarget.value })} w={140} />
      <NumberInput label="size" value={d.size} min={5} max={40} onChange={(v) => setD({ ...d, size: Number(v) || 10 })} w={90} />
      <TextInput label="schedule" placeholder="Thu 20:00" value={d.schedule} onChange={(e) => setD({ ...d, schedule: e.currentTarget.value })} w={120} />
      <Button size="sm" variant="default" leftSection={<IconPlus size={14} />} disabled={!d.key} loading={busy} onClick={() => onCreate(d)}>Create</Button>
    </Group>
  );
}

function BuildModal({ build, meta, busy, onClose, onApply }: { build: Build | null; meta: Meta; busy: boolean; onClose: () => void; onApply: () => void }) {
  const changes = build ? build.adds.length + build.removes.length : 0;
  return (
    <Modal opened={!!build} onClose={onClose} title="Proposed standing rosters" size="xl">
      {build && (
        <Stack gap="md">
          <Text size="sm"><b>{build.status}</b>{build.notes.length ? ` · ${build.notes.join(" · ")}` : ""}</Text>
          <Text size="xs" c="dimmed">One solve over the whole pool: one character per member per roster, one raid per member per time slot, absences and "no" slots respected. Prefers filled seats, preferred times, rank, mains, people who sat out last window, current placements, and one provider of each class buff per roster.</Text>
          {build.shells.map((sh) => (
            <Box key={sh.key}>
              <Group gap="sm"><Text fw={600}>{sh.name}</Text><Text size="xs" c="dimmed">{sh.instance || "no instance"} · {sh.slot} · {sh.seats.length}/{sh.size}</Text>{Object.entries(sh.shortfalls).map(([r, n]) => <Badge key={r} size="xs" color="red" variant="light">short {n} {r}</Badge>)}</Group>
              <Table.ScrollContainer minWidth={480}>
                <Table verticalSpacing={4}>
                  <Table.Tbody>{sh.seats.map((s) => <Table.Tr key={s.uid}><Table.Td w={90}><Group gap={6} wrap="nowrap"><GameIcon meta={meta} kind="role" id={s.role} size={18} /><Text size="xs" c="dimmed">{s.role}</Text></Group></Table.Td><Table.Td><Text size="sm" c={CLASS_COLOURS[s.cls]}>{s.character}</Text></Table.Td><Table.Td><Text size="sm">{s.display_name}</Text></Table.Td><Table.Td><Text size="xs" c="dimmed">{s.spec} · {s.reasons.join(", ")}</Text></Table.Td></Table.Tr>)}</Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            </Box>
          ))}
          <Box>
            <Eyebrow>Changes vs current placements</Eyebrow>
            {changes === 0 ? <Text size="sm" c="teal" mt={4}>No changes — the current placements already match.</Text> : (
              <Group gap={6} mt={6}>
                {build.adds.map((a, i) => <Badge key={`a${i}`} variant="light" color="teal">+ {a.name} ({a.character}) → {a.roster}</Badge>)}
                {build.removes.map((a, i) => <Badge key={`r${i}`} variant="light" color="red">− {a.name} from {a.roster}</Badge>)}
              </Group>
            )}
            {build.unplaced.length > 0 && <Text size="xs" c="dimmed" mt="sm">Not seated: {build.unplaced.map(([n, why]) => `${n} — ${why}`).join(" · ")}</Text>}
          </Box>
          <Group justify="flex-end" gap="sm">
            <Button variant="default" onClick={onClose}>Close</Button>
            <Button disabled={changes === 0} loading={busy} onClick={() => confirm(`Apply ${build.adds.length} placement(s) and ${build.removes.length} removal(s)? Members are asked to confirm by DM.`) && onApply()}>Approve and apply</Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
