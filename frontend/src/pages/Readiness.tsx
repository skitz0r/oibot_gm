import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Anchor, Badge, Box, Button, Card, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import { api, type ForecastRow, type Meta, type Readiness, type ReadinessRaid, type Series } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { RaidHeader } from "../components/RaidHeader";
import { GameIcon } from "../components/Icons";
import { Aura } from "../components/board/shared";
import { CompTargets, SlotIcon } from "../components/CompTargets";
import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";

const LEVEL: Record<string, string> = { green: "teal", amber: "yellow", red: "red" };

/** Readiness (officers; design §3.3 Cluster C, RD-12): per raid, the pool against its size, roles with who could cover,
 *  the buffs nobody brings, and the DESIRED COMP — the officers' targets (Edit targets) against what the pool has; then the
 *  regulars' coverage with the 4-week forecast. Everything is comp.readiness (GET /api/readiness — the analytics card,
 *  MCP raid_readiness and /ask read the same numbers); the bounds are the owner's, on the Raids page. */
export function ReadinessPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<Readiness | null>(null);
  const editing = useRef(0);  // tables in edit mode: a change elsewhere never reloads under them
  const load = () => api.get<Readiness>("/api/readiness").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  useLive(() => { if (!editing.current) load(); }, ["config", "member"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Readiness" intro="What the guild could field for each raid, and how it stands against the aims. Officers set the aims here (or with /roster comp); the limits — the fewest and most of each role a run takes — are the owner's, in the raid's rules on the Raids page." />
      {data.raids.map((r) => <RaidReadiness key={r.raid} r={r} meta={meta} slots={data.slots} onSaved={load}
        onEditing={(on) => { editing.current += on ? 1 : -1; if (!on && editing.current < 0) editing.current = 0; }} />)}
    </Stack>
  );
}

