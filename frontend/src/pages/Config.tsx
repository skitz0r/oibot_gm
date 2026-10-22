import { useRefresh } from "../hooks/usePoll";
import { useLive } from "../hooks/useLive";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Accordion, Alert, Anchor, Avatar, Badge, Box, Button, Card, Code, Group, MultiSelect, SegmentedControl, Select, SimpleGrid, Stack, Switch, Tabs, TagsInput, Text, Textarea, Title } from "@mantine/core";
import { IconPencil, IconRefresh } from "@tabler/icons-react";
import { api, opsApi, type ConfigSummary, type LootLogState, type Meta, type OpsReply, type RoleRef } from "../api";
import { CardHeader, PageTitle, fail, ok } from "../components/Page";
import { useConfirm } from "../components/ConfirmModal";
import { useOpsSave } from "../components/ReviewModal";
import { useDirty } from "../hooks/useDirty";
import { Recruitment } from "../components/Recruitment";
import { ChannelsCard } from "../components/ChannelsCard";
import { SetupCard } from "../components/SetupCard";

interface ConfigData {
  yaml: string; docs: Record<string, { text: string; compiled: boolean; summary: string | null }>;
  channels: Record<string, { id: string | null; name: string | null }>; guild_channels: { id: string; name: string; category: string | null }[]; guild_roles: RoleRef[];
  /** officer_roles: by role id (renames are safe); officer_roles_pending: legacy names the bot could not match to a role */
  settings: { timezone: string; ask_audience: string; about: string; officer_roles: RoleRef[]; officer_roles_pending: string[]; owner_id: string | null; news_keywords: string[];
    /** the public loot log: on/off, and its state in words (off · a channel · on with no channel) */
    loot_log: boolean; loot_log_state: LootLogState };
  test_bench: { members: number; runs: string[] }; owner: boolean;
  revs?: Record<string, string>;
}
interface NewsData { items: { ingested_label: string; title?: string }[]; keywords: Record<string, string[]>; channel_id: string | null }
interface PublicInfo { name: string; about: string | null; invite_url: string | null; recruiting: boolean; icon: string | null; key?: string }

/** who may ask, narrowest first: moving right widens the audience (and the share of the model budget) */
const AUDIENCES = ["officers", "confirmed", "registered", "everyone"];

type SettingsDraft = { timezone: string; ask_audience: string; about: string; officer_roles: string[] };
const FIELD_WORDS: Record<keyof SettingsDraft, string> = { timezone: "Timezone", ask_audience: "Who may ask", about: "About the guild", officer_roles: "Officer roles" };
const same = (a: unknown, b: unknown) => JSON.stringify(Array.isArray(a) ? [...a].sort() : a) === JSON.stringify(Array.isArray(b) ? [...b].sort() : b);

