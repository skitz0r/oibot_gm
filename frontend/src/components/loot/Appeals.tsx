import { useEffect, useState } from "react";
import { Anchor, Badge, Blockquote, Button, Card, Group, Select, Stack, Text } from "@mantine/core";
import { api, type Meta, type OfficerAppeal } from "../../api";
import { CardHeader, fail, ok } from "../Page";
import { useConfirm } from "../ConfirmModal";
import { useLive } from "../../hooks/useLive";
import { SourceBadge, Who } from "./shared";

interface AppealsData { open: OfficerAppeal[]; resolved: OfficerAppeal[]; outcomes: { value: string; label: string; text: string }[]; note_max: number }

/** Loot › Appeals (officers, L32): members' questions about their own awards — the award as the log shows it, the
 *  member's words (data, shown as written), and the council's record of the award (how it was decided, the bot's
 *  pick, the reason, the candidates: officer data, D2). Resolve = one outcome from the fixed choice; the confirm prints
 *  the server's consequence sentence and takes an optional note that stays with the officers. The member hears the
 *  outcome as a notice on their Me page — never a DM. The same question has a card and thread in the officer channel. */
export function Appeals({ meta }: { meta: Meta }) {
  const [data, setData] = useState<AppealsData | null>(null);
  const [pick, setPick] = useState<Record<string, string | null>>({});
  const [ask, dialog] = useConfirm();
  const load = () => api.get<AppealsData>("/api/loot/appeals").then(setData).catch(fail);
  useEffect(() => { load(); }, []);
  useLive(load, ["loot:"]);
  async function resolve(a: OfficerAppeal) {
    const outcome = pick[a.id];
    if (!outcome || !data) return;
    const r = await ask({ title: `Resolve: ${a.member} · ${a.item}`, message: a.consequences?.[outcome] || "", confirmLabel: "Resolve",
      note: { label: "Note for the officers (optional)", maxLength: data.note_max, placeholder: "kept on the question; the member never sees it" } });
    if (!r) return;
    try {
      const res = await api.post<{ message: string }>(`/api/loot/appeals/${a.id}/resolve`, { outcome, note: typeof r === "object" ? r.note || undefined : undefined });
      ok(res.message); load();
    } catch (e) { fail(e); }
  }
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const one = (a: OfficerAppeal) => (
    <Card key={a.id} withBorder>
      <Stack gap="xs" p="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
            <Who meta={meta} name={a.character} cls={a.cls} spec={a.spec} />
            <Text size="sm" fw={600} truncate>{a.item}</Text>
            <Text size="xs" c="dimmed">{a.date_label}{a.boss ? ` · ${a.boss}` : ""} · {a.offspec ? "OS" : "MS"}</Text>
          </Group>
          <Group gap={6}>
            {a.link && <Anchor size="xs" href={a.link} target="_blank" rel="noopener">thread</Anchor>}
            <Text size="xs" c="dimmed">{a.member} asked {a.asked_label}</Text>
          </Group>
        </Group>
        <Blockquote p="xs" style={{ whiteSpace: "pre-wrap" }}><Text size="sm">{a.text}</Text></Blockquote>
        {a.details.found ? (
          <Group gap="xs" wrap="wrap">
            <SourceBadge source={a.details.source} />
            {a.details.void && <Badge size="xs" color="red" variant="light">corrected since</Badge>}
            {a.details.bot && a.details.bot !== a.character && <Text size="xs" c="dimmed">bot picked {a.details.bot}</Text>}
            {a.details.by && <Text size="xs" c="dimmed">recorded by {a.details.by}{a.details.confirmed_label ? ` · ${a.details.confirmed_label}` : ""}</Text>}
            <Text size="xs" c="dimmed">tier {a.details.tier} · weight {a.details.weight}</Text>
            {a.details.reason && <Text size="xs">reason: {a.details.reason}</Text>}
            {!!a.details.candidates?.length && <Text size="xs" c="dimmed">candidates then: {a.details.candidates.map((c) => `${c.character} (${c.tier ?? "?"})`).join(", ")}</Text>}
          </Group>
        ) : <Text size="xs" c="dimmed">The ledger has no record of this award any more.</Text>}
        {a.state === "open" ? (
          <Group gap="xs" wrap="wrap">
            <Select size="xs" placeholder="Pick the outcome…" w={240} data={data.outcomes.map((o) => ({ value: o.value, label: o.label }))} value={pick[a.id] || null}
              onChange={(v) => setPick({ ...pick, [a.id]: v })} />
            <Button size="xs" disabled={!pick[a.id]} onClick={() => resolve(a)}>Resolve…</Button>
            {pick[a.id] && <Text size="xs" c="dimmed">The member reads: “{data.outcomes.find((o) => o.value === pick[a.id])?.text}”</Text>}
          </Group>
        ) : (
          <Text size="xs" c="dimmed">✅ {a.outcome_label} · {a.resolved_by} · {a.resolved_label}{a.note ? ` — ${a.note}` : ""}</Text>
        )}
      </Stack>
    </Card>
  );
  return (
    <Stack gap="lg">
      <Card>
        <CardHeader title="Open questions" hint="oldest first · members ask from My loot or /me loot-ask" />
        <Stack p="md" gap="sm">
          {data.open.length === 0 && <Text size="sm" c="dimmed">No open questions.</Text>}
          {data.open.map(one)}
        </Stack>
      </Card>
      <Card>
        <CardHeader title="Resolved" hint="newest first" />
        <Stack p="md" gap="sm">
          {data.resolved.length === 0 && <Text size="sm" c="dimmed">Nothing resolved yet.</Text>}
          {data.resolved.map(one)}
        </Stack>
      </Card>
      {dialog}
    </Stack>
  );
}
