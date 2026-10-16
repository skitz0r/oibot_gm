import { useState } from "react";
import { Badge, Box, Button, Group, Modal, Select, Stack, Text, Textarea, TextInput, Tooltip } from "@mantine/core";
import { ApiError, type LootReply, type LootRun, type Meta } from "../../api";
import { GameIcon } from "../Icons";
import { classColour } from "../../theme";
import { fail } from "../Page";

/** A character on a loot row: the spec icon alone (role + spec; the names live in the tooltip) and the name in its
 *  class colour. Without a known class/spec: the name only. */
export function Who({ meta, name, cls, spec, dim }: { meta: Meta; name: string; cls?: string | null; spec?: string | null; dim?: boolean }) {
  if (name === "disenchant") return <Text size="sm" c="dimmed" fs="italic">disenchant</Text>;
  return (
    <Group gap={6} wrap="nowrap" style={{ minWidth: 0, opacity: dim ? 0.55 : 1 }}>
      {cls && spec && <Tooltip label={`${spec} ${cls}`}><Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind="spec" id={`${cls}:${spec}`} size={20} title={`${spec} ${cls}`} /></Box></Tooltip>}
      <Text size="sm" fw={600} c={cls ? classColour(meta, cls) : undefined} truncate>{name}</Text>
    </Group>
  );
}

export const STATE_MARK: Record<string, string> = { ticked: "▫️", proposed: "🟡", awarded: "✅" };
export const COMPANION_COLOUR: Record<string, string> = { live: "teal", quiet: "yellow", disconnected: "red" };

/** A write that may come back 409 (a stale batch_rev / drops_rev, a judge already running): the page takes the run as it
 *  is now from the refusal and says what changed; anything else is a red toast. Returns the reply, or null. */
export async function lootWrite(call: () => Promise<LootReply>, onState: (run: LootRun) => void): Promise<LootReply | null> {
  try {
    const r = await call();
    if (r.state) onState(r.state);
    return r;
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const body = e.body as { error?: string; state?: LootRun } | undefined;
      if (body?.state) onState(body.state);
      fail(new Error(`${body?.error || e.message} — the card shows it as it is now.`));
      return null;
    }
    fail(e);
    return null;
  }
}

/** Award a drop to someone else, or record a correction: who (the roster, the table's candidates, Disenchant, or a
 *  name not on the roster — validated by the server) and the REQUIRED reason. */
export function WhoReasonModal({ opened, title, people, current, reasonLabel, onClose, onSubmit }: {
  opened: boolean; title: string; people: { value: string; label: string; group: string }[]; current?: string | null; reasonLabel: string;
  onClose: () => void; onSubmit: (to: string, reason: string) => Promise<boolean>;
}) {
  const [to, setTo] = useState<string | null>(null);
  const [other, setOther] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const groups = Array.from(new Set(people.map((p) => p.group)));
  const data = [...groups.map((g) => ({ group: g, items: people.filter((p) => p.group === g && p.value !== current).map((p) => ({ value: p.value, label: p.label })) })),
    { group: "Other", items: [{ value: "disenchant", label: "Disenchant" }, { value: "__other", label: "Someone not on the roster…" }] }];
  const who = to === "__other" ? other.trim() : to;
  const reset = () => { setTo(null); setOther(""); setReason(""); };
  return (
    <Modal opened={opened} onClose={() => { reset(); onClose(); }} title={title} centered>
      <Stack gap="sm">
        <Select label="Who gets it" data={data} value={to} onChange={setTo} searchable placeholder="Pick a character" />
        {to === "__other" && <TextInput label="Character name" description="a sub or a guest: checked against the signups and registered characters" value={other} maxLength={40} onChange={(e) => setOther(e.currentTarget.value)} />}
        <Textarea label={reasonLabel} required autosize minRows={2} maxLength={300} value={reason} onChange={(e) => setReason(e.currentTarget.value)} description={`${reason.trim().length}/300 · at least 3 characters`} />
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={() => { reset(); onClose(); }}>Back</Button>
          <Button loading={busy} disabled={!who || reason.trim().length < 3} onClick={async () => { setBusy(true); const done = await onSubmit(who!, reason.trim()); setBusy(false); if (done) { reset(); onClose(); } }}>Save</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** One required prose field (a reason for an award the companion recorded, a precedent's retire reason). */
export function ReasonModal({ opened, title, message, label, onClose, onSubmit }: { opened: boolean; title: string; message?: React.ReactNode; label: string; onClose: () => void; onSubmit: (reason: string) => Promise<boolean> }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={() => { setReason(""); onClose(); }} title={title} centered>
      <Stack gap="sm">
        {message && <Text size="sm">{message}</Text>}
        <Textarea label={label} required autosize minRows={2} maxLength={300} value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={() => { setReason(""); onClose(); }}>Back</Button>
          <Button loading={busy} disabled={reason.trim().length < 3} onClick={async () => { setBusy(true); const done = await onSubmit(reason.trim()); setBusy(false); if (done) { setReason(""); onClose(); } }}>Save</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function SourceBadge({ source }: { source: string | null | undefined }) {
  if (!source) return null;
  const colour: Record<string, string> = { override: "orange", companion: "blue", manual: "gray", correct: "grape", bot: "teal" };
  return <Badge size="xs" variant="light" color={colour[source] || "gray"}>{source}</Badge>;
}