function RaidReadiness({ r, meta, slots, onSaved, onEditing }: { r: ReadinessRaid; meta: Meta; slots: Readiness["slots"]; onSaved: () => void; onEditing: (on: boolean) => void }) {
  const words = (k: string) => slots.find((x) => x.value === k)?.label || k;
  const hc = r.headcount;
  const [ask, confirmDialog] = useConfirm();
  const [opening, setOpening] = useState<string | null>(null);
  /** A red forecast row's one action (design §3.3 R46): open that run's sheet now, ahead of its cadence, so the hole is
   *  worked on early — the same POST /api/raid/<id>/open (rc.open_run with the schedule) as the Raids page and /raid open. */
  async function openEarly(s: Series, f: ForecastRow) {
    const short = Object.entries(f.short).map(([role, k]) => `${k} ${role}${k === 1 ? "" : "s"}`).join(", ");
    if (!(await ask({ title: `Open ${s.name} · ${f.when} now?`, message: `Its regulars leave it short (${short}). The sheet posts in the signup channel now instead of when its schedule would open it, so everyone can answer from today; nobody is DMed unless the schedule DMs on open.`, confirmLabel: "Open it now" }))) return;
    setOpening(f.start);
    try { const x = await api.post<{ message: string }>(`/api/raid/${r.raid}/open`, { start: f.start, schedule: s.id }); ok(x.message); onSaved(); } catch (e) { fail(e); } finally { setOpening(null); }
  }
  return (
    <Card>
      <RaidHeader id={r.raid} name={r.name} meta={`${hc.mains} of ${r.size} mains${hc.alts ? ` · ${hc.alts} alts` : ""}${hc.unnamed ? ` · ${hc.unnamed} unnamed` : ""}`}
        right={<Badge variant="light" color={LEVEL[r.level] || "gray"}>{r.level}</Badge>} />
      <Stack gap="md" p="md">
        <Group gap="lg" wrap="wrap" align="center">
          {r.roles.map((x) => {
            const cover = Object.entries(x.cover).filter(([, v]) => v.length).map(([k, v]) => `${k}: ${v.join(", ")}`).join(" · ");
            return (
              <Tooltip key={x.role} label={[x.hint, cover].filter(Boolean).join(" · ") || x.role}>
                <Group gap={4} wrap="nowrap">
                  <GameIcon meta={meta} kind="role" id={x.role} size={20} title={x.role} />
                  <Text size="sm" fw={x.level === "red" ? 700 : 500} c={x.level === "red" ? "red" : x.level === "amber" ? "yellow" : undefined} style={{ fontVariantNumeric: "tabular-nums" }}>{x.need ? `${x.have}/${x.need}` : x.have}</Text>
                </Group>
              </Tooltip>
            );
          })}
          {r.missing_buffs.length > 0 && <Group gap={4} aria-label="buffs nobody brings">{r.missing_buffs.map((b) => <Aura key={b.id} a={b} missing />)}</Group>}
        </Group>

        <Box>
          <Group justify="space-between" mb={4}><Eyebrow>Against the aims</Eyebrow><Anchor component={Link} to={`/raids#raid-${r.raid}`} size="xs">Limits on the Raids page</Anchor></Group>
          <Table.ScrollContainer minWidth={460}>
            <Table verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th w={50} /><Table.Th w={80}>Have</Table.Th><Table.Th w={90}>Aim</Table.Th><Table.Th>Why</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {r.comp.map((c) => (
                  <Table.Tr key={c.key}>
                    <Table.Td><SlotIcon meta={meta} slot={c.key} label={words(c.key)} /></Table.Td>
                    <Table.Td><Text size="sm" fw={600} c={c.have < c.want ? "red" : c.max !== null && c.have > c.max ? "yellow" : "teal"} style={{ fontVariantNumeric: "tabular-nums" }}>{c.have}</Text></Table.Td>
                    <Table.Td style={{ fontVariantNumeric: "tabular-nums" }}>{c.max !== null && c.max !== c.want ? `${c.want}–${c.max}` : c.want}</Table.Td>
                    <Table.Td style={{ whiteSpace: "normal" }}><Group gap={6} wrap="wrap">{c.source === "officer" && <Badge size="xs" variant="light" color="teal">officers' aim</Badge>}<Text size="xs" c="dimmed">{c.note || c.why}</Text></Group></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {r.notes.map((n, i) => <Text key={i} size="xs" c="dimmed">{n}</Text>)}
        </Box>

        <Box>
          <Group gap="sm" align="baseline" wrap="wrap"><Eyebrow>Group make-up</Eyebrow><Text size="xs" c="dimmed">how many of each role a run takes</Text></Group>
          <Box mt={4}><CompTargets meta={meta} rid={r.raid} size={r.size} targets={r.targets} bounds={r.bounds} slots={slots} rev={r.rev} editable onReadiness onSaved={onSaved} onEditing={onEditing} /></Box>
          {r.comp_groups.length > 0 && <Text size="xs" c="dimmed" mt={4}>Group layout: {r.comp_groups.join(", ")} (set in plain text)</Text>}
        </Box>

        {r.series.length > 0 && (
          <Card withBorder={false} p={0}>
            <CardHeader title="Regulars" hint="each recurring schedule's regulars against the tank and healer minimums, and the next four weeks" />
            <Stack gap="xs" p="sm">
              {r.series.map((s) => (
                <Box key={s.key}>
                  <Group gap="sm" wrap="wrap">
                    <Text size="sm" fw={600}>{s.name}</Text>
                    {s.level && <Badge size="xs" variant="light" color={LEVEL[s.level] || "gray"}>{s.level}</Badge>}
                    {!s.active && <Badge size="xs" variant="light" color="gray">paused</Badge>}
                    <Group gap={8}>{Object.entries(s.need).map(([role, need]) => (
                      <Group key={role} gap={3} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={14} title={role} /><Text size="xs" style={{ fontVariantNumeric: "tabular-nums" }}>{s.counts[role] || 0}/{need}</Text></Group>
                    ))}</Group>
                  </Group>
                  <Text size="xs" c="dimmed">{s.label}{s.cover?.length ? ` · could cover: ${s.cover.join(", ")}` : ""}{s.recruit?.length ? ` · to ask: ${s.recruit.join(", ")}` : ""}</Text>
                  {(s.forecast || []).filter((f) => Object.keys(f.short || {}).length).map((f) => (
                    <Group key={f.start} gap={6} wrap="nowrap"><Text size="xs">{f.when}</Text>{Object.entries(f.short).map(([role, k]) => (
                      <Group key={role} gap={2} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={12} title={role} /><Text size="xs" c="red" fw={700}>{k}</Text></Group>
                    ))}
                    {f.open_key ? <Anchor component={Link} to="/rosters" size="xs">sheet open</Anchor>
                      : <Button size="compact-xs" variant="light" color="red" loading={opening === f.start} onClick={() => openEarly(s, f)}>Open this run early</Button>}
                    </Group>
                  ))}
                </Box>
              ))}
            </Stack>
          </Card>
        )}
      </Stack>
      {confirmDialog}
    </Card>
  );
}
