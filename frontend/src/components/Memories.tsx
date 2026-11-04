import { useEffect, useState } from "react";
import { Badge, Box, Button, Card, Group, Select, Text, TextInput } from "@mantine/core";
import { api, type MemoryRow } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { useLive } from "../hooks/useLive";
import type { PlainPermissionsData } from "./PlainPermissions";
import css from "./plain.module.css";

interface Page { rows: MemoryRow[]; total: number; more: boolean; channels: Record<string, string>; words: Record<string, string>; ready: boolean; model: string }
const COLOUR: Record<string, string> = { shared: "teal", channel: "blue" };
const PAGE = 50;

/** The Ops page's Memory tab (design §5.61): which channels remember, then everything the bot remembers members saying —
 *  newest first, searchable, a channel filter, Forget on each row (the server's sentence first). Officers only. The
 *  channels' names come from /api/plain-permissions (a remembering channel with no memory yet has no row to name it). */
export function Memories() {
  const [data, setData] = useState<Page | null>(null);
  const [chanNames, setChanNames] = useState<Record<string, string>>({});
  const [q, setQ] = useState("");
  const [channel, setChannel] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);
  const [ask, confirmDialog] = useConfirm();
  const params = () => `?q=${encodeURIComponent(q)}&channel=${channel || ""}&limit=${limit}`;
  const load = () => {
    api.get<Page>("/api/memories" + params()).then(setData).catch(fail);
    api.get<PlainPermissionsData>("/api/plain-permissions").then((p) => setChanNames(Object.fromEntries(p.channels.map((c) => [c.id, c.name])))).catch(() => {});
  };
  useEffect(() => { load(); }, [q, channel, limit]);  // eslint-disable-line react-hooks/exhaustive-deps
  useLive(load, ["memory", "config"]);
  if (!data) return null;
  const names: Record<string, string> = { ...Object.fromEntries(data.rows.map((r) => [r.channel, r.channel_name])), ...chanNames };
  const remembering = Object.entries(data.channels).map(([id, mode]) => ({ id, mode, name: names[id] }));
  const fetching = data.model === "loading" || data.model === "failed";

  async function forget(r: MemoryRow) {
    try {
      const p = await api.post<{ sentences: string[] }>(`/api/memories/${r.id}/forget`, { preview: true });
      if (!(await ask({ title: "Forget this?", message: p.sentences.join(" "), confirmLabel: "Forget", color: "red" }))) return;
      const x = await api.post<{ message: string }>(`/api/memories/${r.id}/forget`, {});
      ok(x.message); load();
    } catch (e) { fail(e); }
  }

  return (
    <Card id="memory">
      {confirmDialog}
      <CardHeader title="Memory" hint="what the bot remembers members saying, and where it remembers" />
      <Box p="md" pt="sm">
        {remembering.length === 0 ? (
          <Text size="sm" c="dimmed">The bot remembers nothing yet. On the Plain-text permissions tab, the owner sets a listening channel's Memory to Remembers.</Text>
        ) : (
          <Group gap={6} align="center">
            <Text size="sm" c="dimmed">Remembering in</Text>
            {remembering.map((l) => <Badge key={l.id} variant="light" color={COLOUR[l.mode] || "gray"}>#{l.name || "a channel the bot can't see"} · {data.words[l.mode] || l.mode}</Badge>)}
          </Group>
        )}
        {remembering.length > 0 && !data.ready && <Text size="xs" c="dimmed" mt={4}>Memory needs the host's TypeSafe key.</Text>}
        {remembering.length > 0 && fetching && <Text size="xs" c="dimmed" mt={4}>The bot is still fetching its memory model, so for now it finds memories by their words only.</Text>}
      </Box>
      <Box px="md" pb="sm">
        <Group gap="sm">
          <TextInput size="xs" placeholder="Search the words" value={q} onChange={(e) => { setQ(e.currentTarget.value); setLimit(PAGE); }} w={240} />
          <Select size="xs" placeholder="Every channel" clearable value={channel} onChange={(v) => { setChannel(v); setLimit(PAGE); }} w={200}
            data={Object.keys(data.channels).map((id) => ({ value: id, label: names[id] ? `#${names[id]}` : "a channel the bot can't see" }))} />
          <Text size="xs" c="dimmed">{data.total === 1 ? "1 memory" : `${data.total} memories`}</Text>
        </Group>
      </Box>
      <Box pb="md">
        {data.rows.length === 0 ? (
          <Text size="sm" c="dimmed" px="md">{q || channel ? "No memory matches that. Try other words, or clear the filters." : "Nothing remembered yet. What members say in a remembering channel that others could ask about later shows here."}</Text>
        ) : (
          <div>
            {data.rows.map((r) => (
              <div key={r.id} className={css.row}>
                <div className={css.meta}>
                  <Text size="xs" c="dimmed">{r.when}</Text>
                  <Text size="xs">#{r.channel_name}</Text>
                  <Text size="xs" fw={600}>{r.author}</Text>
                </div>
                <Text size="sm" className={css.text}>{r.text}{r.context && <Text span size="xs" c="dimmed"> · replying to {r.context}</Text>}</Text>
                <div className={css.bar}>{r.scope === "channel" && <Badge size="xs" variant="light" color="blue">{r.scope_word}</Badge>}</div>
                <div className={css.did}><Button size="xs" variant="subtle" color="red" onClick={() => forget(r)}>Forget</Button></div>
              </div>
            ))}
            {data.more && <Group justify="center" mt="sm"><Button size="xs" variant="default" onClick={() => setLimit(limit + PAGE)}>Show more</Button></Group>}
          </div>
        )}
      </Box>
    </Card>
  );
}
