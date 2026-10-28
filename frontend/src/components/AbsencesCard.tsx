import { useState } from "react";
import { Badge, Button, Card, Group, Stack, Text, Tooltip } from "@mantine/core";
import { IconPlus } from "@tabler/icons-react";
import { api, type Absence, type AbsenceCleared, type AbsencePreview, type Meta } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { AbsencesModal } from "./AbsencesModal";

/** "I'm back" on the web (design §3.3 cluster B): the server's preview first (which sheets re-open; a seat handed back
 *  stays handed back), then the clear — the same verb as the absences card's I'm back, /me absent clear and /roster back.
 *  `who` null = your own absence; a member = an officer clearing theirs. Returns the press handler and the dialog. */
export function useImBack(onDone: () => void): [(a: Absence, who?: { uid: string; display_name: string } | null) => Promise<void>, React.ReactNode, string | null] {
  const [ask, dialog] = useConfirm();
  const [busy, setBusy] = useState<string | null>(null);
  async function press(a: Absence, who: { uid: string; display_name: string } | null = null) {
    const path = who ? "/api/members/absence/clear" : "/api/me/absence/clear";
    const body = { ...(who ? { uid: who.uid } : {}), start: a.start };
    setBusy(`${who?.uid ?? "me"}${a.start}`);
    try {
      const p = await api.post<AbsencePreview>(path, { ...body, preview: true });
      const message = (p.sentences || []).join("\n");  // THE I'm back confirm (consequences.back_consequence), never the browser's words
      if (!(await ask({ title: who ? `${who.display_name} is back?` : "Back after all?", message, confirmLabel: "I'm back" }))) return;
      const x = await api.post<AbsenceCleared>(path, body);
      ok(<Stack gap={2}><Text size="sm" fw={600}>{x.message.split("\n")[0]}</Text>{x.lines.map((l, i) => <Text key={i} size="sm">{l}</Text>)}</Stack>);
      onDone();
    } catch (e) { fail(e); } finally { setBusy(null); }
  }
  return [press, dialog, busy];
}

/** The Me page's Absences card: your upcoming absences in words, Add (From + For how long, previewed) and I'm back. */
export function AbsencesCard({ meta, absences, onChanged }: { meta: Meta; absences: Absence[]; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [back, dialog, busy] = useImBack(onChanged);
  return (
    <Card>
      <CardHeader title="Absences" action={<Button size="xs" variant="default" leftSection={<IconPlus size={13} />} onClick={() => setOpen(true)}>Add</Button>} />
      <Stack p="md" gap="xs">
        {absences.length === 0 && <Text c="dimmed" size="sm">No absences on file. Add one if you'll miss a raid.</Text>}
        {absences.map((a) => (
          // a narrow card (the Me page's side column): the days wrap, the reason chip shrinks (its full text on hover)
          // and the actions drop under the days rather than run past the card's edge; I'm back is never squeezed
          <Group key={a.start} justify="space-between" wrap="wrap" gap={6} align="center">
            <Text size="sm" style={{ flex: "1 1 8rem", minWidth: 0 }}>{a.span || a.start}</Text>
            <Group gap="xs" wrap="nowrap" ml="auto" style={{ minWidth: 0, maxWidth: "100%" }}>
              {a.reason && <Tooltip label={a.reason}><Badge variant="outline" color="gray" tt="none" title={a.reason} style={{ minWidth: 0, flexShrink: 1, maxWidth: 160 }}>{a.reason}</Badge></Tooltip>}
              <Button size="compact-xs" variant="subtle" style={{ flexShrink: 0 }} loading={busy === `me${a.start}`} onClick={() => back(a)}>I'm back</Button>
            </Group>
          </Group>
        ))}
      </Stack>
      <AbsencesModal meta={meta} opened={open} who={null} onClose={() => setOpen(false)} onSaved={() => { setOpen(false); onChanged(); }} />
      {dialog}
    </Card>
  );
}
