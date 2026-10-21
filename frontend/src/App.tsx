import { useEffect, useState } from "react";
import { REFRESH_EVENT } from "./hooks/usePoll";
import { AppShell, Avatar, Box, Burger, Button, Group, Menu, NavLink, Stack, Text, Title, UnstyledButton } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { IconChartBar, IconCheck, IconSelector, IconSparkles, IconListDetails, IconMountain, IconTable, IconUser, IconUsers, IconRefresh, IconRobot, IconDiamond, IconClipboardList, IconUserPlus } from "@tabler/icons-react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, setGuild, takePrevGuild, type GuildChoice, type Meta } from "./api";
import { NotMember } from "./components/NotMember";
import { MePage } from "./pages/Me";
import { RostersPage } from "./pages/Rosters";
import { RaidsPage } from "./pages/Raids";
import { MembersPage } from "./pages/Members";
import { OpsPage } from "./pages/Ops";
import { ConfigPage } from "./pages/Config";
import { AurasPage } from "./pages/Auras";
import { AgentsPage } from "./pages/Agents";
import { LootPage } from "./pages/Loot";
import { LootLogPage } from "./pages/LootLog";
import { ApplicationsPage } from "./pages/Applications";
import { anyDirty } from "./hooks/useDirty";

const NAV = [{ to: "/me", label: "Me", icon: IconUser }, { to: "/loot-log", label: "Loot log", icon: IconClipboardList }];
const OFFICER = [
  { to: "/rosters", label: "Rosters", icon: IconTable },
  { to: "/raids", label: "Raids", icon: IconMountain },
  { to: "/members", label: "Members", icon: IconUsers },
  { to: "/applications", label: "Applications", icon: IconUserPlus },
  { to: "/loot", label: "Loot", icon: IconDiamond },
  { to: "/auras", label: "Auras", icon: IconSparkles },
  { to: "/ops", label: "Ops", icon: IconChartBar },
  { to: "/agents", label: "Agents", icon: IconRobot },
  { to: "/config", label: "Config", icon: IconListDetails },
];

/** A Discord server's icon (or its initials): the selector's rows. Server icons are not in-game icons, so the name
 *  stands beside them (the iconography rule is about class, spec, role and aura icons). */
function ServerIcon({ g, size = 26 }: { g: Pick<GuildChoice, "name" | "icon">; size?: number }) {
  return <Avatar src={g.icon} size={size} radius="sm" color="teal" style={{ flex: "none" }}>{g.name.slice(0, 2).toUpperCase()}</Avatar>;
}

/** Switch server: the browser's selection (cookie) changes on the server, then the page reloads on the new guild —
 *  every page re-fetches, the live stream reconnects, officer-only pages send a member back to Me. */
async function switchGuild(key: string) {
  if (anyDirty()) {  // design §3.5 S-01: the switch is a full reload — never under an unsaved draft
    notifications.show({ message: "Save or discard your edits first — switching server reloads the page.", color: "yellow" });
    return;
  }
  try {
    await api.post("/api/guild", { key });
    window.location.assign(window.location.pathname);
  } catch (e) {
    notifications.show({ message: (e as Error).message || "Couldn't switch server", color: "red" });
  }
}