export function ConfigPage({ meta }: { meta: Meta }) {
  const [data, setData] = useState<ConfigData | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>("overview");
  const [summary, setSummary] = useState<ConfigSummary | null>(null);
  const [draft, setDraft] = useState<SettingsDraft | null>(null);  // the Settings card's edit mode: one Save
  const [stale, setStale] = useState(false);  // someone else changed the settings while the draft was open
  const draftRef = useRef(false);
  draftRef.current = draft !== null;
  const [saveSetting, settingReview] = useOpsSave();
  useDirty("settings", draft !== null);
  const load = () => Promise.all([
    api.get<ConfigData>("/api/config").then(setData),
    api.get<ConfigSummary>("/api/config/summary").then(setSummary),
  ]).catch(fail);
  useEffect(() => { load(); }, []);
  useRefresh(load);
  // a change made elsewhere shows at once — never under an open draft: the draft is told instead (design §2.4)
  useLive(() => { if (draftRef.current) setStale(true); else load(); }, ["config"]);
  if (!data) return <Text c="dimmed">Loading…</Text>;
  const ro = !data.owner;
  /** One setting through the structured path (useOpsSave → the ReviewModal when it is reviewed, D51). */
  async function set(field: string, value: unknown) {
    setBusy(field);
    try {
      const r = await saveSetting([{ kind: "settings", fields: { [field]: value } }], { revs: data?.revs?.settings ? { "config:settings": data.revs.settings } : undefined });
      await load();
      if (r && r.applied?.length && !r.refused?.length) { setSaved(field); setTimeout(() => setSaved((f) => (f === field ? null : f)), 3000); }
    } catch (e) { fail(e); } finally { setBusy(null); }
  }
  /** The loot-log switch saves as you change it; the mark beside the label says it was just written. */
  const lab = (field: string, text: string) => <>{text}{saved === field && <Text span size="xs" c="teal" ml={8}>✓ saved</Text>}</>;
  return (
    <Stack gap="lg">
      <PageTitle title={meta.guilds.length > 1 ? `Configuration · ${meta.guild}` : "Configuration"} intro={ro ? "Read-only for officers; the owner edits here, with /gm config, or in plain text via /gm change or an @mention in the ops channel." : "Each function of the bot lives in a channel. Changing a channel does what the slash command does: the registration and absence cards are posted, the analytics cards re-post."} />
      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="overview">Overview</Tabs.Tab>
          <Tabs.Tab value="settings">Settings</Tabs.Tab>
          <Tabs.Tab value="channels">Channels</Tabs.Tab>
          <Tabs.Tab value="more">Recruitment and more</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="overview" pt="md">
          <Stack gap="lg">
            <SetupCard />
            <Overview s={summary} />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="settings" pt="md">
          <SettingsCard meta={meta} data={data} draft={draft} setDraft={setDraft} stale={stale}
            onReload={async () => { setStale(false); await load(); }} onSaved={async () => { setDraft(null); setStale(false); await load(); }} />
        </Tabs.Panel>

        <Tabs.Panel value="channels" pt="md">
          <Stack gap="lg">
            <ChannelsCard />
            <NewsCard data={data} ro={ro} onSaved={load} />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="more" pt="md">
          <Stack gap="lg">
            <Card>
              <CardHeader title="Loot log" hint="the public award log" action={<Switch label={lab("loot_log", "on")} checked={data.settings.loot_log} disabled={ro || busy === "loot_log"} onChange={(e) => set("loot_log", e.currentTarget.checked)} />} />
              <Box p="md">
                <Alert color={data.settings.loot_log_state.state === "no_channel" ? "yellow" : data.settings.loot_log_state.state === "off" ? "gray" : "teal"} py="xs">{data.settings.loot_log_state.text}</Alert>
                <Text size="xs" c="dimmed" mt="xs">The channel is picked under Channels (Loot log). Every line carries an "Add to my wishlist" button; members see the whole log on the site (logged in) and their own loot on the Me page.</Text>
              </Box>
            </Card>

            <Recruitment channels={data.guild_channels} />

            <Card>
              <CardHeader title="Test bench" hint="rehearse the whole cycle with puppet members" action={data.test_bench.members > 0 ? <Badge color="yellow" variant="light">{data.test_bench.members} test members · {data.test_bench.runs.length} test run{data.test_bench.runs.length === 1 ? "" : "s"}</Badge> : <Badge color="gray" variant="outline">idle</Badge>} />
              <Box p="md">
                <Text size="sm">The test bench lives on the Members page: seed or compose puppets, open a test run, answer as them and follow the live feed. <Anchor component={Link} to="/members#test-bench">Open the test bench</Anchor> · in Discord: <Code>/gm test</Code>.</Text>
              </Box>
            </Card>

            <Text size="sm" c="dimmed">The loot policy, the standing comp instructions and the persona have their own page: <Anchor component={Link} to="/policy">Policy</Anchor>.</Text>

            <Accordion variant="contained">
              <Accordion.Item value="yaml">
                <Accordion.Control>As stored (advanced) — guild.yaml in machine form</Accordion.Control>
                <Accordion.Panel><Box style={{ overflowX: "auto" }}><Code block p="md" style={{ background: "transparent", fontSize: 12.5 }}>{data.yaml}</Code></Box></Accordion.Panel>
              </Accordion.Item>
            </Accordion>
          </Stack>
        </Tabs.Panel>
      </Tabs>
      {settingReview}
    </Stack>
  );
}

