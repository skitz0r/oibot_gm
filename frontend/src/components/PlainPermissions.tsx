import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Group, MultiSelect, Select, Table, Text } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { api, type RoleRef } from "../api";
import { CardHeader, fail, ok } from "./Page";

// ---- shapes (mirror plain_json in src/oibot_gm/web/api.py)
interface PlainGroup { id: string; label: string; example: string; who: string[]; default: string[]; who_text: string; ops: string[] }
interface PlainChannel { id: string; name: string; category: string | null; mode: string; listed: boolean; role: "ops" | "analytics" | null }
export interface PlainPermissionsData {
  groups: PlainGroup[]; channels: PlainChannel[]; listed: Record<string, string>; dm: string; default: string;
  tiers: string[]; modes: string[]; guild_roles: RoleRef[]; owner: boolean;
}

const TIER: Record<string, string> = { everyone: "Everyone", registered: "Registered members", officers: "Officers", owner: "Owner only" };
const MODE: Record<string, string> = { act: "Acts", self: "Own record only", answer: "Answers only", ignore: "Ignored" };
const MODE_COLOUR: Record<string, string> = { act: "teal", self: "blue", answer: "gray", ignore: "red" };
const MODE_HINT = "Acts: anything the person's groups allow · Own record only: their absences, characters, DMs and answers · Answers only: questions, no changes · Ignored: the bot doesn't answer @mentions";

type Draft = { who: Record<string, string[]>; listed: Record<string, string>; dm: string; default: string };

