import { useEffect, useState } from "react";
import { Alert, Anchor, Badge, Button, Card, Code, Group, Modal, Select, Stack, Table, Text, Tooltip } from "@mantine/core";
import { api } from "../api";
import { useLive } from "../hooks/useLive";
import { useDirty } from "../hooks/useDirty";
import type { ChannelsData, WebhookReply } from "../setupApi";
import { CardHeader, fail, ok } from "./Page";
import { useOpsSave } from "./ReviewModal";

/** Config › Channels (design §3.5 O-12): which channel each function of the bot uses. Read mode lists them with the
 *  `missing` badge (deleted, or the permissions the bot lacks there — the server's perms.missing_for, the same test the
 *  bot runs before posting). The owner's Edit opens an edit mode (one Select per channel, '— not set —' to stop using
 *  one); Save sends ONE structured batch through useOpsSave → the ReviewModal (channel changes always ask: what is posted,
 *  pinned, forgotten or falls back, from code) → the result sheet. While Discord is unreachable editing is off and the page
 *  says so (never "channel not found"). */
export function ChannelsCard() {
  const [data, setData] = useState<ChannelsData | null>(null);
  const [draft, setDraft] = useState<Record<string, string | null> | null>(null);
  const [busy, setBusy] = useState(false);
  const [hook, setHook] = useState<WebhookReply | null>(null);
  const [save, review] = useOpsSave();
  const load = () => api.get<ChannelsData>("/api/admin/channels").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(() => { if (draft === null) load(); }, ["config"]);
  const changed = data && draft ? data.channels.filter((c) => (draft[c.kind] ?? null) !== (c.id ?? null)) : [];
  useDirty("channels", changed.length > 0);
  if (!data) return <Card><CardHeader title="Channels" /><Text c="dimmed" p="md" size="sm">Loading…</Text></Card>;

  const options = [{ value: "", label: "— not set —" }, ...data.guild_channels.map((c) => ({ value: c.id, label: `#${c.name}${c.category ? ` · ${c.category}` : ""}` }))];
  const editing = draft !== null;
  const start = () => setDraft(Object.fromEntries(data.channels.map((c) => [c.kind, c.id])));
  async function onSave() {
    if (!draft || !data) return;
    setBusy(true);
    try {
      const fields = Object.fromEntries(changed.map((c) => [`channel:${c.kind}`, draft[c.kind] || null]));
      const r = await save([{ kind: "settings", fields }], { revs: { "config:settings": data.rev }, title: "Review channel changes" });
      if (r === null) return;  // Back to edits: the draft stays
      if (r.changed?.length) { fail(new Error("Someone changed the settings since you opened the page — reload and check your edits.")); return; }
      const fresh = await api.get<ChannelsData>("/api/admin/channels");
      setData(fresh);
      // refused fields stay drafted (the result sheet said why); everything that landed leaves the draft
      const left = Object.fromEntries(fresh.channels.map((c) => [c.kind, (draft[c.kind] ?? null) !== (c.id ?? null) ? draft[c.kind] : c.id]));
      setDraft(fresh.channels.some((c) => (left[c.kind] ?? null) !== (c.id ?? null)) ? left : null);
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function webhook() {
    try { setHook(await api.post<WebhookReply>("/api/admin/news-webhook", {})); } catch (e) { fail(e); }
  }

  return (
    <Card>
      <CardHeader title="Channels" hint="which channel each function of the bot uses"
        action={data.owner && (editing
          ? <Group gap="xs"><Button size="xs" variant="default" onClick={() => setDraft(null)} disabled={busy}>Discard</Button><Button size="xs" onClick={onSave} loading={busy} disabled={!changed.length || !data.ready}>Save {changed.length || ""}</Button></Group>
          : <Button size="xs" variant="default" onClick={start} disabled={!data.ready}>Edit</Button>)} />
      {!data.ready && <Alert color="yellow" variant="light" mx="md" mt="sm" py="xs">{data.unreachable}</Alert>}
      <Table.ScrollContainer minWidth={520}>
        <Table verticalSpacing="xs">
          <Table.Tbody>
            {data.channels.map((c) => (
              <Table.Tr key={c.kind}>
                <Table.Td w={210}><Text size="sm" fw={600}>{c.label}</Text><Text size="xs" c="dimmed">{c.hint}</Text></Table.Td>
                <Table.Td>
                  {editing ? (
                    <Select size="xs" data={options} value={draft![c.kind] || ""} searchable maw={320} aria-label={`${c.label} channel`}
                      onChange={(v) => setDraft({ ...draft!, [c.kind]: v || null })} disabled={busy || !data.ready} />
                  ) : (
                    <Group gap="xs" wrap="wrap">
                      {c.id ? (data.links.channels?.[c.kind] ? <Anchor size="sm" href={data.links.channels[c.kind]} target="_blank" rel="noreferrer">{c.name ? `#${c.name}` : "a channel"}</Anchor> : <Text size="sm">{c.name ? `#${c.name}` : "a channel"}</Text>)
                        : <Text size="sm" c="dimmed">{data.owner ? "No channel yet — pick one with Edit." : "No channel yet — the owner picks one."}</Text>}
                      {c.problem && <Tooltip label={c.problem} multiline w={300}><Badge color="red" variant="light">{c.missing.length ? `missing: ${c.missing.join(", ")}` : "deleted"}</Badge></Tooltip>}
                      {c.kind === "news" && c.id && data.owner && <Button size="compact-xs" variant="subtle" onClick={webhook}>Webhook URL</Button>}
                    </Group>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {editing && changed.length > 0 && <Text size="xs" c="dimmed" px="md" pb="sm">Not saved yet — Save shows what each change does (cards posted, pinned or forgotten) before anything is written.</Text>}
      {review}
      <Modal opened={hook !== null} onClose={() => setHook(null)} title="The news webhook" centered>
        <Stack gap="sm">
          {hook?.url ? <><Text size="sm">Point your news source at this webhook (keep it private — anyone with it can post there):</Text><Code block style={{ wordBreak: "break-all" }}>{hook.url}</Code>
            <Button size="xs" variant="default" onClick={() => { navigator.clipboard?.writeText(hook.url!); ok("Copied"); }}>Copy</Button></> : null}
          <Text size="sm" c="dimmed">{hook?.note}</Text>
        </Stack>
      </Modal>
    </Card>
  );
}
