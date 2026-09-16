import { useEffect, useState } from "react";
import { AppShell, Box, Burger, Group, NavLink, Stack, Text, Title } from "@mantine/core";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { IconAdjustments, IconChartBar, IconListDetails, IconMountain, IconTable, IconUser, IconUsers } from "@tabler/icons-react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, type Meta } from "./api";
import { MePage } from "./pages/Me";

const NAV = [
  { to: "/me", label: "Me", icon: IconUser },
  { to: "/rosters", label: "Rosters", icon: IconTable },
  { to: "/raids", label: "Raids", icon: IconMountain },
];
const OFFICER = [
  { to: "/bank", label: "Bank", icon: IconUsers },
  { to: "/admin", label: "Admin", icon: IconAdjustments },
  { to: "/ops", label: "Ops", icon: IconChartBar },
  { to: "/config", label: "Config", icon: IconListDetails },
];

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [opened, { toggle, close }] = useDisclosure();
  const mobile = useMediaQuery("(max-width: 48em)");
  const loc = useLocation();
  useEffect(() => { api.get<Meta>("/api/meta").then(setMeta).catch(() => {}); }, []);
  useEffect(() => close(), [loc.pathname, close]);
  if (!meta) return <Box p="xl"><Text c="dimmed">Loading…</Text></Box>;

  const link = (n: { to: string; label: string; icon: React.ElementType }) => (
    <NavLink key={n.to} component={Link} to={n.to} label={n.label} leftSection={<n.icon size={18} stroke={1.8} />} active={loc.pathname.startsWith(n.to)} variant="light" color="teal" style={{ borderRadius: 7 }} />
  );
  return (
    <AppShell navbar={{ width: 212, breakpoint: "sm", collapsed: { mobile: !opened } }} header={{ height: 52, collapsed: !mobile }} padding="lg">
      <AppShell.Header hiddenFrom="sm">
        <Group h="100%" px="md"><Burger opened={opened} onClick={toggle} size="sm" /><Text fw={700} ff="Manrope">oibot_GM</Text></Group>
      </AppShell.Header>
      <AppShell.Navbar p="sm" style={{ background: "var(--mantine-color-slate-7)" }}>
        <Group gap="sm" px="xs" pb="md" visibleFrom="sm">
          <Box style={{ width: 30, height: 30, borderRadius: 8, background: "linear-gradient(135deg, var(--mantine-color-teal-4), #1F6F6B)" }} />
          <Box><Title order={3} size="h5">oibot_GM</Title><Text size="xs" c="dimmed">{meta.guild} · WoW: Forever</Text></Box>
        </Group>
        <Stack gap={2}>{NAV.map(link)}</Stack>
        {meta.viewer.officer && (
          <Stack gap={2} mt="md">
            <Text size="xs" c="dimmed" fw={700} tt="uppercase" px="xs" style={{ letterSpacing: ".1em" }}>Officers</Text>
            {OFFICER.map(link)}
          </Stack>
        )}
        <Group mt="auto" gap="sm" px="xs" pt="md" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
          <Box style={{ width: 30, height: 30, borderRadius: "50%", background: "linear-gradient(135deg, #C79C6E, #6F4E2A)" }} />
          <Box style={{ minWidth: 0 }}><Text size="sm" truncate>{meta.viewer.name}</Text><Text size="xs" c="dimmed">{meta.viewer.owner ? "owner" : meta.viewer.officer ? "officer" : "member"} · <a href="/auth/logout" style={{ color: "inherit" }}>log out</a></Text></Box>
        </Group>
      </AppShell.Navbar>
      <AppShell.Main style={{ background: "var(--mantine-color-slate-8)" }}>
        <Box maw={1180} mx="auto">
          <Routes>
            <Route path="/me" element={<MePage meta={meta} />} />
            <Route path="/rosters" element={<Soon name="Rosters" />} />
            <Route path="/raids" element={<Soon name="Raids" />} />
            <Route path="/bank" element={<Soon name="Bank" />} />
            <Route path="/admin" element={<Soon name="Admin" />} />
            <Route path="/ops" element={<Soon name="Ops" />} />
            <Route path="/config" element={<Soon name="Config" />} />
            <Route path="*" element={<Navigate to="/me" replace />} />
          </Routes>
        </Box>
      </AppShell.Main>
    </AppShell>
  );
}

function Soon({ name }: { name: string }) {
  const legacy: Record<string, string> = { Rosters: "/rosters", Raids: "/raids", Bank: "/bank", Admin: "/admin", Ops: "/ops", Config: "/config" };
  return <Box><Title order={1} size="h2">{name}</Title><Text c="dimmed" mt="xs">This page is being rebuilt. The current version is still at <a href={legacy[name]} style={{ color: "var(--mantine-color-teal-4)" }}>{legacy[name]}</a>.</Text></Box>;
}