/** Who may act through plain text (an @mention, a DM, /gm change, the site's box), and where. Officers read; the owner edits. */
export function PlainPermissions() {
  const [data, setData] = useState<PlainPermissionsData | null>(null);
  const [d, setD] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.get<PlainPermissionsData>("/api/plain-permissions").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  if (!data) return null;
  const editing = d !== null;
  const roleName = (id: string) => data.guild_roles.find((r) => r.id === id)?.name;
  const whoLabel = (t: string) => (t.startsWith("role:") ? `@${roleName(t.slice(5)) || `role ${t.slice(5)}`}` : TIER[t] || t);
  const whoOptions = [...data.tiers.map((t) => ({ value: t, label: TIER[t] || t })), ...data.guild_roles.map((r) => ({ value: `role:${r.id}`, label: `@${r.name}` }))];
  const known = new Set(whoOptions.map((o) => o.value));
  const modeOptions = data.modes.map((m) => ({ value: m, label: MODE[m] || m }));
  const start = () => setD({ who: Object.fromEntries(data.groups.map((g) => [g.id, g.who])), listed: { ...data.listed }, dm: data.dm, default: data.default });

  async function save() {
    if (!d) return;
    setBusy(true);
    try {
      const r = await api.post<{ message: string } & PlainPermissionsData>("/api/admin/plain-permissions", { groups: d.who, channels: d.listed, dm: d.dm, default: d.default });
      ok(r.message); setData(r); setD(null);
    } catch (e) { fail(e); } finally { setBusy(false); }
  }

  const modeBadge = (m: string) => <Badge variant="light" color={MODE_COLOUR[m] || "gray"}>{MODE[m] || m}</Badge>;
  const implicit = (c: PlainChannel) => (c.role ? "act" : (d?.default ?? data.default));
  const channelSelect = (c: PlainChannel) => (
    <Select size="xs" w={200} value={d!.listed[c.id] ?? "default"} allowDeselect={false}
      data={[{ value: "default", label: `Default · ${MODE[implicit(c)]}` }, ...modeOptions]}
      onChange={(v) => { const listed = { ...d!.listed }; if (v && v !== "default") listed[c.id] = v; else delete listed[c.id]; setD({ ...d!, listed }); }} />
  );

  return (
    <Card>
      <CardHeader title="Plain-text permissions" hint="what a sentence to the bot may change, for whom, and where"
        action={data.owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={start}>Edit permissions</Button> : null} />
      <Box p="md">
        <Text size="sm" c="dimmed" mb="sm">
          Applies to @mentions, DMs, <Text span ff="monospace" size="sm">/gm change</Text> and the site's plain-text box. The bot always shows the change first and waits for Apply; the owner can always do everything.
        </Text>
        <Table.ScrollContainer minWidth={560}>
          <Table verticalSpacing="xs">
            <Table.Thead><Table.Tr><Table.Th w="45%">Can change</Table.Th><Table.Th>Who</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {data.groups.map((g) => (
                <Table.Tr key={g.id}>
                  <Table.Td>
                    <Text size="sm" fw={600}>{g.label}</Text>
                    <Text size="xs" c="dimmed">e.g. “{g.example}”</Text>
                  </Table.Td>
                  <Table.Td>
                    {editing ? (
                      <MultiSelect size="xs" placeholder={d!.who[g.id].length ? undefined : "owner only"} searchable clearable
                        data={[...whoOptions, ...d!.who[g.id].filter((t) => !known.has(t)).map((t) => ({ value: t, label: `${whoLabel(t)} (deleted role)` }))]}
                        value={d!.who[g.id]} onChange={(v) => setD({ ...d!, who: { ...d!.who, [g.id]: v } })} />
                    ) : (
                      <Group gap={6}>{g.who.length ? g.who.map((t) => <Badge key={t} variant={t === "owner" ? "outline" : "light"} color={t.startsWith("role:") ? "grape" : "teal"}>{whoLabel(t)}</Badge>) : <Badge variant="outline" color="gray">Owner only</Badge>}</Group>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Text size="xs" c="dimmed" mt="lg" mb={4}>{MODE_HINT}</Text>
        <Table.ScrollContainer minWidth={480}>
          <Table verticalSpacing={6}>
            <Table.Thead><Table.Tr><Table.Th>Where</Table.Th><Table.Th w={220}>Plain text</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              <Table.Tr>
                <Table.Td><Text size="sm" fw={600}>DMs to the bot</Text></Table.Td>
                <Table.Td>{editing ? <Select size="xs" w={200} data={modeOptions} value={d!.dm} allowDeselect={false} onChange={(v) => v && setD({ ...d!, dm: v })} /> : modeBadge(data.dm)}</Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td><Text size="sm" fw={600}>Any other channel</Text><Text size="xs" c="dimmed">channels not set below</Text></Table.Td>
                <Table.Td>{editing ? <Select size="xs" w={200} data={modeOptions} value={d!.default} allowDeselect={false} onChange={(v) => v && setD({ ...d!, default: v })} /> : modeBadge(data.default)}</Table.Td>
              </Table.Tr>
              {data.channels.map((c) => (
                <Table.Tr key={c.id}>
                  <Table.Td>
                    <Text size="sm">#{c.name}{c.role && <Text span size="xs" c="dimmed"> · the {c.role} channel</Text>}</Text>
                    {c.category && <Text size="xs" c="dimmed">{c.category}</Text>}
                  </Table.Td>
                  <Table.Td>{editing ? channelSelect(c) : (c.listed ? modeBadge(c.mode) : <Group gap={6}>{modeBadge(c.mode)}<Text size="xs" c="dimmed">default</Text></Group>)}</Table.Td>
                </Table.Tr>
              ))}
              {data.channels.length === 0 && <Table.Tr><Table.Td colSpan={2}><Text size="xs" c="dimmed">The bot can't see the server's channels right now.</Text></Table.Td></Table.Tr>}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        {editing && (
          <Group gap="sm" justify="flex-end" mt="md">
            <Button variant="default" size="sm" onClick={() => setD(null)}>Cancel</Button>
            <Button size="sm" loading={busy} onClick={save}>Save permissions</Button>
          </Group>
        )}
      </Box>
    </Card>
  );
}
