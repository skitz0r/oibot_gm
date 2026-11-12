import { useEffect, useRef, useState } from "react";
import { Button, Card, Code, Group, Stack, Table, Text, TextInput } from "@mantine/core";
import { api } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { useDirty } from "../hooks/useDirty";

/** GET /api/admin/armory: the guild this server is linked to in the armory (armory link §5.1). */
interface ArmoryState {
  linked: { name: string; faction: string; guild_id: number; region: string; linked_by: string; linked_label: string } | null;
  clear_sentences: string[]; rev: string; owner: boolean; name_max: number;
}
/** POST /api/admin/armory {name, preview}: what the lookup found and the confirm sentences (nothing written). */
interface Found {
  found: { name: string; faction: string; kind: string; members: number; guild_id: number; created_label: string };
  sentences: string[];
  /** the name this answer is for (set here, not by the server): an answer for a name no longer in the box is dropped */
  query: string;
}

/** Config › Settings (owner edits, officers read; shown only when /api/meta.armory): link this server to its guild in the
 *  game. The owner types the guild's name, **Look it up** shows what the armory found — name, faction, kind, members,
 *  created — so a stranger's guild of the same name is seen first; **Link this guild** confirms with the server's
 *  sentences. **Unlink…** confirms with the server's sentences too (every character keeps its link). Discord's
 *  /gm config armory and MCP set_armory_guild are the same verbs. */
export function ArmoryGuildCard() {
  const [d, setD] = useState<ArmoryState | null>(null);
  const [name, setName] = useState("");
  const [found, setFound] = useState<Found | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const typed = useRef("");  // the box as it is now: a lookup that answers after the name changed is ignored
  typed.current = name.trim();
  useDirty("armory", name.trim() !== "" || found !== null);
  const load = () => api.get<ArmoryState>("/api/admin/armory").then((r) => { setD(r); setLoadFailed(false); }).catch((e) => { setLoadFailed(true); fail(e); });
  useEffect(() => { load(); }, []);
  if (!d) {
    return (
      <Card>
        {loadFailed ? (
          <Group p="md" gap="sm" justify="space-between" wrap="wrap">
            <Text size="sm">The guild in the armory couldn't be loaded. Reload to try again.</Text>
            <Button size="xs" variant="light" onClick={() => load()}>Reload</Button>
          </Group>
        ) : <Text c="dimmed" p="md">Loading…</Text>}
      </Card>
    );
  }

  async function lookUp() {
    const query = name.trim();
    setBusy(true);
    try {
      const r = await api.post<Omit<Found, "query">>("/api/admin/armory", { name: query, preview: true });
      if (typed.current === query) setFound({ ...r, query });
    } catch (e) { if (typed.current === query) { setFound(null); fail(e); } } finally { setBusy(false); }
  }

  async function link() {
    if (!found || !d) return;
    if (!(await ask({ title: `Link ${found.found.name}?`, message: found.sentences.join(" "), confirmLabel: "Link this guild" }))) return;
    try {
      const r = await api.post<ArmoryState & { message: string }>("/api/admin/armory", { name: found.query, guild_id: found.found.guild_id, rev: d.rev });
      setD(r); setFound(null); setName("");
      ok(r.message);
    } catch (e) { fail(e); await load(); }
  }

  async function unlink() {
    if (!d?.linked) return;
    if (!(await ask({ title: `Unlink ${d.linked.name}?`, message: d.clear_sentences.join(" "), confirmLabel: "Unlink", color: "red" }))) return;
    try {
      const r = await api.post<ArmoryState & { message: string }>("/api/admin/armory", { clear: true, rev: d.rev });
      setD(r);
      ok(r.message);
    } catch (e) { fail(e); await load(); }
  }

  const f = found?.found;
  return (
    <Card>
      <CardHeader title="The guild in the armory" hint={d.owner ? "owner" : "the owner links it"} />
      <Stack p="md" gap="md">
        {d.linked ? (
          <Group justify="space-between" wrap="wrap" gap="sm">
            <div>
              <Text size="sm" fw={600}>{d.linked.name}</Text>
              <Text size="xs" c="dimmed">{d.linked.faction} · linked by {d.linked.linked_by} on {d.linked.linked_label}</Text>
            </div>
            {d.owner && <Button size="xs" variant="light" color="red" onClick={unlink}>Unlink…</Button>}
          </Group>
        ) : (
          <Text size="sm">{d.owner ? "No guild is linked to this server yet. Type its name as it is spelled in the game and look it up."
            : "No guild is linked to this server yet. The owner links it here or with /gm config armory."}</Text>
        )}
        {d.owner && (
          <>
            <Group align="flex-end" gap="sm" wrap="wrap">
              <TextInput label={d.linked ? "Link another guild: its name in the game" : "The guild's name in the game"} placeholder="as it is spelled in the game"
                value={name} maxLength={d.name_max} style={{ flex: "1 1 220px" }}
                onChange={(e) => { setName(e.currentTarget.value); setFound(null); }}
                onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) lookUp(); }} />
              <Button variant="light" onClick={lookUp} loading={busy} disabled={!name.trim()}>Look it up</Button>
            </Group>
            {f && found && (
              <Stack gap="xs">
                <Table withRowBorders={false} verticalSpacing={4}>
                  <Table.Tbody>
                    <Table.Tr><Table.Td><Text size="xs" c="dimmed">Name</Text></Table.Td><Table.Td><Text size="sm" fw={600}>{f.name}</Text></Table.Td></Table.Tr>
                    <Table.Tr><Table.Td><Text size="xs" c="dimmed">Faction</Text></Table.Td><Table.Td><Text size="sm">{f.faction}</Text></Table.Td></Table.Tr>
                    <Table.Tr><Table.Td><Text size="xs" c="dimmed">Kind</Text></Table.Td><Table.Td><Text size="sm">{f.kind}</Text></Table.Td></Table.Tr>
                    <Table.Tr><Table.Td><Text size="xs" c="dimmed">Members</Text></Table.Td><Table.Td><Text size="sm">{f.members}</Text></Table.Td></Table.Tr>
                    <Table.Tr><Table.Td><Text size="xs" c="dimmed">Created</Text></Table.Td><Table.Td><Text size="sm">{f.created_label}</Text></Table.Td></Table.Tr>
                  </Table.Tbody>
                </Table>
                <Text size="sm">{found.sentences.join(" ")}</Text>
                <Group justify="flex-end" gap="sm">
                  <Button variant="default" onClick={() => { setFound(null); setName(""); }}>Cancel</Button>
                  <Button onClick={link}>Link this guild</Button>
                </Group>
              </Stack>
            )}
          </>
        )}
        <Text size="xs" c="dimmed">In Discord: <Code>/gm config armory</Code>.</Text>
      </Stack>
      {confirmDialog}
    </Card>
  );
}
