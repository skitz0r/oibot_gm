import { useEffect, useRef, useState } from "react";
import { useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";
import { Badge, Box, Button, Card, Group, MultiSelect, Progress, Select, Table, Text, Tooltip } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { api, type RoleRef } from "../api";
import { CardHeader, fail } from "./Page";
import { useOpsSave } from "./ReviewModal";
import css from "./plain.module.css";

// ---- shapes (mirror plain_json in src/oibot_gm/web/api.py)
interface PlainGroup { id: string; label: string; example: string; who: string[]; default: string[]; who_text: string; ops: string[] }
interface PlainChannel { id: string; name: string; category: string | null; mode: string; listed: boolean; role: "ops" | "analytics" | null }
/** One listening verdict (bot.listen_recent, design §5.59): `when` is already 12-hour words from the server. */
interface ListenRow { when: string; channel: string; channel_name: string; author: string; text: string; p: number | null; mode: string; did: string }
export interface PlainPermissionsData {
  groups: PlainGroup[]; channels: PlainChannel[]; listed: Record<string, string>; dm: string; default: string;
  tiers: string[]; modes: string[]; guild_roles: RoleRef[]; owner: boolean; rev?: string;
  listen?: Record<string, string>; listen_modes?: string[]; listen_words?: Record<string, string>; listen_ready?: boolean; listen_recent?: ListenRow[];
}

const TIER: Record<string, string> = { everyone: "Everyone", registered: "Registered members", officers: "Officers", owner: "Owner only" };
const MODE: Record<string, string> = { act: "Acts", self: "Own record only", answer: "Answers only", ignore: "Ignored" };
const MODE_COLOUR: Record<string, string> = { act: "teal", self: "blue", answer: "gray", ignore: "red" };
const MODE_HINT = "Acts: anything the person's groups allow · Own record only: their absences, characters, DMs and answers · Answers only: questions, no changes · Ignored: the bot doesn't answer @mentions";

const LISTEN_HINT = "Listening: the bot reads these channels and answers what is clearly for it, without a tag. Watches only notes what it would have answered.";
const LISTEN_COLOUR: Record<string, string> = { watch: "gray", reply: "teal" };
/** What the bot did with a message it read, in words (discord_bot.ListenMixin's `did`). */
const DID: Record<string, string> = {
  replied: "Replied", asked: "Asked with 👀", quiet: "Stayed quiet",
  "would reply": "Would have replied", "would ask": "Would have asked", skipped: "Skipped",
};
const DID_WHY: Record<string, string> = { skipped: "over the hourly limit of decisions for this server" };

type Draft = { who: Record<string, string[]>; listed: Record<string, string>; dm: string; default: string; listen: Record<string, string> };

/** Who may act through plain text (an @mention, a DM, /gm change, the site's box), and where. Officers read; the owner edits. */
export function PlainPermissions() {
  const [data, setData] = useState<PlainPermissionsData | null>(null);
  const [d, setD] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveOps, reviewDialog] = useOpsSave();
  useDirty("plain-permissions", d !== null);
  const load = () => api.get<PlainPermissionsData>("/api/plain-permissions").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  const editingRef = useRef(false);
  editingRef.current = d !== null;
  useLive(() => { if (!editingRef.current) load(); }, ["config"]);  // a change made elsewhere shows — never under the grid's edit mode
  // a new listening verdict: only Recent decisions refresh, so an open edit keeps its draft
  useLive(() => {
    api.get<PlainPermissionsData>("/api/plain-permissions")
      .then((n) => setData((o) => (o ? { ...o, listen_recent: n.listen_recent, listen_ready: n.listen_ready } : n))).catch(fail);
  }, ["listen"]);
  if (!data) return null;
  const editing = d !== null;
  const roleName = (id: string) => data.guild_roles.find((r) => r.id === id)?.name;
  const whoLabel = (t: string) => (t.startsWith("role:") ? `@${roleName(t.slice(5)) || `role ${t.slice(5)}`}` : TIER[t] || t);
  const whoOptions = [...data.tiers.map((t) => ({ value: t, label: TIER[t] || t })), ...data.guild_roles.map((r) => ({ value: `role:${r.id}`, label: `@${r.name}` }))];
  const known = new Set(whoOptions.map((o) => o.value));
  const modeOptions = data.modes.map((m) => ({ value: m, label: MODE[m] || m }));
  const listenMap = data.listen || {};
  const listenWords = data.listen_words || { watch: "Watches", reply: "Replies" };
  const start = () => setD({ who: Object.fromEntries(data.groups.map((g) => [g.id, g.who])), listed: { ...data.listed }, dm: data.dm, default: data.default, listen: { ...listenMap } });

  /** One structured change on the ops path: a cut that locks people out (plain text off in the ops channel or in DMs) is
   *  warn-class, so the Review shows the server's own consequence sentence (configops.op_consequences); a stale grid is a 409. */
  async function save() {
    if (!d || !data) return;
    setBusy(true);
    try {
      const r = await saveOps([{ kind: "plain", fields: { groups: d.who, channels: d.listed, dm: d.dm, default: d.default, listen: d.listen } }],
        { revs: data.rev ? { "config:plain": data.rev } : undefined, title: "Review the plain-text permissions" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) { fail(new Error("Someone else changed the plain-text permissions since you opened them — reload to see their change, your edits are kept.")); return; }
      setData(await api.get<PlainPermissionsData>("/api/plain-permissions"));
      if (!(r.results || []).some((x) => !x.ok)) setD(null);
    } catch (e) { fail(e); } finally { setBusy(false); }
  }

  const modeBadge = (m: string) => <Badge variant="light" color={MODE_COLOUR[m] || "gray"}>{MODE[m] || m}</Badge>;
  const implicit = (c: PlainChannel) => (c.role ? "act" : (d?.default ?? data.default));
  const channelSelect = (c: PlainChannel) => (
    <Select size="xs" w={200} value={d!.listed[c.id] ?? "default"} allowDeselect={false}
      data={[{ value: "default", label: `Default · ${MODE[implicit(c)]}` }, ...modeOptions]}
      onChange={(v) => {
        const listed = { ...d!.listed }; if (v && v !== "default") listed[c.id] = v; else delete listed[c.id];
        const listen = { ...d!.listen }; if ((listed[c.id] ?? implicit(c)) === "ignore") delete listen[c.id];  // an ignored channel can't listen
        setD({ ...d!, listed, listen });
      }} />
  );
  const listenBadge = (c: PlainChannel) => (listenMap[c.id] ? <Badge variant="light" color={LISTEN_COLOUR[listenMap[c.id]] || "gray"}>{listenWords[listenMap[c.id]] || listenMap[c.id]}</Badge> : null);
  const listenSelect = (c: PlainChannel) => {
    const ignored = (d!.listed[c.id] ?? implicit(c)) === "ignore";
    return (
      <Select size="xs" w={130} value={d!.listen[c.id] ?? "off"} allowDeselect={false} disabled={ignored}
        title={ignored ? "The bot ignores this channel, so it can't listen here." : undefined}
        data={[{ value: "off", label: "Off" }, ...(data.listen_modes || ["watch", "reply"]).map((m) => ({ value: m, label: listenWords[m] || m }))]}
        onChange={(v) => { const listen = { ...d!.listen }; if (v && v !== "off") listen[c.id] = v; else delete listen[c.id]; setD({ ...d!, listen }); }} />
    );
  };
  const anyListening = Object.keys(editing ? d!.listen : listenMap).length > 0;
  const recent = data.listen_recent || [];

  return (
    <Card id="plain">
      {reviewDialog}
      <CardHeader title="Plain-text permissions" hint="what a sentence to the bot may change, for whom, and where"
        action={data.owner && !editing ? <Button variant="default" size="sm" leftSection={<IconPencil size={15} />} onClick={start}>Edit permissions</Button> : null} />
      <Box p="md">
        <Text size="sm" c="dimmed" mb="sm">
          Applies to @mentions, DMs, <Text span ff="monospace" size="sm">/gm change</Text> and MCP <Text span ff="monospace" size="sm">plain_change</Text> (which counts as a place where plain text acts). The bot always shows the change first and waits for Apply; the owner can always do everything. Only the owner changes these permissions, in chat too. Pinning someone in or out of a run is a <b>Runs</b> change (it decides who the lock rosters), not Comp &amp; policy.
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
        <Table.ScrollContainer minWidth={560}>
          <Table verticalSpacing={6}>
            <Table.Thead><Table.Tr><Table.Th>Where</Table.Th><Table.Th w={220}>Plain text</Table.Th><Table.Th w={140}>Listening</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              <Table.Tr>
                <Table.Td><Text size="sm" fw={600}>DMs to the bot</Text></Table.Td>
                <Table.Td>{editing ? <Select size="xs" w={200} data={modeOptions} value={d!.dm} allowDeselect={false} onChange={(v) => v && setD({ ...d!, dm: v })} /> : modeBadge(data.dm)}</Table.Td>
                <Table.Td />
              </Table.Tr>
              <Table.Tr>
                <Table.Td><Text size="sm" fw={600}>Any other channel</Text><Text size="xs" c="dimmed">channels not set below</Text></Table.Td>
                <Table.Td>{editing ? <Select size="xs" w={200} data={modeOptions} value={d!.default} allowDeselect={false} onChange={(v) => v && setD({ ...d!, default: v })} /> : modeBadge(data.default)}</Table.Td>
                <Table.Td />
              </Table.Tr>
              {data.channels.map((c) => (
                <Table.Tr key={c.id}>
                  <Table.Td>
                    <Text size="sm">#{c.name}{c.role && <Text span size="xs" c="dimmed"> · the {c.role} channel</Text>}</Text>
                    {c.category && <Text size="xs" c="dimmed">{c.category}</Text>}
                  </Table.Td>
                  <Table.Td>{editing ? channelSelect(c) : (c.listed ? modeBadge(c.mode) : <Group gap={6}>{modeBadge(c.mode)}<Text size="xs" c="dimmed">default</Text></Group>)}</Table.Td>
                  <Table.Td>{editing ? listenSelect(c) : listenBadge(c)}</Table.Td>
                </Table.Tr>
              ))}
              {data.channels.length === 0 && <Table.Tr><Table.Td colSpan={3}><Text size="xs" c="dimmed">The bot can't see the server's channels right now.</Text></Table.Td></Table.Tr>}
              <Table.Tr style={{ opacity: 0.55 }}>
                <Table.Td><Text size="sm" fw={600}>The site and MCP</Text><Text size="xs" c="dimmed">the Ops page's box and MCP plain_change; what they may do is the person's own rights</Text></Table.Td>
                <Table.Td><Group gap={6}>{modeBadge("act")}<Text size="xs" c="dimmed">not configurable</Text></Group></Table.Td>
                <Table.Td />
              </Table.Tr>
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Text size="xs" c="dimmed" mt={6}>{LISTEN_HINT}</Text>
        {anyListening && data.listen_ready === false && <Text size="xs" c="dimmed" style={{ opacity: 0.7 }}>Listening needs the host's TypeSafe key.</Text>}
        {editing && (
          <Group gap="sm" justify="flex-end" mt="md">
            <Button variant="default" size="sm" onClick={() => setD(null)}>Cancel</Button>
            <Button size="sm" loading={busy} onClick={save}>Save permissions</Button>
          </Group>
        )}
      </Box>
      {(anyListening || recent.length > 0) && (
        <Box pb="md">
          <Text fw={600} size="sm" px="md" pt="sm">Recent decisions</Text>
          <Text size="xs" c="dimmed" px="md" mb="xs">The last messages the bot read in a listening channel, newest first, and what it did with each.</Text>
          {recent.length === 0 ? (
            <Text size="sm" c="dimmed" px="md">No messages read yet. Each untagged message in a listening channel shows here with what the bot did.</Text>
          ) : (
            <div>
              <div className={css.head}>
                <Text size="xs" c="dimmed">When</Text><Text size="xs" c="dimmed">Channel</Text><Text size="xs" c="dimmed">Who wrote</Text>
                <Text size="xs" c="dimmed">Message</Text><Text size="xs" c="dimmed">Likely for the bot</Text><Text size="xs" c="dimmed">What the bot did</Text>
              </div>
              {recent.map((r, i) => (
                <div key={`${r.when}-${i}`} className={css.row}>
                  <div className={css.meta}>
                    <Text size="xs" c="dimmed">{r.when}</Text>
                    <Text size="xs">{r.channel_name ? `#${r.channel_name}` : "a channel the bot can't see"}</Text>
                    <Text size="xs" fw={600}>{r.author}</Text>
                  </div>
                  <Text size="sm" className={css.text}>{r.text}</Text>
                  <div className={css.bar}>
                    {r.p === null ? <Text size="xs" c="dimmed">—</Text> : <>
                      <Progress value={Math.round(r.p * 100)} size="sm" color={r.p >= 0.8 ? "teal" : r.p >= 0.5 ? "blue" : "gray"} aria-label={`${Math.round(r.p * 100)}% likely for the bot`} />
                      <Text size="xs" w={34} ta="right">{Math.round(r.p * 100)}%</Text>
                    </>}
                  </div>
                  <div className={css.did}>
                    {DID_WHY[r.did]
                      ? <Tooltip label={DID_WHY[r.did]}><Text size="sm" span>{DID[r.did] || r.did}</Text></Tooltip>
                      : <Text size="sm">{DID[r.did] || r.did}</Text>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Box>
      )}
    </Card>
  );
}
