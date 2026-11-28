import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge, Box, Button, Group, Select, Stack, Tabs, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { api, type BotActivity, type BotActivityRow, type EventPerson, type EventView, type Meta } from "../../api";
import { GameIcon } from "../Icons";
import { ViewSection } from "../Facts";
import { fail, ok } from "../Page";
import { classColour } from "../../theme";
import css from "../../pages/listpage.module.css";

const HASH = "#bot-activity";

/** The event page's two top tabs (design 2026-11-28-event-activity-tab): **Event** — the page as it was — and **Bot
 *  activity** with the count of DMs still waiting for an answer when there are any. The tab is in the URL hash
 *  (#bot-activity), so a link opens it. The tab is read from the router's location, so back / forward land on the tab
 *  their address names; a switch goes through the router's own navigate (replace, its history state kept). The Event tab
 *  stays mounted while hidden (the board is never rebuilt by a switch). The tab's rows come from GET
 *  /api/event/{key}/activity, read again whenever the event is (the page's live topics); another event's rows are
 *  cleared at once, never shown under this one. Every word is the server's (meta.page_words, the route). */
export function EventTabs({ ev, meta, onChanged, children }: { ev: EventView; meta: Meta; onChanged: () => void; children: React.ReactNode }) {
  const loc = useLocation();
  const navigate = useNavigate();
  const tab = loc.hash === HASH ? "activity" : "event";
  const [data, setData] = useState<BotActivity | null>(null);
  const seq = useRef(0);
  useEffect(() => { setData(null); }, [ev.key]);
  useEffect(() => {
    const n = ++seq.current;
    api.get<BotActivity>(`/api/event/${encodeURIComponent(ev.key)}/activity`).then((d) => { if (n === seq.current) setData(d); }).catch(fail);
  }, [ev]);
  const pick = (v: string | null) => {
    navigate({ pathname: loc.pathname, search: loc.search, hash: v === "activity" ? HASH : "" }, { replace: true, state: loc.state });
  };
  const n = data && data.key === ev.key ? data.pending_count : 0;
  return (
    <Tabs value={tab} onChange={pick}>
      <Box px={20} pt={6}>
        <Tabs.List>
          <Tabs.Tab value="event">{meta.page_words.tab_event}</Tabs.Tab>
          <Tabs.Tab value="activity" rightSection={n ? <Badge size="sm" variant="filled" color="teal" circle>{n}</Badge> : undefined}>{meta.page_words.tab_bot_activity}</Tabs.Tab>
        </Tabs.List>
      </Box>
      <Tabs.Panel value="event">{children}</Tabs.Panel>
      <Tabs.Panel value="activity">
        {data && data.key === ev.key ? <BotActivityPanel data={data} meta={meta} onChanged={onChanged} /> : <div className={css.sect}><Text c="dimmed">Loading…</Text></div>}
      </Tabs.Panel>
    </Tabs>
  );
}

/** What the bot still waits on (soonest deadline first), then what it sent and how each ended (newest first), with a
 *  filter by kind and by person. Nothing here composes a sentence: every label, outcome and empty state is the server's.
 *  The one action is Withdraw on a waiting fill, pre-lock or role switch ask (the row carries it) — the same route and press as
 *  the sheet card's What happened list. */
