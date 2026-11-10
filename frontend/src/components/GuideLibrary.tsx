import { useEffect, useRef, useState } from "react";
import { ActionIcon, Badge, Box, Button, Card, FileButton, Group, Menu, Select, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { IconDotsVertical } from "@tabler/icons-react";
import { api } from "../api";
import { CardHeader, Eyebrow, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { useDirty } from "../hooks/useDirty";
import { useLive } from "../hooks/useLive";
import css from "./guidelibrary.module.css";

/** One version of a guild guide, as GET /api/guides/library lists it (newest first). `when` is already words
 *  (Registry.local12); `withdrawn_by` / `withdrawn_when` reach officers only. */
type GuideRow = {
  id: string; faction: string; faction_words: string; when: string; by: string; guides: number; latest: boolean; withdrawn: boolean;
  withdrawn_by?: string | null; withdrawn_when?: string | null;
};
type Library = {
  available: boolean; rows: GuideRow[]; factions: { id: string; words: string }[]; can_withdraw: boolean;
  upload_sentences: Record<string, string>;
};
type Saved = Library & { message: string };

const ORDER = ["alliance", "horde", "both"];
const FILE_MAX = 5_000_000;  // a full 1–60 import string is about 1 MB; the server refuses past 4 MB anyway

/** Me › Leveling guide › Guild guides (spec 2026-11-09 §5): the guides the guild made itself, one block per faction, each
 *  upload a version. "Get it for my BattleTag" downloads the version locked to the tag typed at the top — the tag lives in
 *  this component's state for the page visit only (never browser storage, never a URL, never the console). Officers withdraw
 *  or restore from the row's ⋮ menu; the confirm prints only the server's sentences. Anyone uploads a guide below: the string
 *  is unlocked once with the BattleTag it was issued to, and only the guide texts are kept. */
export function GuideLibrary() {
  const [lib, setLib] = useState<Library | null>(null);
  const [tag, setTag] = useState("");
  const [tagAsk, setTagAsk] = useState(false);
  const [getting, setGetting] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [upTag, setUpTag] = useState("");
  const [faction, setFaction] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const tagRef = useRef<HTMLInputElement>(null);
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Library>("/api/guides/library").then(setLib).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["guides"]);
  useDirty("guild-guide-upload", text.trim() !== "");

  async function get(r: GuideRow) {
    const t = tag.trim();
    if (!t) {
      setTagAsk(true);
      tagRef.current?.focus();
      return;
    }
    setGetting(r.id);
    try {
      const name = await api.download(`/api/guides/library/${encodeURIComponent(r.id)}/download`, { battletag: t }, "RXPGuides-import.txt");
      ok(`${name} is downloading: import it with RestedXP on the account with that BattleTag.`);
    } catch (e) { fail(e); } finally { setGetting(null); }
  }

  async function officer(r: GuideRow, verb: "withdraw" | "restore") {
    const path = `/api/guides/library/${encodeURIComponent(r.id)}/${verb}`;
    setBusy(r.id);
    try {
      const p = await api.post<{ sentences: string[] }>(path, { preview: true });
      setBusy(null);
      const yes = await ask({
        title: verb === "withdraw" ? "Withdraw this version?" : "Restore this version?",
        message: p.sentences.join(" "),
        confirmLabel: verb === "withdraw" ? "Withdraw" : "Restore",
        color: verb === "withdraw" ? "red" : undefined,
      });
      if (!yes) return;
      setBusy(r.id);
      const s = await api.post<Saved>(path, {});
      ok(s.message);
      setLib(s);
    } catch (e) { fail(e); } finally { setBusy(null); }
  }

  async function pick(f: File | null) {
    if (!f) return;
    if (f.size > FILE_MAX) { fail(new Error("That file is too big to be a guide's import string.")); return; }
    try { setText(await f.text()); } catch (e) { fail(e); }
  }

  async function upload() {
    if (!faction) return;
    setUploading(true);
    try {
      const s = await api.post<Saved>("/api/guides/library/upload", { text, battletag: upTag.trim(), faction });
      ok(s.message);
      setLib(s);
      setText("");
      setUpTag("");
    } catch (e) { fail(e); } finally { setUploading(false); }
  }

  if (!lib) return <Text c="dimmed">Loading…</Text>;
  if (!lib.available) return null;
  const blocks = ORDER.map((f) => ({ f, rows: lib.rows.filter((r) => r.faction === f) })).filter((b) => b.rows.length);
  const factionWords = (f: string) => lib.factions.find((x) => x.id === f)?.words || f;
  const sentence = faction ? lib.upload_sentences[faction] : null;

  const row = (r: GuideRow) => (
    <Box key={r.id} className={`${css.row} ${r.withdrawn ? css.withdrawn : ""}`}>
      <Box className={css.who}>
        <Group gap={6} wrap="wrap">
          <Text size="sm" fw={600}>{r.when}</Text>
          {r.latest && <Badge size="sm" variant="light" color="teal" tt="none">Latest</Badge>}
        </Group>
        <Text size="xs" c="dimmed">Uploaded by {r.by} · {r.guides} guide{r.guides === 1 ? "" : "s"}</Text>
        {r.withdrawn && <Text size="xs" c="dimmed">Withdrawn by {r.withdrawn_by || "an officer"}, {r.withdrawn_when}</Text>}
      </Box>
      <Box className={css.get}>
        <Button size="xs" variant={r.latest ? "filled" : "default"} loading={getting === r.id} onClick={() => get(r)}>Get it for my BattleTag</Button>
      </Box>
      <Box className={css.menu}>
        {lib.can_withdraw && (
          <Menu shadow="md" width={220} position="bottom-end" classNames={{ item: css.menuItem }}>
            <Menu.Target>
              <ActionIcon variant="subtle" color="gray" loading={busy === r.id} aria-label={`The ${r.faction_words} version from ${r.when}`}><IconDotsVertical size={17} /></ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>{r.faction_words} · {r.when}</Menu.Label>
              {r.withdrawn
                ? <Menu.Item onClick={() => officer(r, "restore")}>Restore…</Menu.Item>
                : <Menu.Item color="red" onClick={() => officer(r, "withdraw")}>Withdraw…</Menu.Item>}
            </Menu.Dropdown>
          </Menu>
        )}
      </Box>
    </Box>
  );

  return (
    <Stack gap="lg" className={css.touch}>
      <Card>
        <CardHeader title="Guild guides" hint="leveling guides the guild made, locked to your BattleTag when you get one" />
        <Stack p="md" gap="md">
          <TextInput ref={tagRef} label="Your BattleTag" placeholder="Name#1234" value={tag} style={{ maxWidth: 320 }}
            description="Each guide you get here is locked to this BattleTag. It is not kept."
            error={tagAsk && !tag.trim() ? "Type your BattleTag first, then press Get it for my BattleTag again." : undefined}
            onChange={(e) => { setTag(e.currentTarget.value); setTagAsk(false); }} />
          {blocks.length === 0 ? (
            <Text size="sm" c="dimmed">No guild guide yet. Upload one below.</Text>
          ) : blocks.map((b) => (
            <Box key={b.f}>
              <Eyebrow plain>{factionWords(b.f)}</Eyebrow>
              <Box mt={4}>{b.rows.map(row)}</Box>
            </Box>
          ))}
        </Stack>
      </Card>

      <Card>
        <CardHeader title="Upload a guild guide" hint="it becomes the latest version for its faction" />
        <Stack p="md" gap="sm">
          <Text size="sm" c="dimmed">
            Paste the <b>import string</b> of a guide the guild made, or pick the .txt file it came in, and the BattleTag it was issued to.
            The string is unlocked once with that BattleTag and dropped; only the guide is kept, for every member to get locked to their own BattleTag.
          </Text>
          <Textarea label="Import string" placeholder="78|…" autosize minRows={3} maxRows={6} value={text} disabled={uploading}
            onChange={(e) => setText(e.currentTarget.value)} styles={{ input: { fontFamily: "var(--mantine-font-family-monospace)", fontSize: 12 } }} />
          <Group gap="sm" wrap="wrap">
            <FileButton onChange={pick} accept=".txt,text/plain">{(props) => <Button size="xs" variant="default" disabled={uploading} {...props}>Pick a .txt file…</Button>}</FileButton>
            {text && <Button size="xs" variant="subtle" color="gray" disabled={uploading} onClick={() => setText("")}>Clear the box</Button>}
          </Group>
          <Group align="flex-end" gap="sm" wrap="wrap">
            <TextInput label="The BattleTag this string was issued to" placeholder="Name#1234" value={upTag} disabled={uploading}
              onChange={(e) => setUpTag(e.currentTarget.value)} style={{ flex: "1 1 220px", maxWidth: 320 }} />
            <Select label="Faction" placeholder="Pick the guide's faction" data={lib.factions.map((f) => ({ value: f.id, label: f.words }))}
              value={faction} onChange={setFaction} disabled={uploading} allowDeselect={false} style={{ flex: "1 1 180px", maxWidth: 240 }} />
          </Group>
          {sentence && <Text size="sm">{sentence}</Text>}
          <Group>
            <Button onClick={upload} loading={uploading} disabled={!text.trim() || !upTag.trim() || !faction}>Upload</Button>
          </Group>
        </Stack>
      </Card>
      {confirmDialog}
    </Stack>
  );
}
