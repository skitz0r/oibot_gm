import { Box, Group, Text, Title } from "@mantine/core";

/** Emblem + name + meta line, shared by the Rosters and Raids pages. */
export function RaidHeader({ id, name, meta, right }: { id: string; name: string; meta: React.ReactNode; right?: React.ReactNode }) {
  return (
    <Group justify="space-between" align="center" wrap="wrap" p="md" style={{ borderBottom: "1px solid var(--mantine-color-slate-5)" }}>
      <Group gap="md" wrap="nowrap" style={{ minWidth: 0 }}>
        <Box component="img" src={`/img/raid/${id}.png`} alt="" style={{ width: 96, height: 45, objectFit: "cover", borderRadius: 8, border: "1px solid var(--mantine-color-slate-5)", flex: "none" }} />
        <Box style={{ minWidth: 0 }}><Title order={2} size="h4">{name}</Title><Text size="xs" c="dimmed">{meta}</Text></Box>
      </Group>
      {right}
    </Group>
  );
}
