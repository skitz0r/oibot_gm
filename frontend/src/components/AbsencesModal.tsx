import { useEffect, useState } from "react";
import { Alert, Button, Group, Modal, Select, Stack, Text, TextInput } from "@mantine/core";
import { DateInput } from "@mantine/dates";
import { api, type AbsencePreview, type Meta } from "../api";
import { fail, ok } from "./Page";

const FALLBACK = [1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30].map((d) => ({ days: d, label: d === 1 ? "1 day" : d === 7 ? "a week" : `${d} days` }));
const iso = (d: Date | null) => (d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` : null);

/** An absence, the Discord way (design §3.3 cluster B): **From** (a day) + **For how long** (the wizard's own list, from
 *  /api/meta `durations`) + an optional reason — so an end before the start, or past the cap, can't be entered. Under the
 *  pickers the server's preview (`{preview: true}`): the days in words and what it does to each live sheet, or the verb's
 *  refusal (an overlap with an absence on file, the reason cap) before anything is pressed. `who` null = your own (the
 *  Me page); a member = an officer recording it for them (the Members page). */
export function AbsencesModal({ meta, opened, who, onClose, onSaved }: { meta: Meta; opened: boolean; who: { uid: string; display_name: string } | null; onClose: () => void; onSaved: () => void }) {
  const [start, setStart] = useState<Date | null>(null);
  const [days, setDays] = useState("1");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<AbsencePreview | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const path = who ? "/api/members/absence" : "/api/me/absence";
  const durations = meta.durations?.length ? meta.durations : FALLBACK;
  const max = meta.absence_reason_max ?? 200;
  const body = { ...(who ? { uid: who.uid } : {}), start: iso(start), days: Number(days), reason };

  useEffect(() => {
    if (!opened || !start) { setPreview(null); setRefusal(null); return; }
    let live = true;
    const t = setTimeout(() => {
      api.post<AbsencePreview>(path, { ...body, preview: true })
        .then((p) => { if (live) { setPreview(p); setRefusal(null); } })
        .catch((e) => { if (live) { setPreview(null); setRefusal((e as Error).message); } });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [opened, start, days, reason, path, who?.uid]);  // eslint-disable-line react-hooks/exhaustive-deps

  function reset() { setStart(null); setDays("1"); setReason(""); setPreview(null); setRefusal(null); }
  async function add() {
    setBusy(true);
    try { const r = await api.post<{ message: string }>(path, body); ok(r.message); reset(); onSaved(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const n = preview?.lines.length ?? 0;
  return (
    <Modal opened={opened} onClose={() => { reset(); onClose(); }} title={who ? `${who.display_name} is away` : "I'll be away"} centered>
      <Stack>
        <Group grow align="flex-start">
          <DateInput label="From" value={start} onChange={(v) => setStart(v ? new Date(v) : null)} required valueFormat="ddd D MMM YYYY" />
          <Select label="For how long" value={days} onChange={(v) => setDays(v || "1")} allowDeselect={false}
            data={durations.map((d) => ({ value: String(d.days), label: d.label }))} description="counted from the first day" />
        </Group>
        <TextInput label="Reason (optional)" description={`only ${who ? "they" : "you"} and the officers see it · ${reason.length}/${max}`} value={reason} maxLength={max}
          onChange={(e) => setReason(e.currentTarget.value)} />
        {preview && (
          <Stack gap={2}>
            <Text size="sm" fw={600}>{preview.span}</Text>
            {preview.lines.length ? preview.lines.map((l, i) => <Text key={i} size="xs">{l}</Text>) : <Text size="xs" c="dimmed">No live sheet falls in those days.</Text>}
          </Stack>
        )}
        {refusal && <Alert color="red" variant="light" p="xs"><Text size="sm">{refusal}</Text></Alert>}
        <Group justify="flex-end">
          <Button variant="default" onClick={() => { reset(); onClose(); }}>Cancel</Button>
          <Button disabled={!start || !!refusal} loading={busy} onClick={add}>{n ? `Add — touches ${n} sheet${n === 1 ? "" : "s"}` : "Add"}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
