import { Box, Group, SimpleGrid, Text, Tooltip } from "@mantine/core";
import type { Aura, GroupSummary, Meta } from "../api";
import { GameIcon } from "./Icons";
import { CLASS_COLOURS } from "../theme";

const OK: Record<string, string> = { green: "var(--mantine-color-green-5)", amber: "var(--mantine-color-yellow-5)", red: "var(--mantine-color-red-5)" };

function AuraBadge({ a, missing, who }: { a: Aura; missing?: boolean; who?: string }) {
  const tip = `${a.name}${missing ? " — nobody in this group brings it" : who ? ` — ${who}` : ""}`;
  return (
    <Tooltip label={tip}>
      {a.art ? (
        <Box component="img" src={`/img/icon/${a.art}.jpg`} alt={a.abbr} style={{ width: 26, height: 26, borderRadius: 5, border: `2px solid ${missing ? "var(--mantine-color-red-5)" : "transparent"}`, opacity: missing ? 0.35 : 1, filter: missing ? "grayscale(1)" : undefined }} />
      ) : (
        <Box style={{ minWidth: 30, textAlign: "center", padding: "2px 5px", borderRadius: 6, fontSize: 11, fontWeight: 700, color: missing ? "var(--mantine-color-red-5)" : "#14181F", background: missing ? "transparent" : a.colour, border: missing ? "2px solid var(--mantine-color-red-5)" : undefined }}>{a.abbr}</Box>
      )}
    </Tooltip>
  );
}

/** A run's groups with per-group aura coverage and raid-wide buff status. */
export function GroupsBlock({ meta, sm }: { meta: Meta; sm: GroupSummary | null }) {
  if (!sm) return null;
  return (
    <Box mt="sm">
      <Group gap="xs" mb="sm">
        {Object.entries(sm.roles).map(([role, n]) => (
          <Group key={role} gap={6} px={8} py={2} style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 999 }}><GameIcon meta={meta} kind="role" id={role} size={18} /><Text size="xs">{n} {role}</Text></Group>
        ))}
        {sm.synergy !== null && <Text size="xs" c="dimmed">synergy {sm.synergy}</Text>}
      </Group>
      {sm.groups.length > 0 && (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="sm">
          {sm.groups.map((g) => (
            <Box key={g.n} p="sm" style={{ background: "var(--mantine-color-slate-6)", border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
              <Group justify="space-between" mb={4}><Text size="sm" fw={700}>Group {g.n}</Text><Text size="xs" c="green">+{g.value}</Text></Group>
              {g.members.map((m) => (
                <Group key={m.member} gap={6} wrap="nowrap" style={{ lineHeight: 1.7 }}>
                  <GameIcon meta={meta} kind="role" id={m.role} size={18} title={m.role} />
                  <GameIcon meta={meta} kind="spec" id={`${m.cls}:${m.spec}`} size={18} title={`${m.cls} ${m.spec}`} />
                  <Text size="sm" c={CLASS_COLOURS[m.cls]} truncate>{m.name}</Text>
                  <Text size="xs" c="dimmed" style={{ flex: "none" }}>{m.spec}</Text>
                </Group>
              ))}
              {Array.from({ length: Math.max(0, 5 - g.members.length) }, (_, i) => <Text key={i} size="sm" c="dimmed" style={{ lineHeight: 1.7 }}>· open</Text>)}
              <Group gap={4} mt={6}>
                {g.present.map((a) => <AuraBadge key={a.abbr} a={a} who={a.who} />)}
                {g.missing.map((a) => <AuraBadge key={`m${a.abbr}`} a={a} missing />)}
              </Group>
              {g.picks.length > 0 && <Text size="xs" c="dimmed" mt={4}>totems: {g.picks.join(" · ")}</Text>}
            </Box>
          ))}
        </SimpleGrid>
      )}
      <Group gap="xs" mt="sm">
        <Text size="xs" c="dimmed">raid-wide:</Text>
        {sm.raid.map((b) => (
          <Tooltip key={b.abbr} label={`${b.detail} · ${b.status}`}>
            <Group gap={6} px={8} py={2} style={{ border: `1px solid ${OK[b.ok] || "var(--mantine-color-slate-5)"}`, borderRadius: 999, color: b.ok === "red" ? OK.red : undefined }}>
              {b.art ? <Box component="img" src={`/img/icon/${b.art}.jpg`} alt="" style={{ width: 18, height: 18, borderRadius: 4, opacity: b.n ? 1 : 0.35, filter: b.n ? undefined : "grayscale(1)" }} />
                : <Box style={{ minWidth: 26, textAlign: "center", padding: "1px 4px", borderRadius: 5, fontSize: 10, fontWeight: 700, color: "#14181F", background: b.n ? b.colour : "#3A4351" }}>{b.abbr.slice(0, 4)}</Box>}
              <Text size="xs">{b.name} ×{b.n}</Text>
            </Group>
          </Tooltip>
        ))}
      </Group>
      {sm.unmet.length > 0 && <Text size="xs" c="yellow" mt={6}>nobody in this run brings: {sm.unmet.join(", ")}</Text>}
      <Text size="xs" c="dimmed" mt={4}>{sm.assumptions.join(" · ")}</Text>
    </Box>
  );
}
