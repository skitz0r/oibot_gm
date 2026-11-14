import { useEffect, useState } from "react";
import { Button, Card, Code, Group, Stack, Text } from "@mantine/core";
import { api, ApiError, type MarketChoices, type MarketState } from "../api";
import { CardHeader, fail, ok } from "./Page";
import { useConfirm } from "./ConfirmModal";
import { ItemName } from "./ItemName";
import { useDirty } from "../hooks/useDirty";
import { useHeldLive } from "../hooks/useLive";

/** The server's line as a sentence: a capital first letter and a closing period (the words themselves are the server's). */
const asSentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) + (/[.!?]$/.test(s) ? "" : ".") : "");

/** Config › Settings, under the guild card (auction prices §4.1; shown when /api/meta.armory and prices.market — the
 *  version has auction houses and the host its keys): which auction house the guild's prices come from. Officers read it;
 *  the owner presses **Pick the auction house…** to see the guild's faction's houses that have listings, each with how
 *  much is listed and three well-known items' lowest prices to match against the game, then picks one — the server's
 *  sentences in the confirm before anything is written. **Turn prices off…** forgets the choice, with the server's
 *  sentences too. A stale page (409) reloads. Discord's /gm config armory and MCP market are the same verbs. */
export function MarketCard() {
  const [d, setD] = useState<MarketState | null>(null);
  const [gone, setGone] = useState(false);
  const [choices, setChoices] = useState<MarketChoices | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  useDirty("market", choices !== null);
  const load = () => api.get<MarketState>("/api/admin/market").then((r) => { setD(r); setGone(false); })
    .catch((e) => { if (e instanceof ApiError && e.status === 404) setGone(true); else fail(e); });
  useEffect(() => { load(); }, []);
  useHeldLive(load, ["config", "prices"], choices !== null || busy);
  if (gone) return null;  // this version has no auction houses
  if (!d) return <Card><Text c="dimmed" p="md">Loading…</Text></Card>;

  async function look() {
    setBusy(true); setProblem(null);
    try {
      setChoices(await api.post<MarketChoices>("/api/admin/market", { choices: true }));
    } catch (e) { setChoices(null); setProblem(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }

  async function pick(c: MarketChoices["choices"][number]) {
    if (!choices?.ruleset_id || !d) return;
    const ids = { house_id: c.house_id, ruleset_id: choices.ruleset_id };
    setBusy(true);
    try {
      const p = await api.post<{ sentences: string[]; rev: string }>("/api/admin/market", { ...ids, preview: true });
      if (!(await ask({ title: `Pick the ${c.name}?`, message: p.sentences.join(" "), confirmLabel: "Pick this auction house" }))) return;
      const r = await api.post<MarketState & { message: string }>("/api/admin/market", { ...ids, rev: d.rev });
      setD(r); setChoices(null);
      ok(r.message);
    } catch (e) {
      fail(e);
      if (e instanceof ApiError && e.status === 409) { setChoices(null); await load(); }
    } finally { setBusy(false); }
  }

  async function turnOff() {
    if (!d?.selected) return;
    if (!(await ask({ title: "Turn prices off?", message: d.clear_sentences.join(" "), confirmLabel: "Turn prices off", color: "red" }))) return;
    setBusy(true);
    try {
      const r = await api.post<MarketState & { message: string }>("/api/admin/market", { clear: true, rev: d.rev });
      setD(r); setChoices(null);
      ok(r.message);
    } catch (e) { fail(e); await load(); } finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader title="The auction house" hint={d.owner ? "owner" : "the owner picks it"} />
      <Stack p="md" gap="md">
        <div>
          <Text size="sm" fw={d.selected ? 600 : undefined}>{d.selected ? d.selected.name : asSentence(d.words)}</Text>
          {d.selected && <Text size="xs" c="dimmed">{d.selected.faction} · picked by {d.selected.picked_by} on {d.selected.picked_label}</Text>}
          {d.selected && d.prices && <Text size="xs" c="dimmed">{d.prices}</Text>}
        </div>
        {d.owner && !choices && (
          <Group gap="sm" wrap="wrap">
            <Button size="xs" variant="light" loading={busy} onClick={look}>{d.selected ? "Pick another auction house…" : "Pick the auction house…"}</Button>
            {d.selected && <Button size="xs" variant="light" color="red" disabled={busy} onClick={turnOff}>Turn prices off…</Button>}
          </Group>
        )}
        {problem && <Text size="sm" c="yellow.4">{problem}</Text>}
        {choices && (
          <Stack gap="sm">
            {choices.choices.length === 0 ? <Text size="sm">{choices.words}</Text> : (
              <Text size="sm">Match the prices against the game to find the auction house the guild trades on.</Text>
            )}
            {choices.choices.map((c) => (
              <Card key={c.house_id} withBorder p="sm">
                <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
                  <Stack gap={2} style={{ minWidth: 0, flex: "1 1 220px" }}>
                    <Text size="sm" fw={600}>{c.name}{d.selected?.house_id === c.house_id ? " · the guild's now" : ""}</Text>
                    <Text size="xs" c="dimmed">{c.listings_words}</Text>
                    {c.samples.map((s) => (
                      <Text key={s.id} size="xs"><ItemName id={s.id} name={s.name} /> · {s.low_words}</Text>
                    ))}
                  </Stack>
                  <Button size="xs" disabled={busy} onClick={() => pick(c)}>Pick this one…</Button>
                </Group>
              </Card>
            ))}
            <Group justify="flex-end"><Button size="xs" variant="default" onClick={() => setChoices(null)}>Cancel</Button></Group>
          </Stack>
        )}
        <Text size="xs" c="dimmed">In Discord: <Code>/gm config armory</Code>.</Text>
      </Stack>
      {confirmDialog}
    </Card>
  );
}
