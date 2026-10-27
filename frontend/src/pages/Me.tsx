import { useEffect, useMemo, useState } from "react";
import { Badge, Box, Button, Card, Group, Select, Stack, Switch, Tabs, Text, Title } from "@mantine/core";
import { useSearchParams } from "react-router-dom";
import css from "./me.module.css";
import { api, type Me, type Meta, type MyRegular, type MySheet } from "../api";
import { AskCard, NoticesCard } from "../components/AskCard";
import type { ConfirmOpts, ConfirmResult } from "../components/ConfirmModal";
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
  usePoll(() => { if (!editing) load(); });
  useLive(() => { if (!editing) load(); }, ["run:", "member"]);

  async function stepDown(r: MyRegular) {
    if (!(await ask({ title: `Step down from ${r.label}?`, message: `You stop being one of its regulars (${r.character}): the bot no longer expects your answer or reminds you. You can still Join any of its sheets. To come back, ask an officer.`, confirmLabel: "Step down", color: "red" }))) return;
    try { const x = await api.post<{ message: string }>("/api/me/regular/remove", { series: r.series }); ok(x.message); load(); } catch (e) { fail(e); }
  }
  async function setDm(on: boolean) { try { await api.post("/api/me/dm", { on }); ok(on ? "DMs on" : "DMs off"); load(); } catch (e) { fail(e); } }
  const summary = useMemo(() => {
    if (!me) return "";
    const n = me.characters.length;
    const sheet = me.sheets.find((s) => s.rostered) || me.sheets.find((s) => s.status === "in");
    return [`${n} character${n === 1 ? "" : "s"}`, me.roles.primary || null, sheet ? `${sheet.raid} · ${sheet.when} — ${sheet.rostered ? "you're rostered" : "joined"}` : null].filter(Boolean).join(" · ");
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
          <Tabs.Panel value="guide"><GuideTools meta={meta} /></Tabs.Panel>
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
          <Card>
            <CardHeader title="My sheets" />
            <Stack p="md" gap="xs">
              {me.sheets.length === 0 && <Text c="dimmed" size="sm">No live raids.</Text>}
              {me.sheets.map((s) => <SheetRow key={s.key} s={s} me={me} meta={meta} ask={ask} onDone={load} />)}
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

/** One live sheet, answerable here (design B1): Join · Bench · No thanks while the sheet is open (the current answer filled), a
 *  character select when you have more than one, and — on a locked run you joined or are rostered on — "Can't make it…" with an
 *  optional note for the officers. Rendered from the server's state only: every button is disabled while a press is in flight,
 *  nothing flips before the reply, the reply's receipt is the toast and the page reloads (two devices: the last press wins). An
 *  ad hoc run you joined asks once before a change away from Join (the queue). */
function SheetRow({ s, me, meta, ask, onDone }: { s: MySheet; me: Me; meta: Meta; ask: (o: ConfirmOpts) => Promise<ConfirmResult>; onDone: () => void }) {
  const chars = me.characters;  // the active ones (planned by label), main first on the server
  const main = chars.find((c) => c.is_main) || chars[0];
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const character = pick || s.character || main?.label || null;
  async function answer(status: string) {
    if (s.queue_note && status !== "in" && !(await ask({ title: `${meta.labels[status] || status} on ${s.raid}?`, message: s.queue_note, confirmLabel: meta.labels[status] || status }))) return;
    setBusy(status);
    try {
      const r = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/answer`, { status, character: chars.length > 1 ? character : null });
      ok(r.message);
    } catch (e) { fail(e); } finally { setBusy(null); setPick(null); onDone(); }
  }
  async function cant() {
    const r = await ask({ title: `Can't make ${s.raid} · ${s.when}?`, message: s.rostered ? "Your seat is freed and the bench is asked; the officers see it as a callout." : "You go to No thanks and the officers see it as a callout.",
      confirmLabel: "Can't make it", color: "red", note: { label: "Note for the officers (optional — never on the public sheet)", maxLength: 300 } });
    if (!r) return;
    setBusy("cant");
    try {
      const x = await api.post<{ message: string }>(`/api/me/run/${encodeURIComponent(s.key)}/out`, { note: typeof r === "object" ? r.note : "" });
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); onDone(); }
  }
  return (
    <Stack gap={4} py={4} style={{ borderBottom: "1px solid var(--mantine-color-slate-6)" }}>
      <Group justify="space-between" wrap="nowrap">
        <Box style={{ minWidth: 0 }}><Text size="sm" fw={600} truncate>{s.raid}{s.kind === "adhoc" ? " · ad hoc" : ""}</Text><Text size="xs" c="dimmed">{s.when}{s.state_word ? ` · ${s.state_word}` : ""}{s.character ? ` · ${s.character}` : ""}{s.regular && !s.status ? " · you're a regular: answer it" : ""}</Text></Box>
        {/* the badge's words are the server's (rc.answer_word): "not rostered" for a joiner the lock left out, never "Join" on a locked run */}
        <Badge variant="light" tt="none" color={s.rostered ? "teal" : s.state !== "open" && s.status === "in" ? "gray" : s.status === "in" ? "teal" : s.status === "out" ? "red" : s.status === "sub" ? "yellow" : "gray"}>{s.badge || s.label || "not answered"}</Badge>
      </Group>
      {s.may_answer && (
        <Group gap={6} wrap="wrap">
          {chars.length > 1 && (
            <Select size="xs" w={150} aria-label="as which character" allowDeselect={false} value={character} disabled={busy !== null}
              data={chars.map((c) => ({ value: c.label, label: `${c.label}${c.is_main ? " (main)" : ""}` }))} onChange={(v) => setPick(v)} />
          )}
          {meta.statuses.map((st) => (
            <Button key={st} size="compact-sm" variant={s.status === st && (!pick || pick === s.character) ? "filled" : "default"} color={st === "out" ? "red" : st === "sub" ? "yellow" : "teal"}
              loading={busy === st} disabled={busy !== null || (s.status === st && (!pick || pick === s.character))} onClick={() => answer(st)}>{meta.labels[st] || st}</Button>
          ))}
        </Group>
      )}
      {s.may_cant && <Group gap={6}><Button size="compact-sm" variant="subtle" color="red" loading={busy === "cant"} disabled={busy !== null} onClick={cant}>Can't make it…</Button></Group>}
      {s.receipt && s.status && <Text size="xs" c="dimmed">{s.receipt}</Text>}
    </Stack>
  );
}