/** The effective configuration in words (configops.config_summary — the same sections /gm config show prints). */
function Overview({ s }: { s: ConfigSummary | null }) {
  if (!s) return <Text c="dimmed">Loading…</Text>;
  return (
    <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
      {s.sections.map((x) => (
        <Card key={x.key}>
          <CardHeader title={x.title} hint={x.key === "host" && s.audience === "owner" ? "only you see this" : undefined} />
          <Stack gap={4} p="md">{x.lines.map((ln, i) => <Text key={i} size="sm" style={{ overflowWrap: "anywhere" }}>{ln}</Text>)}</Stack>
        </Card>
      ))}
    </SimpleGrid>
  );
}

/** Settings edit mode (design §3.5 O-14): timezone PICKED from a searchable list grouped by region (never a typed IANA name)
 *  with the server's preview of every raid's next run and the runs already open under it; who may ask (widening asks
 *  first); officer roles; the about text with its counter and the homepage card as it will look. One Save through the
 *  structured path (useOpsSave): a timezone or officer-role change is reviewed with its consequences, the rest saves. */
function SettingsCard({ meta, data, draft, setDraft, stale, onReload, onSaved }: { meta: Meta; data: ConfigData; draft: SettingsDraft | null; setDraft: (d: SettingsDraft | null) => void; stale: boolean; onReload: () => Promise<void>; onSaved: () => Promise<void> }) {
  const [save, review] = useOpsSave();
  const [ask, confirmDialog] = useConfirm();
  const [busy, setBusy] = useState(false);
  const [tzPreview, setTzPreview] = useState<OpsReply | null>(null);
  const [pub, setPub] = useState<PublicInfo | null>(null);
  const st = data.settings;
  const ro = !data.owner;
  const aboutMax = meta.about_max || 600;
  const base: SettingsDraft = { timezone: st.timezone, ask_audience: st.ask_audience, about: st.about || "", officer_roles: st.officer_roles.map((r) => r.id) };
  const d = draft;
  // a reload while editing re-bases the draft: a field nobody touched here takes the new stored value; a field edited here
  // AND changed by someone else is a conflict the owner settles field by field (Keep mine / Take theirs) before Save —
  // never a whole-draft overwrite either way (design §2.4)
  const startRef = useRef<SettingsDraft | null>(null);
  const [conflicts, setConflicts] = useState<(keyof SettingsDraft)[]>([]);
  useEffect(() => {
    const s0 = startRef.current;
    if (!d || !s0) return;
    const nd = { ...d };
    let moved = false;
    const clash: (keyof SettingsDraft)[] = [];
    for (const k of Object.keys(base) as (keyof SettingsDraft)[]) {
      const mine = !same(d[k], s0[k]), theirs = !same(base[k], s0[k]);
      if (!mine && theirs) { (nd as Record<string, unknown>)[k] = base[k]; moved = true; }
      else if (mine && theirs && !same(d[k], base[k])) clash.push(k);
    }
    startRef.current = base;
    setConflicts((c) => [...new Set([...c, ...clash])]);
    if (moved) setDraft(nd);
  }, [data]);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {  // the homepage card's facts (name, icon, invite) for the about preview
    if (!d || pub) return;
    api.get<PublicInfo | { guilds: PublicInfo[] }>("/api/public").then((x) => setPub("guilds" in x ? x.guilds.find((g) => g.key === meta.guild_key) || null : x)).catch(() => setPub(null));
  }, [d !== null]);  // eslint-disable-line react-hooks/exhaustive-deps

  // what a timezone change does with the runs already open or locked (Registry.timezone_change_effects): keep them at their
  // real moment, or move the open ones with the clock — the Save carries the pick (timezone_runs); a locked run never moves
  const [tzRuns, setTzRuns] = useState<"keep" | "move">("keep");
  const tzSeq = useRef(0);
  useEffect(() => {  // the timezone's preview from the server: rc.timezone_preview rows (notes) + Registry.timezone_change_effects (consequences)
    if (!d || d.timezone === st.timezone) { setTzPreview(null); return; }
    const n = ++tzSeq.current;
    opsApi.preview([{ kind: "settings", fields: { timezone: d.timezone, timezone_runs: tzRuns } }]).then((x) => { if (n === tzSeq.current) setTzPreview(x); }).catch(() => { if (n === tzSeq.current) setTzPreview(null); });
  }, [d?.timezone, tzRuns]);  // eslint-disable-line react-hooks/exhaustive-deps

  const known = new Set(data.guild_roles.map((r) => r.id));
  const roleOptions = [...data.guild_roles.map((r) => ({ value: r.id, label: r.name })), ...st.officer_roles.filter((r) => !known.has(r.id)).map((r) => ({ value: r.id, label: `${r.name} (deleted role)` }))];
  const tzLabel = (z: string) => meta.timezones?.flatMap((g) => g.items).find((i) => i.value === z)?.label || z;
  const roleName = (id: string) => roleOptions.find((o) => o.value === id)?.label || id;
  /** a draft value in words, for the conflict lines */
  const show = (k: keyof SettingsDraft, v: SettingsDraft[keyof SettingsDraft]) => k === "timezone" ? tzLabel(String(v))
    : k === "officer_roles" ? ((v as string[]).map((x) => `@${roleName(x)}`).join(", ") || "none")
    : k === "about" ? (String(v).trim() ? `“${String(v).trim().slice(0, 60)}${String(v).trim().length > 60 ? "…" : ""}”` : "empty") : String(v);

  function changes(): Record<string, unknown> {
    if (!d) return {};
    const f: Record<string, unknown> = {};
    if (d.timezone !== base.timezone) { f.timezone = d.timezone; if (tzPreview?.described?.consequences?.length) f.timezone_runs = tzRuns; }
    if (d.ask_audience !== base.ask_audience) f.ask_audience = d.ask_audience;
    if (d.about.trim() !== base.about.trim()) f.about = d.about.trim();
    if ([...d.officer_roles].sort().join(",") !== [...base.officer_roles].sort().join(",")) f.officer_roles = d.officer_roles;
    return f;
  }

  async function submit() {
    if (conflicts.length) return;  // settle each clash first (Keep mine / Take theirs)
    const f = changes();
    if (!Object.keys(f).length) { setDraft(null); ok("nothing changed"); return; }
    if (f.ask_audience && AUDIENCES.indexOf(String(f.ask_audience)) > AUDIENCES.indexOf(base.ask_audience)) {
      if (!(await ask({ title: `Let ${f.ask_audience} ask the bot questions?`, message: "Widening this shares the model budget with everyone who may ask: each question is a model call against the host's monthly cap.", confirmLabel: "Widen it" }))) return;
    }
    setBusy(true);
    try {
      const r = await save([{ kind: "settings", fields: f }], { revs: data.revs?.settings ? { "config:settings": data.revs.settings } : undefined, title: "Save the guild settings?" });
      if (r === null) return;  // Back to edits
      if (r.changed?.length) { await onReload(); return; }  // someone changed them meanwhile: reload under the draft — untouched fields re-base, clashes ask Keep mine / Take theirs
      if (!r.refused?.length) await onSaved();
    } finally { setBusy(false); }
  }

  if (!d) {
    return (
      <Card>
        <CardHeader title="Settings" action={!ro ? <Button size="xs" variant="default" leftSection={<IconPencil size={13} />} onClick={() => { startRef.current = base; setDraft(base); }}>Edit settings</Button> : undefined} />
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md" p="md">
          <Fact label="Timezone" value={`${tzLabel(st.timezone)} (${st.timezone})`} />
          <Fact label="Who may ask the bot questions" value={st.ask_audience} />
          <Fact label="Officer roles" value={st.officer_roles.map((r) => `@${r.name}`).join(", ") || "none (Manage Server counts)"} />
          <Fact label="About the guild" value={st.about || "—"} />
        </SimpleGrid>
        {st.officer_roles_pending.length > 0 && <Text size="xs" c="orange" px="md" pb="md">Not found in the server (from an older config, by name): {st.officer_roles_pending.join(", ")} — edit the settings to pick the role again.</Text>}
        <Text size="xs" c="dimmed" px="md" pb="md">Owner: {st.owner_id ? `Discord id ${st.owner_id}` : "not claimed — /gm config owner"} · transfer with /gm config owner in the server</Text>
      </Card>
    );
  }
  const notes = tzPreview?.described?.lines.flatMap((l) => l.notes) || [];
  const cons = tzPreview?.described?.consequences || [];
  const tzError = tzPreview?.described?.lines.find((l) => l.error)?.error;
  return (
    <Card>
      <CardHeader title="Settings" hint="edit mode — nothing is saved until Save" />
      <Stack gap="md" p="md">
        {stale && <Alert color="yellow" variant="light" p="xs"><Group justify="space-between"><Text size="sm">Someone changed the settings since you opened them.</Text><Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} onClick={onReload}>Reload (keeps your edits)</Button></Group></Alert>}
        {conflicts.length > 0 && (
          <Alert color="orange" variant="light" p="xs" title="Changed by someone else while you edited">
            <Stack gap={6}>
              {conflicts.map((k) => (
                <Group key={k} justify="space-between" wrap="wrap" gap="xs">
                  <Text size="sm" style={{ minWidth: 0, overflowWrap: "anywhere" }}><b>{FIELD_WORDS[k]}</b>: yours {show(k, d[k])} · saved now {show(k, base[k])}</Text>
                  <Group gap={4} wrap="nowrap">
                    <Button size="compact-xs" variant="default" onClick={() => setConflicts((c) => c.filter((x) => x !== k))}>Keep mine</Button>
                    <Button size="compact-xs" variant="light" onClick={() => { setDraft({ ...d, [k]: base[k] }); setConflicts((c) => c.filter((x) => x !== k)); }}>Take theirs</Button>
                  </Group>
                </Group>
              ))}
            </Stack>
          </Alert>
        )}
        <Box>
          <Select label="Timezone" description="every schedule, lock and clock on the site and in Discord — pick the city whose clock you keep" data={meta.timezones || [{ group: "Current", items: [{ value: st.timezone, label: st.timezone }] }]}
            value={d.timezone} onChange={(v) => v && setDraft({ ...d, timezone: v })} searchable allowDeselect={false} nothingFoundMessage="no such city" maxDropdownHeight={320} w={360} />
          {d.timezone !== st.timezone && (
            <Stack gap={2} mt={6}>
              {tzError && <Text size="sm" c="red">⛔ {tzError}</Text>}
              {notes.map((n, i) => <Text key={i} size="xs" c="dimmed">{n}</Text>)}
              {cons.map((c, i) => <Text key={`c${i}`} size="xs" c="yellow">⚠ {c}</Text>)}
              {cons.length > 0 && (
                <SegmentedControl size="xs" mt={4} value={tzRuns} onChange={(v) => setTzRuns(v as "keep" | "move")}
                  data={[{ value: "keep", label: "Keep runs at their real moment" }, { value: "move", label: "Move open runs with the clock" }]} />
              )}
            </Stack>
          )}
        </Box>
        <Select label="Who may ask the bot questions" description="/ask, DMs, @mentions; others get the static guide" data={AUDIENCES} value={d.ask_audience} onChange={(v) => v && setDraft({ ...d, ask_audience: v })} allowDeselect={false} w={260} />
        <MultiSelect label="Officer roles" description="Discord roles that count as officer (Manage Server always does); picked by role, so renaming a role keeps its officers" data={roleOptions} value={d.officer_roles} onChange={(v) => setDraft({ ...d, officer_roles: v })} searchable w={420} />
        <Box>
          <Textarea label="About the guild" description="one paragraph on the public homepage and in the static guide to unregistered visitors" value={d.about} maxLength={aboutMax} autosize minRows={2}
            onChange={(e) => setDraft({ ...d, about: e.currentTarget.value })} />
          <Text size="xs" c={d.about.length > aboutMax - 40 ? "yellow" : "dimmed"} ta="right">{d.about.length}/{aboutMax}</Text>
          <HomepageCard info={pub} about={d.about} name={meta.guild} />
        </Box>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={() => { setConflicts([]); setDraft(null); }} disabled={busy}>Cancel</Button>
          <Button size="sm" loading={busy} onClick={submit} disabled={conflicts.length > 0}>Save settings</Button>
        </Group>
      </Stack>
      {review}
      {confirmDialog}
    </Card>
  );
}