function BotActivityPanel({ data, meta, onChanged }: { data: BotActivity; meta: Meta; onChanged: () => void }) {
  const [kind, setKind] = useState<string | null>(null);
  const [who, setWho] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const keep = (r: BotActivityRow) => (!kind || r.kind === kind) && (!who || r.uids.includes(who));
  const pending = data.pending.filter(keep);
  const history = data.history.filter(keep);
  const any = data.pending.length + data.history.length > 0;
  async function withdraw(r: BotActivityRow) {
    if (!r.withdraw || busy) return;
    setBusy(r.id);
    try { const res = await api.post<{ message: string }>(r.withdraw.path, { uid: r.withdraw.uid, asked_at: r.withdraw.asked_at }); if (res.message) ok(res.message); onChanged(); }
    catch (e) { fail(e); } finally { setBusy(null); }
  }
  return (
    <>
      {any && (
        <div className={css.sect}>
          <Group gap="sm" wrap="wrap">
            <Select size="xs" w={190} aria-label={meta.page_words.filter_kind} value={kind ?? ""} onChange={(v) => setKind(v || null)} allowDeselect={false}
              data={[{ value: "", label: data.filters.all_kinds }, ...data.filters.kinds]} comboboxProps={{ withinPortal: true }} />
            <Select size="xs" w={190} aria-label={meta.page_words.filter_person} value={who ?? ""} onChange={(v) => setWho(v || null)} allowDeselect={false} searchable
              data={[{ value: "", label: data.filters.everyone }, ...data.filters.people]} comboboxProps={{ withinPortal: true }} />
          </Group>
        </div>
      )}
      <ViewSection title={data.pending_title}>
        {data.paused.map((p) => <Text key={p} size="sm" c="orange" mb={6}>{p}</Text>)}
        {pending.length === 0 ? <Text size="sm" c="dimmed">{data.empty_pending}</Text> : (
          <Stack gap={0}>{pending.map((r) => <Row key={r.id} r={r} meta={meta} busy={busy === r.id} onWithdraw={() => withdraw(r)} />)}</Stack>
        )}
      </ViewSection>
      <ViewSection title={data.history_title}>
        {history.length === 0 ? <Text size="sm" c="dimmed">{data.empty_history}</Text> : (
          <Stack gap={0}>{history.map((r) => <Row key={r.id} r={r} meta={meta} busy={false} />)}</Stack>
        )}
        {data.unrecorded && <Text size="xs" c="dimmed" mt="sm">{data.unrecorded}</Text>}
      </ViewSection>
    </>
  );
}

const TONE: Record<BotActivityRow["tone"], string | undefined> = { yes: "teal", no: "red", waiting: "blue", closed: undefined };

/** A person on one line: the spec icon alone (role + spec, the names in its tooltip) and the character in its class colour. */
function Who({ p, meta }: { p: EventPerson; meta: Meta }) {
  return (
    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
      {p.cls && <GameIcon meta={meta} kind="spec" id={`${p.cls}:${p.spec}`} size={18} title={p.icon_title} />}
      <Text size="sm" fw={600} c={classColour(meta, p.cls)} truncate>{p.character}</Text>
    </Group>
  );
}

/** A send to many people: its sentence, unfolding to one person per line. */
function Fold({ text, people, meta, color }: { text: string; people: EventPerson[]; meta: Meta; color?: string }) {
  const [open, setOpen] = useState(false);
  if (!people.length) return <Text size="sm" c={color}>{text}</Text>;
  return (
    <Box>
      <UnstyledButton onClick={() => setOpen((o) => !o)} aria-expanded={open} style={{ display: "inline-flex", alignItems: "center", gap: 4, minHeight: 28 }}>
        {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}<Text size="sm" c={color}>{text}</Text>
      </UnstyledButton>
      {open && <Stack gap={2} pl={18} pb={4}>{people.map((p) => <Who key={p.uid || p.character} p={p} meta={meta} />)}</Stack>}
    </Box>
  );
}

function Row({ r, meta, busy, onWithdraw }: { r: BotActivityRow; meta: Meta; busy: boolean; onWithdraw?: () => void }) {
  return (
    <Box py={10} style={{ borderTop: "1px solid var(--mantine-color-slate-5)", minWidth: 0 }}>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="xs">
        <Group gap={8} wrap="wrap" style={{ minWidth: 0 }}>
          {r.person && <Who p={r.person} meta={meta} />}
          <Badge variant="light" color={TONE[r.tone] || "gray"} tt="none">{r.kind_label}</Badge>
          {r.where && <Text size="xs" c="dimmed">{r.where}</Text>}
        </Group>
        {r.withdraw && onWithdraw && <Button size="compact-xs" variant="subtle" color="gray" loading={busy} onClick={onWithdraw}>{r.withdraw.label}</Button>}
      </Group>
      {r.summary && <Fold text={r.summary} people={r.people} meta={meta} />}
      {r.failed_summary && <Fold text={r.failed_summary} people={r.failed_people} meta={meta} color="orange" />}
      {r.outcome && <Text size="sm" c={TONE[r.tone]}>{r.outcome}</Text>}
      {r.detail && <Text size="xs" c="dimmed" style={{ overflowWrap: "anywhere" }}>{r.detail}</Text>}
      {r.delivery && <Text size="xs" c="orange">{r.delivery}</Text>}
      <Group gap="md" wrap="wrap" mt={2}>
        {r.times.map((t) => <Text key={t.label} size="xs" c="dimmed">{t.label} {t.when}{t.rel ? ` · ${t.rel}` : ""}</Text>)}
      </Group>
    </Box>
  );
}
