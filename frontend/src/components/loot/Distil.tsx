import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Group, Loader, Modal, Stack, Text, Textarea } from "@mantine/core";
import { api, type OpsReply } from "../../api";
import { ReviewModal } from "../ReviewModal";
import { fail, ok } from "../Page";
import { useDirty } from "../../hooks/useDirty";

/** One drafted sentence (POST /api/loot/distil): the precedents it rests on come from code, checked against the active list. */
interface Proposal { id: string; text: string; why: string; rests_on: { ref: string; item: string; human: string; bot: string; reason: string; date_label?: string }[] }
interface Draft { proposals: Proposal[]; dropped: string[]; warnings: string[]; precedents: number; provenance: string }

/** Loot › Precedents › Distil (L29, design §5.48): Claude reads the active precedents and the compiled loot policy and drafts
 *  at most five clarification sentences, each beside the precedents it rests on — "drafted from N precedents, unverified".
 *  The officer ticks the ones worth keeping (and may reword them); Review shows exactly those lines (the ReviewModal, one
 *  confirm for the batch) and Apply appends them through the policy_append verb as a draft for compile and confirm.
 *  Nothing is written by the model. */
export function DistilModal({ onClose }: { onClose: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<string, string>>({});  // proposal id → the sentence as the officer will append it
  const [preview, setPreview] = useState<OpsReply | null>(null);
  const [result, setResult] = useState<OpsReply | null>(null);
  const [busy, setBusy] = useState(false);
  useDirty("distil", Object.keys(picked).length > 0 && result === null);
  useEffect(() => {
    api.post<{ draft: Draft }>("/api/loot/distil", {}).then((r) => setDraft(r.draft)).catch((e) => setError((e as Error).message));
  }, []);
  const sentences = () => (draft?.proposals || []).filter((p) => p.id in picked).map((p) => picked[p.id].trim()).filter(Boolean);
  async function review() {
    setBusy(true);
    try { setPreview(await api.post<OpsReply>("/api/loot/distil/append", { sentences: sentences() })); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  async function apply() {
    if (!preview?.token) return;
    setBusy(true);
    try {
      const r = await api.post<OpsReply>("/api/loot/distil/append", { sentences: sentences(), token: preview.token });
      setPreview(null); setResult(r); ok(r.summary || "added");
    } catch (e) { fail(e); setPreview(null); } finally { setBusy(false); }
  }
  return (
    <Modal opened onClose={onClose} title="Distil the precedents" size="xl">
      <Stack gap="sm">
        {!draft && !error && <Group gap="xs"><Loader size="xs" /><Text size="sm" c="dimmed">Claude is reading the precedents and the policy…</Text></Group>}
        {error && <Alert color="yellow" py="xs">{error}</Alert>}
        {draft && (
          <>
            <Text size="xs" c="dimmed">{draft.provenance}. Tick the sentences worth adding (reword them if you like); they go into the loot policy as a draft that the officers compile and confirm on the Policy page.</Text>
            {draft.proposals.length === 0 && <Text size="sm" c="dimmed">No clarification stands out — the policy already covers the precedents.</Text>}
            {draft.proposals.map((p) => (
              <Stack key={p.id} gap={4} p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 6 }}>
                <Checkbox label="Add this" checked={p.id in picked} onChange={(e) => {
                  const on = e.currentTarget.checked;
                  setPicked((m) => { const n = { ...m }; if (on) n[p.id] = p.text; else delete n[p.id]; return n; });
                }} />
                <Textarea autosize minRows={1} maxRows={4} maxLength={280} value={p.id in picked ? picked[p.id] : p.text} disabled={!(p.id in picked)}
                  onChange={(e) => { const v = e.currentTarget.value; setPicked((m) => ({ ...m, [p.id]: v })); }} />
                {p.why && <Text size="xs" c="dimmed">{p.why}</Text>}
                <Stack gap={0}>
                  {p.rests_on.map((r) => <Text key={r.ref} size="xs" c="dimmed">· {r.date_label ? `${r.date_label} · ` : ""}{r.item}: the bot picked {r.bot}, the council gave it to {r.human} — “{r.reason}”</Text>)}
                </Stack>
              </Stack>
            ))}
            {draft.warnings.map((w) => <Text key={w} size="xs" c="yellow">{w}</Text>)}
            {draft.dropped.length > 0 && <Text size="xs" c="dimmed">Left out by the checks: {draft.dropped.join(" · ")}</Text>}
          </>
        )}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Close</Button>
          <Button disabled={!sentences().length || result !== null} loading={busy} onClick={review}>Review {sentences().length || ""} sentence{sentences().length === 1 ? "" : "s"}</Button>
        </Group>
      </Stack>
      <ReviewModal opened={preview !== null || result !== null} title="Add to the loot policy" preview={preview} result={result} busy={busy}
        onApply={apply} onClose={() => { if (result) { setResult(null); onClose(); } else setPreview(null); }} />
    </Modal>
  );
}
