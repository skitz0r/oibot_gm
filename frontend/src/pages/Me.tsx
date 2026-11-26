import { useEffect, useMemo, useState } from "react";
import { Box, Button, Card, Group, Stack, Switch, Tabs, Text, Title } from "@mantine/core";
import { useSearchParams } from "react-router-dom";
import css from "./me.module.css";
import { api, type Me, type Meta, type MyRegular } from "../api";
import { AskCard, NoticesCard } from "../components/AskCard";
import { GameIcon } from "../components/Icons";
import { classColour } from "../theme";
import { CardHeader, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { usePoll } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { MyLootCard, WishlistCard } from "../components/loot/MyLoot";
import { CharacterTable } from "../components/CharacterTable";
import { AbsencesCard } from "../components/AbsencesCard";
import { AttendanceCard } from "../components/AttendanceCard";
import { MyApplication } from "../components/MyApplication";
import { GuideTools } from "../components/GuideTools";
import { GuideLibrary } from "../components/GuideLibrary";
import { MyMemories } from "../components/MyMemories";
import { MyAnswer } from "../components/MyAnswer";


/** Me: the member's own record and inbox, and — when the game version has the tool (meta.guides, §5.55) — a second tab,
 *  Leveling guide, where they unlock their own RestedXP guide. The tab is the URL's ?tab= so a link can open it. */
export function MePage({ meta }: { meta: Meta }) {
  const [params, setParams] = useSearchParams();
  const tab = meta.guides && params.get("tab") === "guide" ? "guide" : "me";
  const go = (t: string) => setParams((p) => { const n = new URLSearchParams(p); if (t === "me") n.delete("tab"); else n.set("tab", t); return n; }, { replace: true });
  const [me, setMe] = useState<Me | null>(null);
  const [editing, setEditing] = useState(false);
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Me>("/api/me").then(setMe).catch(fail);
  useEffect(() => { load(); }, []);
  // deep link from a Discord sheet's title: /app/me#run-<key> scrolls to that run's row ONCE it has rendered (below the
  // phone header: scrollMarginTop on the row), lights the row for a moment, and drops the hash so the next reload (the
  // poll, a live event) does not jump back there
  const [flash, setFlash] = useState<string | null>(null);
  // the hash names an event with no row here (finished or cancelled): one sentence under My sheets says so
  const [over, setOver] = useState(false);
  useEffect(() => {
    if (!me || !window.location.hash.startsWith("#run-")) return;
    const key = window.location.hash.slice(5);
    const t = setTimeout(() => {
      const row = document.getElementById(`run-${key}`);
      row?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
      if (!row) { setOver(true); return; }
      setFlash(key);
      setTimeout(() => setFlash(null), 3000);
    }, 150);
    return () => clearTimeout(t);
  }, [me]);
  usePoll(() => { if (!editing) load(); });
  useLive(() => { if (!editing) load(); }, ["run:", "member", "memory"]);

  async function stepDown(r: MyRegular) {
    if (!(await ask({ title: `Step down from ${r.label}?`, message: `You stop being one of its regulars (${r.character}): the bot no longer expects your answer or reminds you. You can still Join any of its sheets. To come back, ask an officer.`, confirmLabel: "Step down", color: "red" }))) return;
    try { const x = await api.post<{ message: string }>("/api/me/regular/remove", { series: r.series }); ok(x.message); load(); } catch (e) { fail(e); }
  }
  async function setDm(on: boolean) { try { await api.post("/api/me/dm", { on }); ok(on ? "DMs on" : "DMs off"); load(); } catch (e) { fail(e); } }
  // one line: where you stand on your next raid, else what the page is for (finding 33: no dot chain of counts)
  const summary = useMemo(() => {
    if (!me) return "";
    const sheet = me.sheets.find((s) => s.rostered) || me.sheets.find((s) => s.status === "in");
    if (sheet) return `${sheet.rostered ? "You're rostered for" : "You joined"} ${sheet.raid} on ${sheet.when}.`;
    return "Your characters, your raids and anything waiting for your answer.";
  }, [me]);

  if (!me) return <Text c="dimmed">Loading…</Text>;
  const header = (
    <Group justify="space-between" align="flex-start">
      <Box><Title order={1} size="h2">{me.display_name}</Title><Text c="dimmed" size="sm" mt={4}>{summary}</Text></Box>
    </Group>
  );
  if (meta.guides) {
    return (
      <Stack gap="lg">
        {header}
        <Tabs value={tab} onChange={(v) => v && go(v)} keepMounted={false}>
          <Tabs.List mb="md">
            <Tabs.Tab value="me">My record</Tabs.Tab>
            <Tabs.Tab value="guide">Leveling guide</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="me">{record(me)}</Tabs.Panel>
          <Tabs.Panel value="guide"><Stack gap="lg"><GuideLibrary /><GuideTools meta={meta} /></Stack></Tabs.Panel>
        </Tabs>
        {confirmDialog}
      </Stack>
    );
  }
  return <Stack gap="lg">{header}{record(me)}{confirmDialog}</Stack>;

  function record(me: Me) {
    return (
    <Stack gap="lg">
      <MyApplication application={me.application} registered={me.characters.length > 0} />
      {me.must_apply && !(me.application && me.application.state === "open") && (
        <Card withBorder>
          <Text fw={700}>New here? Apply in Discord first.</Text>
          <Text size="sm" c="dimmed" mt={4}>Press <b>Apply to join</b> on the registration card, or use <code>/apply</code>. Once an officer accepts you, your character shows up here.</Text>
        </Card>
      )}

      {/* the inbox (design B2 / D26): every open ask wherever it waits, then the notices — outcomes are told here, never by DM */}
      {me.asks.map((a) => <AskCard key={`${a.kind}:${a.id}`} a={a} meta={meta} onDone={load} />)}
      <NoticesCard notices={me.notices || []} onDone={load} />

      <div className={css.layout}>
        <Stack gap="lg" style={{ minWidth: 0 }}>
          <CharacterTable meta={meta} me={me} onChanged={load} onEditing={setEditing} />
          {!me.must_apply && me.characters.length > 0 && <WishlistCard meta={meta} />}
        </Stack>

        <Stack gap="lg" style={{ minWidth: 0 }}>
          {(me.regulars || []).length > 0 && (
            <Card>
              <CardHeader title="My regular raids" hint="you're expected on every sheet of these" />
              <Stack p="md" gap="xs">
                {me.regulars!.map((r) => (
                  <Group key={r.series} justify="space-between" wrap="nowrap">
                    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                      <GameIcon meta={meta} kind="spec" id={`${r.cls}:${r.spec}`} size={20} title={`${r.spec} · ${r.role}`} />
                      <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{r.label}</Text><Text size="xs" c={classColour(meta, r.cls)}>{r.character}</Text></Box>
                    </Group>
                    <Button size="xs" variant="subtle" color="red" onClick={() => stepDown(r)}>Step down</Button>
                  </Group>
                ))}
                <Text size="xs" c="dimmed">Nothing is answered for you: press Join or No thanks on each sheet (an absence marks you away). Want to become a regular somewhere? Ask an officer.</Text>
              </Stack>
            </Card>
          )}
          {me.memory_on && <MyMemories page={me.memories || { rows: [], total: 0, more: false }} onChanged={load} />}
          <Card>
            <CardHeader title="My sheets" />
            <Stack p="md" gap="xs">
              {over && <Text size="sm">{meta.page_words.me_event_over}{!me.must_apply && me.characters.length > 0 ? ` ${meta.page_words.me_event_over_tail}` : ""}</Text>}
              {me.sheets.length === 0 && <Text c="dimmed" size="sm">No live raids.</Text>}
              {me.sheets.map((s) => <MyAnswer key={s.key} s={s} characters={me.characters} meta={meta} ask={ask} onDone={load} flash={flash === s.key} link />)}
              {me.sheets.length > 0 && <Text size="xs" c="dimmed">Answer here or on the sheet in Discord — the same answer either way. Once the roster locks you confirm {me.dm ? "by DM or here" : "here — your DMs are off"}.</Text>}
            </Stack>
          </Card>
          {!me.must_apply && <MyLootCard meta={meta} />}
          {!me.must_apply && me.characters.length > 0 && <AttendanceCard meta={meta} />}
          <AbsencesCard meta={meta} absences={me.absences} onChanged={load} />
          <Card>
            <CardHeader title="Direct messages" action={<Switch checked={me.dm} onChange={(e) => setDm(e.currentTarget.checked)} />} />
            <Text p="md" size="sm" c="dimmed">{(meta.dm_sentences ? (me.dm ? meta.dm_sentences.on : meta.dm_sentences.off) : me.dm_sentence) || ""}</Text>
          </Card>
        </Stack>
      </div>
    </Stack>
    );
  }
}
