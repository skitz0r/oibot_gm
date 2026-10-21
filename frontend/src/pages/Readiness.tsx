import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Anchor, Badge, Box, Card, Group, Stack, Table, Text, Tooltip } from "@mantine/core";
import { api, type Meta, type Readiness, type ReadinessRaid } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail } from "../components/Page";
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
      <PageTitle title="Readiness" intro="What the guild could field for each raid, and how it stands against the desired comp. Officers set the targets here (or with /roster comp); the bounds the solver seats are the owner's, under the raid's rules on the Raids page." />
      {data.raids.map((r) => <RaidReadiness key={r.raid} r={r} meta={meta} slots={data.slots} onSaved={load}
        onEditing={(on) => { editing.current += on ? 1 : -1; if (!on && editing.current < 0) editing.current = 0; }} />)}
    </Stack>
  );
}

function RaidReadiness({ r, meta, slots, onSaved, onEditing }: { r: ReadinessRaid; meta: Meta; slots: Readiness["slots"]; onSaved: () => void; onEditing: (on: boolean) => void }) {
  const words = (k: string) => slots.find((x) => x.value === k)?.label || k;
  const hc = r.headcount;
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
          <Group justify="space-between" mb={4}><Eyebrow>Against the desired comp</Eyebrow><Anchor component={Link} to={`/raids#raid-${r.raid}`} size="xs">Group make-up (bounds) on the Raids page</Anchor></Group>
          <Table.ScrollContainer minWidth={460}>
            <Table verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th w={50} /><Table.Th w={80}>Have</Table.Th><Table.Th w={90}>Aim</Table.Th><Table.Th>Why</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>
                {r.comp.map((c) => (
                  <Table.Tr key={c.key}>
                    <Table.Td><SlotIcon meta={meta} slot={c.key} label={words(c.key)} /></Table.Td>
                    <Table.Td><Text size="sm" fw={600} c={c.have < c.want ? "red" : c.max !== null && c.have > c.max ? "yellow" : "teal"} style={{ fontVariantNumeric: "tabular-nums" }}>{c.have}</Text></Table.Td>
                    <Table.Td style={{ fontVariantNumeric: "tabular-nums" }}>{c.max !== null && c.max !== c.want ? `${c.want}–${c.max}` : c.want}</Table.Td>
                    <Table.Td><Group gap={6} wrap="nowrap">{c.source === "officer" && <Badge size="xs" variant="light" color="teal">target</Badge>}<Text size="xs" c="dimmed">{c.note || c.why}</Text></Group></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {r.notes.map((n, i) => <Text key={i} size="xs" c="dimmed">{n}</Text>)}
        </Box>

        <Box>
          <Eyebrow>Desired comp — the officers' targets</Eyebrow>
          <Box mt={4}><CompTargets meta={meta} rid={r.raid} size={r.size} targets={r.targets} bounds={r.bounds} slots={slots} rev={r.rev} editable onSaved={onSaved} onEditing={onEditing} /></Box>
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
                    ))}</Group>
                  ))}
                </Box>
              ))}
            </Stack>
          </Card>
        )}
      </Stack>
    </Card>
  );
}
