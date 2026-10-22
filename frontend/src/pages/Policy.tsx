import { useEffect, useState } from "react";
import { Alert, Badge, Box, Button, Card, Group, SimpleGrid, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { IconPencil, IconPlus } from "@tabler/icons-react";
import { api, type PolicyDoc } from "../api";
import { CardHeader, Eyebrow, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { useLive } from "../hooks/useLive";

const TITLE: Record<string, string> = { loot: "Loot policy", comp: "Standing comp instructions", persona: "Persona" };
const HINT: Record<string, string> = {
  loot: "the loot council cites these rules as [R1], [R2]… — the compiled reading is what it applies",
  comp: "standing instructions for the group solver — the compiled constraints are what it applies",
  persona: "the bot's voice in every answer — the confirmed text is what it speaks with",
};

/** Policy (officers, D36): per document the WRITTEN text beside what is APPLIED (the confirmed reading, compiled when),
 *  a red bar when they differ, the waiting draft (whose, with Confirm / Discard), and the two-step edit: Save draft →
 *  Compile → the reading beside the draft → Confirm or Discard. Add a rule appends one line as a draft. Every step is the
 *  same function Discord's /gm policy and /gm rule, plain text and MCP call (policy.py). */
export function PolicyPage() {
  const [docs, setDocs] = useState<PolicyDoc[] | null>(null);
  const load = () => api.get<{ docs: PolicyDoc[] }>("/api/policy").then((d) => setDocs(d.docs)).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["agents", "config", "policy"]);
  if (!docs) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack gap="lg">
      <PageTitle title="Policy" intro="What officers wrote, and what the bot applies. An edit is a draft until its reading is confirmed; Discard puts the text before it back. The same steps work in Discord (/gm policy, /gm rule) and in plain text." />
      {docs.map((d) => <DocCard key={d.doc} d={d} onChange={(n) => setDocs((cur) => (cur || []).map((x) => (x.doc === n.doc ? n : x)))} reload={load} />)}
    </Stack>
  );
}

