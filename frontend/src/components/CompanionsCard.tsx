import { useEffect, useState } from "react";
import { Alert, Badge, Button, Card, Code, CopyButton, Group, Modal, Stack, Table, Text, TextInput } from "@mantine/core";
import { api } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { useDirty } from "../hooks/useDirty";
import { usePoll } from "../hooks/usePoll";

/** One issued companion token as GET /api/companions shows it (feed.token_rows): never a value, never a hash. */
interface TokenRow {
  id: string; label: string; state: "live" | "quiet" | "disconnected" | "offline" | "never" | "revoked"; state_words: string;
  characters: string[]; events: number; last_seen_label: string | null; issued_by: string; issued_label: string; sent_to: string | null;
  active: boolean; revoked_label: string | null; revoked_by: string | null; revoke_consequence: string | null;
}
interface CompanionsData {
  tokens: TokenRow[]; listening: boolean; local_only?: boolean; listen_words?: string; max: number; label_max: number; issue_consequence: string;
  env: { shared: boolean; var: string; set: boolean; bind_set: boolean };
}
interface Issued { token: string; label: string; message: string }

const STATE_COLOUR: Record<string, string> = { live: "teal", quiet: "yellow", disconnected: "red", offline: "gray", never: "gray", revoked: "gray" };

/** Config › Companions (owner; design §5.50, out/dual_interface_design.md §3.1 L08/S-03): issue a token for a raid leader's
 *  loot companion — shown ONCE in a modal with Copy and "I've saved it", then gone for good (the bot keeps a hash) — revoke
 *  one (the server's consequence sentence in the confirm; the companion is cut off at its next message, no restart), and
 *  see which companions are connected with each. Discord's /gm config companion is the same verbs and DMs the token. */
export function CompanionsCard() {
  const [d, setD] = useState<CompanionsData | null>(null);
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<Issued | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  // a typed label or a token on screen is a draft: the server selector asks before switching (the token would be lost)
  useDirty("companions", label.trim() !== "" || issued !== null);
  const load = () => api.get<CompanionsData>("/api/companions").then(setD).catch(fail);
  useEffect(() => { load(); }, []);
  usePoll(load);
  if (!d) return <Card><Text c="dimmed" p="md">Loading…</Text></Card>;
  const active = d.tokens.filter((t) => t.active);

  async function issue() {
    setBusy(true);
    try {
      const r = await api.post<CompanionsData & Issued>("/api/companions/issue", { label: label.trim() });
      setIssued({ token: r.token, label: r.label, message: r.message });
      setLabel("");
      setD(r);
    } catch (e) { fail(e); } finally { setBusy(false); }
  }

  async function revoke(t: TokenRow) {
    if (!(await ask({ title: `Revoke “${t.label}”?`, message: t.revoke_consequence || "", confirmLabel: "Revoke", color: "red" }))) return;
    try {
      const r = await api.post<CompanionsData & { message: string }>("/api/companions/revoke", { id: t.id });
      setD(r);
      ok(r.message);
    } catch (e) { fail(e); }
  }

  return (
    <Card>
      <CardHeader title="Companions" hint="owner · loot companion tokens" />
      <Stack p="md" gap="md">
        <Text size="sm">The companion on a raid leader's PC streams drops and awards from their chat log. Each token feeds this guild only. {d.issue_consequence}</Text>
        {(!d.listening || d.local_only) && <Alert color="yellow" py="xs">{d.listen_words}</Alert>}
        <Group align="flex-end" gap="sm" wrap="wrap">
          <TextInput label="Label: whose PC runs it" placeholder="Jon's PC" value={label} maxLength={d.label_max} style={{ flex: "1 1 220px" }}
            onChange={(e) => setLabel(e.currentTarget.value)} description={`${label.length}/${d.label_max}`} />
          <Button onClick={issue} loading={busy} disabled={!label.trim() || active.length >= d.max}>Issue a token</Button>
        </Group>
        {active.length >= d.max && <Text size="xs" c="dimmed">{d.max} tokens in use — revoke one to issue another.</Text>}
        <Table.ScrollContainer minWidth={560}>
          <Table>
            <Table.Thead><Table.Tr><Table.Th>Label</Table.Th><Table.Th>Presence</Table.Th><Table.Th>Last seen</Table.Th><Table.Th>Issued</Table.Th><Table.Th /></Table.Tr></Table.Thead>
            <Table.Tbody>
              {d.tokens.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">No companion token yet.</Text></Table.Td></Table.Tr>}
              {d.tokens.map((t) => (
                <Table.Tr key={t.id} style={t.active ? undefined : { opacity: 0.55 }}>
                  <Table.Td><Text size="sm" fw={600}>{t.label}</Text><Text size="xs" c="dimmed">{t.id}</Text></Table.Td>
                  <Table.Td>
                    <Badge variant="light" color={STATE_COLOUR[t.state] || "gray"}>{t.state_words}</Badge>
                    {t.characters.length > 0 && t.state !== "revoked" && <Text size="xs" c="dimmed">{t.characters.join(", ")} · {t.events} events</Text>}
                  </Table.Td>
                  <Table.Td><Text size="sm">{t.last_seen_label || "—"}</Text></Table.Td>
                  <Table.Td>
                    <Text size="sm">{t.issued_label}</Text>
                    <Text size="xs" c="dimmed">by {t.issued_by}{t.sent_to ? ` · DMed to ${t.sent_to}` : ""}{t.revoked_label ? ` · revoked ${t.revoked_label}` : ""}</Text>
                  </Table.Td>
                  <Table.Td>{t.active && <Button size="xs" variant="light" color="red" onClick={() => revoke(t)}>Revoke</Button>}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Text size="xs" c="dimmed">In Discord: <Code>/gm config companion</Code> issues a token straight to the raid leader's DMs. Host tokens in .env ({d.env.var}{d.env.set ? " is set" : " is not set"}{d.env.shared ? ", OIBOT_FEED_TOKEN is set" : ""}) keep working.</Text>
      </Stack>
      <Modal opened={issued !== null} onClose={() => setIssued(null)} title={issued ? `Token “${issued.label}”` : ""} centered closeOnClickOutside={false} closeOnEscape={false} withCloseButton={false}>
        {issued && (
          <Stack gap="md">
            <Alert color="yellow" py="xs">This is the only time it is shown. Copy it to the raid leader's PC (or send it privately) — never post it in a channel.</Alert>
            <Code block style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{issued.token}</Code>
            <Text size="xs" c="dimmed">Start the companion with <Code>--token</Code> (or <Code>--token-file</Code>, or OIBOT_COMPANION_TOKEN).</Text>
            <Group justify="flex-end" gap="sm">
              <CopyButton value={issued.token}>{({ copied, copy }) => <Button variant="light" onClick={copy}>{copied ? "Copied" : "Copy"}</Button>}</CopyButton>
              <Button onClick={() => { setIssued(null); ok(issued.message); }}>I've saved it</Button>
            </Group>
          </Stack>
        )}
      </Modal>
      {confirmDialog}
    </Card>
  );
}
