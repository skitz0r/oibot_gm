import { useEffect, useState } from "react";
import { Badge, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { api, type FillPreview, type Meta, type Sheet } from "../../api";
import { GameIcon } from "../Icons";
import { classColour } from "../../theme";
import { Eyebrow, fail, ok } from "../Page";
import { runName } from "./shared";

const KIND: Record<string, string> = { sub: "from the bench", pool: "from the pool", other_roster: "from the other roster", offspec: "swap to offspec", alt: "bring an alt" };

/** Fill seats (post-lock): preview what the fill engine would send — the shortfall, who is still being waited on, the next batch — then send. */
export function FillModal({ e, meta, opened, onClose, onSent }: { e: Sheet; meta: Meta; opened: boolean; onClose: () => void; onSent: () => Promise<void> }) {
  const [p, setP] = useState<FillPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  useEffect(() => {
    if (!opened) return;
    setP(null); setBusy(true);
    api.post<FillPreview>(`/api/run/${e.key}/fill/preview`, {}).then(setP).catch((err) => { fail(err); onClose(); }).finally(() => setBusy(false));
  }, [opened, e.key]); // eslint-disable-line react-hooks/exhaustive-deps
  async function send() {
    setSending(true);
    try { const r = await api.post<{ message: string }>(`/api/run/${e.key}/fill`, {}); ok(r.message); await onSent(); onClose(); } catch (err) { fail(err); } finally { setSending(false); }
  }
  const short = p?.short;
  const nothingShort = short ? !short.headcount && !Object.keys(short.roles).length : false;
  return (
    <Modal opened={opened} onClose={onClose} title={`Fill seats · ${runName(e)}`} centered>
      <Stack gap="md">
        {busy && <Text size="sm" c="dimmed">Working out who to ask…</Text>}
        {p && short && (
          <>
            <Group gap="md" align="center">
              <Eyebrow>Short</Eyebrow>
              {nothingShort ? <Badge variant="light" color="teal">full</Badge> : (
                <Group gap="md">
                  {short.headcount > 0 && <Text size="sm" fw={600} style={{ fontVariantNumeric: "tabular-nums" }}>{short.headcount} seat{short.headcount === 1 ? "" : "s"}</Text>}
                  {Object.entries(short.roles).map(([role, k]) => <Group key={role} gap={4} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={18} title={role} /><Text size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>{k}</Text></Group>)}
                </Group>
              )}
            </Group>
            {p.waiting.length > 0 && <Text size="xs" c="dimmed">Still waiting on {p.waiting.join(", ")}.</Text>}
            <Stack gap={2}>
              <Eyebrow>Asks to send</Eyebrow>
              {p.batch.length === 0 && <Text size="sm" c="dimmed">{nothingShort ? "Nothing to fill." : "Nobody left to ask — everyone who could cover it has been asked or is unavailable."}</Text>}
              {p.batch.map((a, i) => (
                <Group key={i} gap={6} wrap="nowrap" style={{ lineHeight: 1.8, minWidth: 0 }}>
                  {a.cls ? <GameIcon meta={meta} kind="spec" id={`${a.cls}:${a.spec}`} size={18} title={`${a.cls} ${a.spec} · ${a.role}`} /> : <GameIcon meta={meta} kind="role" id={a.role} size={18} title={a.role} />}
                  <Text size="sm" fw={600} c={a.cls ? classColour(meta, a.cls) : undefined} truncate>{a.character}</Text>
                  <Text size="xs" c="dimmed" truncate>{a.display_name}</Text>
                  <Text size="xs" c="dimmed" style={{ flex: "none" }}>· {KIND[a.kind] || a.kind} · {a.reason}</Text>
                </Group>
              ))}
            </Stack>
            <Text size="xs" c="dimmed">Each ask is a DM with Yes / No; no answer by the deadline counts as no. Tied pairs (a swap and its backfill) go out together.</Text>
          </>
        )}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button disabled={!p || p.batch.length === 0} loading={sending} onClick={send}>Send {p?.batch.length || 0} ask{p?.batch.length === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