function DocCard({ d, onChange, reload }: { d: PolicyDoc; onChange: (d: PolicyDoc) => void; reload: () => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  const [rule, setRule] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [reading, setReading] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();

  async function step(name: string, fn: () => Promise<{ message?: string } & Partial<PolicyDoc> & { state?: PolicyDoc; reading?: string }>) {
    setBusy(name);
    try {
      const r = await fn();
      if (r.message) ok(r.message);
      const next = r.state || (r.doc ? (r as PolicyDoc) : null);
      if (next) onChange(next);
      return r;
    } catch (e) { fail(e); reload(); return null; } finally { setBusy(null); }
  }
  const save = async () => {
    if (draft === null) return;
    const r = await step("save", () => api.put(`/api/policy/${d.doc}`, { text: draft }));
    if (r) { setDraft(null); setReading(null); }
  };
  const compile = async () => {
    const r = await step("compile", () => api.post(`/api/policy/${d.doc}/compile`));
    if (r?.reading) setReading(r.reading);
  };
  const confirm = async () => { if (await step("confirm", () => api.post(`/api/policy/${d.doc}/confirm`))) setReading(null); };
  const discard = async () => {
    const yes = await ask({ title: "Discard the draft?", message: `The text before ${d.pending_by ? `${d.pending_by}'s` : "the"} draft comes back; what the bot applies doesn't change.`, confirmLabel: "Discard", color: "red" });
    if (yes && (await step("discard", () => api.post(`/api/policy/${d.doc}/discard`)))) setReading(null);
  };
  const append = async () => {
    if (!rule.trim()) return;
    if (await step("append", () => api.post(`/api/policy/${d.doc}/append`, { text: rule }))) setRule("");
  };
  const shownReading = reading || d.pending_reading;

  return (
    <Card id={d.doc}>
      {dialog}
      <CardHeader title={TITLE[d.doc] || d.doc} hint={HINT[d.doc]}
        action={<Group gap="xs">
          {d.pending && <Badge color="yellow" variant="light">draft by {d.pending_by || "an officer"}{d.pending_at ? ` · ${d.pending_at}` : ""}</Badge>}
          {draft === null && <Button size="xs" variant="default" leftSection={<IconPencil size={14} />} onClick={() => setDraft(d.text)}>Edit</Button>}
        </Group>} />
      {d.differs && <Box px="md" py={6} style={{ background: "var(--mantine-color-red-9)", borderBottom: "1px solid var(--mantine-color-red-7)" }}>
        <Text size="sm">The written text no longer reads the way the bot applies it — compile and confirm to apply it{d.pending ? `, or discard ${d.pending_by || "the"} draft` : ""}.</Text>
      </Box>}
      {d.legacy && !d.differs && <Text size="xs" c="dimmed" px="md" pt="xs">This reading was confirmed before readings recorded the text they were made from; the next confirm records it.</Text>}
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing={0}>
        <Box p="md" style={{ borderRight: "1px solid var(--mantine-color-slate-5)" }}>
          <Eyebrow>Written</Eyebrow>
          {draft === null ? (
            <Text size="sm" mt={6} style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{d.text.trim() || "(empty)"}</Text>
          ) : (
            <Stack gap="xs" mt={6}>
              <Textarea value={draft} onChange={(e) => setDraft(e.currentTarget.value)} autosize minRows={8} maxRows={30} description={`${draft.length} characters — no limit here (Discord's box holds ${d.modal_max})`} />
              <Group gap="xs" justify="flex-end">
                <Button size="xs" variant="default" onClick={() => setDraft(null)}>Cancel</Button>
                <Button size="xs" loading={busy === "save"} disabled={draft.trim() === d.text.trim()} onClick={save}>Save draft</Button>
              </Group>
            </Stack>
          )}
          {d.doc !== "persona" && draft === null && (
            <Group gap="xs" mt="md" align="flex-end" wrap="nowrap">
              <TextInput size="xs" style={{ flex: 1 }} label="Add a rule" placeholder={d.doc === "loot" ? "e.g. tanks get shield upgrades first" : "e.g. keep Innater with Boviche"} value={rule}
                onChange={(e) => setRule(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === "Enter") append(); }} maxLength={1000} />
              <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} loading={busy === "append"} disabled={!rule.trim()} onClick={append}>Add</Button>
            </Group>
          )}
        </Box>
        <Box p="md">
          <Eyebrow>{d.compiled ? `Applied (compiled ${d.compiled_label}${d.confirmed_by ? ` by ${d.confirmed_by}` : ""})` : "Applied"}</Eyebrow>
          {d.doc === "persona" ? (
            <Text size="sm" mt={6} style={{ whiteSpace: "pre-wrap" }}>{d.applied_text || "(none)"}</Text>
          ) : d.compiled ? (
            <Stack gap={4} mt={6}>
              {d.items.map((t, i) => <Text key={i} size="sm"><Text span c="dimmed" ff="monospace">{d.doc === "loot" ? `R${i + 1}` : `${i + 1}.`}</Text> {t}</Text>)}
              {d.summary && <Text size="xs" c="dimmed" fs="italic" mt={4}>{d.summary}</Text>}
            </Stack>
          ) : <Text size="sm" c="dimmed" mt={6}>No confirmed reading yet — compile the text and confirm the reading.</Text>}
        </Box>
      </SimpleGrid>
      {(d.pending || reading) && (
        <Box p="md" style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <Box style={{ minWidth: 0, flex: "1 1 320px" }}>
              <Eyebrow>{shownReading ? `The bot's reading of the draft${d.pending_compiled_label ? ` (compiled ${d.pending_compiled_label})` : ""}` : "Draft waiting"}</Eyebrow>
              {shownReading ? <Text size="sm" mt={6} style={{ whiteSpace: "pre-wrap" }}>{shownReading.replace(/\*\*/g, "").replace(/(^|\s)_(.+?)_(?=\s|$)/g, "$1$2")}</Text>
                : <Text size="sm" c="dimmed" mt={6}>Not compiled yet: compile it to see how the bot would read it.</Text>}
            </Box>
            <Group gap="xs">
              <Button size="xs" variant="default" loading={busy === "compile"} onClick={compile}>{shownReading ? "Compile again" : "Compile"}</Button>
              {shownReading && <Button size="xs" color="teal" loading={busy === "confirm"} onClick={confirm}>Confirm</Button>}
              {d.pending && <Button size="xs" variant="default" color="red" loading={busy === "discard"} onClick={discard}>Discard</Button>}
            </Group>
          </Group>
          {!shownReading && d.differs && <Alert mt="sm" color="gray" variant="light" p="xs"><Text size="xs">Nothing changes for the raid until a reading is confirmed.</Text></Alert>}
        </Box>
      )}
      {!d.pending && !reading && d.differs && (
        <Group p="md" pt={0} justify="flex-end"><Button size="xs" variant="default" loading={busy === "compile"} onClick={compile}>Compile the written text</Button></Group>
      )}
    </Card>
  );
}
