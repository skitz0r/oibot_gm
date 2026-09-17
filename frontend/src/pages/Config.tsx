import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Code, Group, MultiSelect, Select, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { api } from "../api";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";

interface ConfigData {
  yaml: string; docs: Record<string, { text: string; compiled: boolean; summary: string | null }>;
  channels: Record<string, { id: string | null; name: string | null }>; guild_channels: { id: string; name: string; category: string | null }[]; guild_roles: string[];
  settings: { timezone: string; ask_audience: string; about: string; officer_roles: string[]; owner_id: string | null };
  test_bench: { members: number; runs: string[] }; owner: boolean;
}

const CHANNELS: { kind: string; label: string; hint: string }[] = [
  { kind: "registration", label: "Registration", hint: "public, read-only: the registration card with its buttons" },
  { kind: "signup", label: "Signups", hint: "public: one sheet per run (Join / Bench / No thanks)" },
  { kind: "absences", label: "Absences", hint: "public: the I'll be away card; one line per absence" },
  { kind: "roster", label: "Roster (officers)", hint: "health cards, lock cards, fill progress, puppet DMs during tests" },
  { kind: "analytics", label: "Analytics (officers)", hint: "character bank, readiness, groups and desired comp cards + change log" },
  { kind: "applications", label: "Applications (officers)", hint: "review cards for /apply (defaults to ops)" },
  { kind: "ops", label: "Ops (officers)", hint: "one line per action the bot takes; plain-text config by @mention" },
];

export function ConfigPage() {
  const [data, setData] = useState<ConfigData | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<ConfigData>("/api/config").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const ro = !data.owner;
  async function set(field: string, value: unknown) {
    setBusy(field);
    try { const r = await api.post<{ message: string }>("/api/admin/config", { field, value }); ok(r.message); await load(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  const channelOptions = [{ value: "", label: "— not set —" }, ...data.guild_channels.map((c) => ({ value: c.id, label: `#${c.name}${c.category ? ` · ${c.category}` : ""}` }))];
  return (
    <Stack gap="lg">
      <PageTitle title="Configuration" intro={ro ? "Read-only for officers; the owner edits here, with /gm config, or in plain text via /gm change or an @mention in the ops channel." : "Each function of the bot lives in a channel. Changing a channel does what the slash command does: the registration and absence cards are posted, the analytics cards re-post."} />

      <Card>
        <CardHeader title="Channels" hint="which channel each function of the bot uses" />
        <Stack gap="sm" p="md">
          {CHANNELS.map(({ kind, label, hint }) => (
            <Group key={kind} gap="md" wrap="wrap" align="flex-end">
              <Select label={label} description={hint} data={channelOptions} value={data.channels[kind]?.id || ""} onChange={(v) => v !== null && v !== (data.channels[kind]?.id || "") && set(`channel:${kind}`, v || null)} disabled={ro || busy === `channel:${kind}`} searchable w={360} />
              {data.channels[kind]?.id && !data.channels[kind]?.name && <Badge color="red" variant="light">channel not found</Badge>}
            </Group>
          ))}
        </Stack>
      </Card>

      <Card>
        <CardHeader title="Settings" />
        <Stack gap="sm" p="md">
          <MultiSelect label="Officer roles" description="Discord roles that count as officer (Manage Server always does)" data={data.guild_roles} value={data.settings.officer_roles} onChange={(v) => set("officer_roles", v)} disabled={ro || busy === "officer_roles"} searchable w={420} />
          <Group gap="md" wrap="wrap" align="flex-end">
            <TextInput label="Timezone" description="IANA name; every schedule and clock on the site" defaultValue={data.settings.timezone} disabled={ro} w={260} onBlur={(e) => e.currentTarget.value !== data.settings.timezone && set("timezone", e.currentTarget.value)} />
            <Select label="Who may ask the bot questions" description="/ask, DMs, @mentions; others get the static guide" data={["officers", "confirmed", "registered", "everyone"]} value={data.settings.ask_audience} onChange={(v) => v && v !== data.settings.ask_audience && set("ask_audience", v)} disabled={ro} w={260} />
          </Group>
          <Textarea label="About the guild" description="one paragraph shown in the static guide to unregistered visitors" defaultValue={data.settings.about} disabled={ro} autosize minRows={2} onBlur={(e) => e.currentTarget.value !== data.settings.about && set("about", e.currentTarget.value)} />
          <Text size="xs" c="dimmed">Owner: {data.settings.owner_id ? `Discord id ${data.settings.owner_id}` : "not claimed — /gm config owner"} · transfer with /gm config owner</Text>
        </Stack>
      </Card>

      <Card>
        <CardHeader title="Test bench" hint="rehearse the whole cycle in Discord with puppet members" action={data.test_bench.members > 0 ? <Badge color="yellow" variant="light">{data.test_bench.members} test members · {data.test_bench.runs.length} test run{data.test_bench.runs.length === 1 ? "" : "s"}</Badge> : <Badge color="gray" variant="outline">idle</Badge>} />
        <Box p="md">
          <Text size="sm">In Discord: <Code>/gm test seed count:20</Code>, then <Code>/gm test run raid:barrow_deeps start_in:40 lock_in:25 confirm_in:15</Code>, then <Code>/gm test answer join:16 bench:3 out:1</Code>. Sign up yourself on the sheet too. The puppets' DMs appear in the roster channel with the real buttons; officers press them on their behalf. Lock, drag the board and propose splits here. <Code>/gm test clear</Code> removes everything.</Text>
          {data.test_bench.members > 0 && <Text size="xs" c="dimmed" mt="xs">While test members exist the analytics cards don't re-post.</Text>}
        </Box>
      </Card>

      <Card>
        <CardHeader title="guild.yaml" hint="the whole configuration as stored" />
        <Box style={{ overflowX: "auto" }}><Code block p="md" style={{ background: "transparent", fontSize: 12.5 }}>{data.yaml}</Code></Box>
      </Card>
      {Object.entries(data.docs).map(([name, d]) => (
        <Card key={name}>
          <CardHeader title={`policy · ${name}`} action={d.compiled ? <Badge variant="light" color="teal">compiled</Badge> : name !== "persona" ? <Badge variant="light" color="yellow">not compiled</Badge> : null} />
          <Box style={{ overflowX: "auto" }}><Code block p="md" style={{ background: "transparent", fontSize: 12.5, whiteSpace: "pre-wrap" }}>{d.text || "(empty)"}</Code></Box>
          {d.summary && <Text size="xs" c="dimmed" px="md" pb="md">Compiled reading: {d.summary}</Text>}
        </Card>
      ))}
      {!ro && <Button variant="subtle" size="xs" style={{ alignSelf: "flex-start" }} onClick={load}>Reload</Button>}
    </Stack>
  );
}