/** The public homepage's card as it will look with this about text (name, icon, the blurb, the Join button). */
function HomepageCard({ info, about, name }: { info: PublicInfo | null; about: string; name: string }) {
  return (
    <Box mt="xs" p="md" style={{ background: "#151B23", border: "1px solid #313B48", borderRadius: 14, maxWidth: 520 }}>
      <Text size="xs" c="dimmed" mb={6}>The homepage will read:</Text>
      <Group gap="sm" mb={8} wrap="nowrap">
        {info?.icon && <Avatar src={info.icon} size={40} radius="md" />}
        <Title order={3} size="h4" style={{ overflowWrap: "anywhere" }}>{info?.name || name}</Title>
      </Group>
      {about.trim() ? <Text size="sm" c="#C9D1DC" style={{ whiteSpace: "pre-line" }}>{about.trim()}</Text> : <Text size="sm" c="dimmed">(no about text: the card shows just the name)</Text>}
      <Group gap="xs" mt="sm">
        {info?.invite_url && <Badge color="indigo" variant="filled" radius="sm">Join our Discord</Badge>}
        <Badge color="teal" variant="filled" radius="sm">Log in with Discord</Badge>
      </Group>
    </Box>
  );
}

/** News keywords (saved ONCE on blur / when the dropdown closes, not per keystroke), the terms that are always matched,
 *  and the news channel's status with Re-read the channel (POST /api/news/backfill). */
