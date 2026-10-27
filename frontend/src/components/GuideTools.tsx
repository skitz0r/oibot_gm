import { useEffect, useState } from "react";
import { Anchor, Badge, Button, Card, Group, Stack, Table, Text, TextInput, Textarea } from "@mantine/core";
import { api, guildQuery, type GuideStatus, type Meta } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";

const EXPLORER_HEIGHT = "min(78vh, 900px)";

/** Me › Leveling guide (design §5.55): paste the RestedXP import string issued to you and the BattleTag it was issued to,
 *  and the bot unlocks it for you alone — a companion addon, a one-file route explorer you can keep offline, and the guides
 *  as readable text. The explorer also runs here, in a sandboxed frame. Nothing is stored but the files; the string and the
 *  BattleTag are read once and dropped. Every state renders from the server's status; a build disables the form until it
 *  answers. The text / BattleTag boxes are a form, not an edit mode of guild data: nothing here touches the guild. */
export function GuideTools({ meta }: { meta: Meta }) {
  const [st, setSt] = useState<GuideStatus | null>(null);
  const [text, setText] = useState("");
  const [tag, setTag] = useState("");
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<GuideStatus>("/api/me/guide").then(setSt).catch(fail);
  useEffect(() => { load(); }, []);

  async function build() {
    setBusy(true);
    try {
      const r = await api.post<GuideStatus & { message: string }>("/api/me/guide", { text, battletag: tag.trim() });
      ok(r.message);
      setSt(r);
      setText("");
      setTag("");
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function clear() {
    if (!(await ask({ title: "Remove your guide files?", message: "The addon, the explorer and the text built from your import string are deleted from the site. Paste the string again to rebuild them.", confirmLabel: "Remove", color: "red" }))) return;
    try { const r = await api.post<GuideStatus & { message: string }>("/api/me/guide/clear"); ok(r.message); setSt(r); } catch (e) { fail(e); }
  }

  if (!st) return <Text c="dimmed">Loading…</Text>;
  if (!st.available) {
    return <Card><CardHeader title="Leveling guide" /><Text p="md" size="sm" c="dimmed">There is no leveling-guide tool for {meta.game} yet: it needs the game's zone tables, which only WoW: Forever has so far.</Text></Card>;
  }
  const q = guildQuery();
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title="Your RestedXP guide" hint="unlocked for you alone — nobody else sees it" />
        <Stack p="md" gap="sm">
          <Text size="sm" c="dimmed">
            RestedXP locks a bought guide to the Battle.net account it was issued to. Paste the <b>import string</b> the addon's Export button gives you
            (it starts with a number and a bar, like <code>78|…</code>) and that account's <b>BattleTag</b>, which is the key. You get the guide back as
            an addon that loads it on any of your characters, a route explorer to browse offline, and readable text. {st.starter_pack
              ? "The free 1–20 starter guides are added from the host's copy of the addon, so the explorer covers 1–60."
              : "The explorer covers the levels you bought; the free 1–20 starter guides are in the addon itself."}
          </Text>
          <Textarea label="Import string" placeholder="78|…" autosize minRows={3} maxRows={6} value={text} onChange={(e) => setText(e.currentTarget.value)} disabled={busy} styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)", fontSize: 12 } }} />
          <Group align="flex-end" gap="sm" wrap="wrap">
            <TextInput label="BattleTag" description="the account the guide was issued to" placeholder="Name#1234" value={tag} onChange={(e) => setTag(e.currentTarget.value)} disabled={busy} style={{ flex: "1 1 220px", maxWidth: 320 }} />
            <Button onClick={build} loading={busy} disabled={!text.trim() || !tag.trim()}>{st.built ? "Build it again" : "Build my guide"}</Button>
          </Group>
          <Text size="xs" c="dimmed">The string and the BattleTag are read once to unlock the guide and not kept; only the files below stay, in your own folder on the site.</Text>
        </Stack>
      </Card>

      {!st.built ? (
        <Card><Text p="md" size="sm" c="dimmed">Nothing is built yet: paste your import string and BattleTag above and press <b>Build my guide</b>.</Text></Card>
      ) : (
        <>
          <Card>
            <CardHeader title="What you got" hint={`${st.guides} guide${st.guides === 1 ? "" : "s"} · ${st.steps} steps · levels ${st.range} · built ${st.built_when}`}
              action={<Button size="xs" variant="subtle" color="red" onClick={clear}>Remove my files</Button>} />
            <Stack p="md" gap="sm">
              <Group gap="sm" wrap="wrap">
                {st.files.map((f) => (
                  <Button key={f.name} component="a" href={`/api/me/guide/file/${encodeURIComponent(f.name)}${q}`} download={f.name} variant="default" size="sm" title={f.words}>
                    {f.name}{st.sizes?.[f.name] ? ` · ${Math.max(1, Math.round(st.sizes[f.name] / 1024))} KB` : ""}
                  </Button>
                ))}
              </Group>
              <Stack gap={2}>
                {st.files.map((f) => <Text key={f.name} size="xs" c="dimmed"><b>{f.name}</b> — {f.words}</Text>)}
              </Stack>
              <Table withTableBorder={false} verticalSpacing={4} fz="sm" layout="auto" style={{ tableLayout: "auto" }}>
                <Table.Thead>
                  <Table.Tr><Table.Th>Guide group</Table.Th><Table.Th>Levels</Table.Th><Table.Th>Guides</Table.Th><Table.Th>Steps</Table.Th><Table.Th>Zones in order</Table.Th><Table.Th>Dungeons</Table.Th><Table.Th>Walking</Table.Th></Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {(st.groups || []).map((g) => (
                    <Table.Tr key={g.group}>
                      <Table.Td><Group gap={6} wrap="nowrap"><Text size="sm" fw={600}>{g.group}</Text>{g.starter && <Badge size="xs" variant="light" color="gray" tt="none">free starter</Badge>}</Group></Table.Td>
                      <Table.Td style={{ whiteSpace: "nowrap" }}>{g.lo}–{g.hi}</Table.Td>
                      <Table.Td>{g.guides}</Table.Td>
                      <Table.Td>{g.steps}</Table.Td>
                      <Table.Td><Text size="xs">{g.zones.join(" → ")}</Text></Table.Td>
                      <Table.Td><Text size="xs">{g.dungeons.length ? g.dungeons.join(", ") : "no dungeon on this route"}</Text></Table.Td>
                      <Table.Td style={{ whiteSpace: "nowrap" }}>{g.km} km</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Stack>
          </Card>
          <Card>
            <CardHeader title="Route explorer" hint="pick your character on the left; the map and the steps follow"
              action={<Anchor href={`/api/me/guide/explorer${q}`} target="_blank" rel="noopener" size="sm">Open in its own tab</Anchor>} />
            <iframe title="Route explorer" src={`/api/me/guide/explorer${q}`} sandbox="allow-scripts allow-downloads" style={{ display: "block", width: "100%", height: EXPLORER_HEIGHT, border: 0, background: "#12171f" }} />
          </Card>
        </>
      )}
      {confirmDialog}
    </Stack>
  );
}