/** The server selector in the rail (only when this person may open more than one guild). */
function GuildSelector({ meta }: { meta: Meta }) {
  const current = meta.guilds.find((g) => g.key === meta.guild_key) || { key: meta.guild_key, name: meta.guild, icon: null, role: "member" as const };
  return (
    <Menu position="bottom-start" width={196} withinPortal>
      <Menu.Target>
        <UnstyledButton aria-label="Choose a server" px="xs" py={6} mb="sm" style={{ borderRadius: 7, border: "1px solid var(--mantine-color-slate-5)", width: "100%" }}>
          <Group gap="sm" wrap="nowrap">
            <ServerIcon g={current} />
            <Text size="sm" fw={600} truncate style={{ flex: 1, minWidth: 0 }}>{current.name}</Text>
            <IconSelector size={16} stroke={1.8} style={{ flex: "none", opacity: 0.7 }} />
          </Group>
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Servers</Menu.Label>
        {meta.guilds.map((g) => (
          <Menu.Item key={g.key} leftSection={<ServerIcon g={g} size={22} />} rightSection={g.key === meta.guild_key ? <IconCheck size={14} /> : <Text size="xs" c="dimmed">{g.role}</Text>}
            onClick={() => { if (g.key !== meta.guild_key) switchGuild(g.key); }}>
            <Text size="sm" truncate>{g.name}</Text>
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [opened, { toggle, close }] = useDisclosure();
  const mobile = useMediaQuery("(max-width: 48em)");
  const loc = useLocation();
  const [notMember, setNotMember] = useState(false);
  // a first load that failed for another reason (a 503 "Discord didn't answer", the bot restarting, no network): say
  // so and offer Try again, never sit on "Loading…" (K4)
  const [failed, setFailed] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setFailed(null);
    api.get<Meta>("/api/meta").then((m) => {
      setGuild(m.guild_key, m.guild);  // from here on every call, image and live stream names this guild
      setMeta(m);
      // never change server without saying so: a selection this person lost, a tab that lost its server, a link to a
      // server they aren't in (K16)
      const prev = takePrevGuild();
      const linked = new URLSearchParams(window.location.search).get("g");
      const note = m.moved ? `You're not in ${m.moved} any more; showing ${m.guild}.`
        : prev && prev.key !== m.guild_key ? `You no longer have access to ${prev.name}; showing ${m.guild}.`
        : linked && linked !== m.guild_key && m.guilds.length ? `That link is for a server you're not in; showing ${m.guild}.` : null;
      if (note) notifications.show({ message: note, color: "yellow", autoClose: 8000 });
    }).catch((e) => {
      if (e instanceof ApiError && e.status === 403) setNotMember(true);
      else if (e instanceof ApiError && e.status === 401) return;  // api() is already sending the browser to log in
      else setFailed((e instanceof ApiError && e.message) || "The site didn't answer; try again in a moment.");
    });
  }, [attempt]);
  useEffect(() => close(), [loc.pathname, close]);
  if (notMember) return <NotMember />;
  if (failed) return (
    <Stack p="xl" gap="sm" align="flex-start">
      <Text>{failed}</Text>
      <Button leftSection={<IconRefresh size={16} />} onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
    </Stack>
  );
  if (!meta) return <Box p="xl"><Text c="dimmed">Loading…</Text></Box>;

  const officer = (el: React.ReactElement) => (meta.viewer.officer ? el : <Navigate to="/me" replace />);
  const several = meta.guilds.length > 1;
  const link = (n: { to: string; label: string; icon: React.ElementType }) => (
    <NavLink key={n.to} component={Link} to={n.to} label={n.label} leftSection={<n.icon size={18} stroke={1.8} />} active={loc.pathname === n.to || loc.pathname.startsWith(n.to + "/")} variant="light" color="teal" style={{ borderRadius: 7 }} />
  );
  return (
    <AppShell navbar={{ width: 212, breakpoint: "sm", collapsed: { mobile: !opened } }} header={{ height: 52, collapsed: !mobile }} padding="lg">
      <AppShell.Header hiddenFrom="sm">
        <Group h="100%" px="md"><Burger opened={opened} onClick={toggle} size="sm" /><Text fw={700} ff="Manrope">oibot_GM</Text>{several && <Text size="sm" c="dimmed" truncate style={{ minWidth: 0 }}>{meta.guild}</Text>}</Group>
      </AppShell.Header>
      <AppShell.Navbar p="sm" style={{ background: "var(--mantine-color-slate-7)" }}>
        <Group gap="sm" px="xs" pb="md" visibleFrom="sm">
          <Box style={{ width: 30, height: 30, borderRadius: 8, background: "linear-gradient(135deg, var(--mantine-color-teal-4), #1F6F6B)" }} />
          <Box><Title order={3} size="h5">oibot_GM</Title><Text size="xs" c="dimmed">{meta.guild} · {meta.profile}</Text></Box>
        </Group>
        {several && <GuildSelector meta={meta} />}
        <Stack gap={2}>{NAV.map(link)}</Stack>
        {meta.viewer.officer && (
          <Stack gap={2} mt="md">
            <Text size="xs" c="dimmed" fw={700} tt="uppercase" px="xs" style={{ letterSpacing: ".1em" }}>Officers</Text>
            {OFFICER.map(link)}
          </Stack>
        )}
        <NavLink mt="auto" label="Refresh" leftSection={<IconRefresh size={18} stroke={1.8} />} onClick={() => window.dispatchEvent(new Event(REFRESH_EVENT))} style={{ borderRadius: 7, color: "var(--mantine-color-dimmed)" }} />
        <Group gap="sm" px="xs" pt="md" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
          <Box style={{ width: 30, height: 30, borderRadius: "50%", background: "linear-gradient(135deg, #C79C6E, #6F4E2A)" }} />
          <Box style={{ minWidth: 0 }}><Text size="sm" truncate>{meta.viewer.name}</Text><Text size="xs" c="dimmed">{meta.viewer.owner ? "owner" : meta.viewer.officer ? "officer" : "member"} · <a href="/auth/logout" style={{ color: "inherit" }}>log out</a></Text></Box>
        </Group>
      </AppShell.Navbar>
      <AppShell.Main style={{ background: "var(--mantine-color-slate-8)" }}>
        <Box maw={1180} mx="auto">
          <Routes>
            <Route path="/me" element={<MePage meta={meta} />} />
            <Route path="/rosters" element={officer(<RostersPage meta={meta} />)} />
            <Route path="/raids" element={officer(<RaidsPage />)} />
            <Route path="/members" element={officer(<MembersPage meta={meta} />)} />
            <Route path="/applications" element={officer(<ApplicationsPage meta={meta} />)} />
            <Route path="/bank" element={<Navigate to="/members" replace />} />
            <Route path="/admin" element={<Navigate to="/members" replace />} />
            <Route path="/auras" element={officer(<AurasPage meta={meta} />)} />
            <Route path="/ops" element={officer(<OpsPage />)} />
            <Route path="/agents" element={officer(<AgentsPage />)} />
            <Route path="/loot" element={officer(<LootPage meta={meta} />)} />
            <Route path="/loot-log" element={<LootLogPage meta={meta} />} />
            <Route path="/config" element={officer(<ConfigPage meta={meta} />)} />
            <Route path="*" element={<Navigate to="/me" replace />} />
          </Routes>
        </Box>
      </AppShell.Main>
    </AppShell>
  );
}