function NewsCard({ data, ro, onSaved }: { data: ConfigData; ro: boolean; onSaved: () => void }) {
  const [news, setNews] = useState<NewsData | null>(null);
  const [kw, setKw] = useState<string[]>(data.settings.news_keywords || []);
  const [busy, setBusy] = useState(false);
  const [save, review] = useOpsSave();
  const loadNews = () => api.get<NewsData>("/api/news").then(setNews).catch(() => setNews(null));
  useEffect(() => { loadNews(); }, []);
  useEffect(() => { setKw(data.settings.news_keywords || []); }, [data.settings.news_keywords.join("\u0000")]);  // eslint-disable-line react-hooks/exhaustive-deps
  const saving = useRef<string | null>(null);  // blur and dropdown-close can both fire: one write per change
  async function commit() {
    const want = kw.join("\u0000");
    if (ro || want === (data.settings.news_keywords || []).join("\u0000") || saving.current === want) return;
    saving.current = want;
    try {
      const r = await save([{ kind: "settings", fields: { news_keywords: kw } }], { revs: data.revs?.settings ? { "config:settings": data.revs.settings } : undefined });
      if (r && !r.refused?.length && !r.changed?.length) onSaved();
    } finally { saving.current = null; }
  }
  async function reread() {
    setBusy(true);
    try { const r = await api.post<{ message: string }>("/api/news/backfill"); ok(r.message); await loadNews(); } catch (e) { fail(e); } finally { setBusy(false); }
  }
  const always = [...(news?.keywords.version || []), ...(news?.keywords.raid || [])];
  const items = news?.items || [];
  const status = !data.channels.news?.id ? "No news channel set: pick one under Channels for the webhook to post in."
    : items.length ? `Last item kept ${items[items.length - 1].ingested_label} · ${items.length} recent item${items.length === 1 ? "" : "s"} kept`
    : "Nothing received yet — is the webhook pointed at the news channel?";
  return (
    <Card>
      <CardHeader title="News" hint="what the news review keeps from the news channel" action={<Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} loading={busy} disabled={!data.channels.news?.id} onClick={reread}>Re-read the channel</Button>} />
      <Stack gap="sm" p="md">
        <Text size="sm" c={items.length ? undefined : "dimmed"}>{status}</Text>
        <TagsInput label="News keywords" description="extra words or phrases that make a news post relevant — saved when you leave the field" value={kw} onChange={setKw}
          onBlur={commit} onDropdownClose={commit} disabled={ro} clearable maw={560} />
        {always.length > 0 && (
          <Group gap={6} wrap="wrap">
            <Text size="xs" c="dimmed">Always matched:</Text>
            {always.map((t) => <Badge key={t} size="sm" variant="outline" color="gray">{t}</Badge>)}
          </Group>
        )}
      </Stack>
      {review}
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <Box style={{ minWidth: 0 }}>
      <Text size="xs" c="dimmed" tt="uppercase" style={{ letterSpacing: ".08em" }}>{label}</Text>
      <Text size="sm" fw={600} style={{ overflowWrap: "anywhere", whiteSpace: "pre-line" }}>{value}</Text>
    </Box>
  );
}
