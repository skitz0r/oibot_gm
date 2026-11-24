import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ActionIcon, Anchor, Box as MBox, Button, Group, Select, Stack, Text, Tooltip } from "@mantine/core";
import { Link } from "react-router-dom";
import { IconX } from "@tabler/icons-react";
import { api, ApiError, type Box, type ComingView, type Meta, type OrphanRegular, type ScheduleView } from "../../api";
import { Facts, ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { guardClick } from "../LeaveGuard";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { boxOf } from "../events/shared";
import { classColour } from "../../theme";
import css from "../../pages/listpage.module.css";

type Row = NonNullable<ScheduleView["regulars"]>["rows"][number];
type Week = NonNullable<NonNullable<ScheduleView["regulars"]>["forecast"]>[number];

/** A regular's Remove (box 18): the box the page holds, pressed with its fp (`seen`) and the record's rev; a box whose
 *  facts changed comes back fresh (409 with a box), a record changed meanwhile (409 without one) is read again — the
 *  fresh row's box AND rev, so the next press goes through. `fresh` = that row read again (null: they are no longer on it). */
function useRemove(onDone: () => void) {
  const [ask, dialog] = useConfirmBox(["member", "config"]);
  async function remove(first: { box: Box; rev: string }, body: Record<string, unknown>, fresh: () => Promise<{ box: Box; rev: string } | null>) {
    const shown = { rev: first.rev };
    const again = async (): Promise<Box> => {
      const x = await fresh();
      if (!x) throw new Error("gone");
      shown.rev = x.rev;
      return x.box;
    };
    const done = await ask({
      box: first.box,
      refresh: again,
      act: async (fp) => {
        try {
          const r = await api.post<{ message: string }>("/api/admin/raid/regulars", { ...body, on: false, rev: shown.rev, seen: fp });
          ok(r.message);
          return null;
        } catch (e) {
          const b = boxOf(e);
          if (b) return b;
          if (e instanceof ApiError && e.status === 409) return again();
          throw e;
        }
      },
    });
    if (done) onDone();
  }
  return [remove, dialog] as const;
}

/** The Regulars section (Schedules.dc.html): the server's facts — how many for how many seats, who is expected without
 *  regulars, what the regulars are short (role icon + number), the next four weeks in words — then Add a member (a
 *  search over the server's `people`, then which character when they have several; adds at once, no box) and one name
 *  per line, each with its Remove (the server's box). Under the facts, each short night of the next four weeks: its
 *  start, what is short, who is away (in the tooltip) and Open this run early — the night's own box from the Events
 *  page's coming-up view, greyed with the server's sentence while it can't open; a night with an event links it. */
export function RegularsSection({ view, meta, onReload }: { view: ScheduleView; meta: Meta; onReload: () => Promise<unknown> }) {
  const r = view.regulars!;
  const sid = view.id;
  const navigate = useNavigate();
  const [who, setWho] = useState<string | null>(null);
  const [char, setChar] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState<string | null>(null);
  const [said, setSaid] = useState<Record<string, string>>({});
  const [remove, removeDialog] = useRemove(() => { onReload(); });
  const [ask, openDialog] = useConfirmBox(["run:", "config"]);
  const person = view.people.find((p) => p.uid === who);

  async function add() {
    if (!person) return;
    setBusy(true);
    try {
      const x = await api.post<{ message: string }>("/api/admin/raid/regulars", { schedule: sid, uid: person.uid, character: char || person.characters[0]?.label, on: true });
      ok(x.message);
      setWho(null); setChar(null);
      await onReload();
    } catch (e) { fail(e); } finally { setBusy(false); }
  }

  /** the row as the server has it now (a stale box or record): its fresh box and rev */
  const rowNow = (uid: string) => async () => {
    const v = (await onReload()) as ScheduleView | null;
    const x = v?.regulars?.rows.find((y) => y.uid === uid);
    return x ? { box: x.box, rev: x.rev } : null;
  };
  const drop = (x: Row) => remove({ box: x.box, rev: x.rev }, { schedule: sid, uid: x.uid }, rowNow(x.uid));

  /** Open this run early: the coming-up night's own box (Events page, event_view.coming_json), pressed with its fp */
  async function openEarly(f: Week) {
    setOpening(f.occurrence);
    const url = `/api/event/coming/${encodeURIComponent(sid)}/${encodeURIComponent(f.occurrence)}`;
    let night: ComingView;
    try { night = await api.get<ComingView>(url); } catch (e) {
      const b = e instanceof ApiError && e.status === 404 ? (e.body as { error?: string; key?: string | null } | undefined) : undefined;
      if (b?.key) navigate(`/events/${encodeURIComponent(b.key)}`);
      else if (b?.error) setSaid((s) => ({ ...s, [f.occurrence]: b.error! }));
      else fail(e);
      setOpening(null);
      return;
    } finally { setOpening(null); }
    if (!night.box) { setSaid((s) => ({ ...s, [f.occurrence]: night.why || "" })); return; }
    let key = "";
    let message = "";
    const done = await ask({
      box: night.box,
      refresh: async (): Promise<Box> => { const v = await api.get<ComingView>(url); if (v.box) return v.box; throw new Error("gone"); },
      act: async (fp) => {
        try {
          const x = await api.post<{ message: string; key: string }>(`/api/raid/${encodeURIComponent(night.open.raid)}/open`,
            { start: night.open.start_iso, schedule: night.open.schedule, occurrence: night.open.occurrence, seen: fp });
          key = x.key; message = x.message;
          return null;
        } catch (e) { const b = boxOf(e); if (b) return b; throw e; }
      },
    });
    if (done && key) { ok(message); navigate(`/events/${encodeURIComponent(key)}`); }
  }

  const facts = [
    { label: "Regulars", value: r.words },
    ...(r.expected ? [{ label: "Expected without regulars", value: r.expected }] : []),
    ...(r.short ? [{ label: "Regulars are short", value: "" }] : []),
    ...(r.outlook ? [{ label: "Next four weeks", value: r.outlook }] : []),
  ];
  const weeks = (r.forecast || []).filter((f) => Object.keys(f.short).length > 0);
  const addRow = (
    <Group gap="xs" align="flex-end" wrap="wrap">
      <Select label="Add a member" placeholder="search members" searchable clearable w={220} maw="100%" value={who} comboboxProps={{ withinPortal: true }}
        data={view.people.map((p) => ({ value: p.uid, label: p.display_name }))} onChange={(v) => { setWho(v); setChar(null); }} />
      {person && person.characters.length > 1 && (
        <Select label="Character" w={180} maw="100%" allowDeselect={false} value={char || person.characters[0].label} onChange={setChar} comboboxProps={{ withinPortal: true }}
          data={person.characters.map((c) => ({ value: c.label, label: c.label }))} />
      )}
      <Button variant="default" disabled={!person} loading={busy} onClick={add}>Add</Button>
    </Group>
  );
  return (
    <ViewSection title="Regulars">
      <Stack gap="md">
        <Group justify="space-between" align="flex-end" wrap="wrap" gap="md">
          <MBox style={{ flex: "1 1 320px", minWidth: 0 }}>
            <Facts items={facts} show={(f) => (f.label === "Regulars are short" ? <ShortIcons meta={meta} short={r.short!} /> : undefined)} />
          </MBox>
          {!view.gone && addRow}
        </Group>
        {weeks.length > 0 && (
          <Stack gap={6} aria-label="Next four weeks">
            {weeks.map((f) => (
              <Group key={f.occurrence} gap="sm" wrap="wrap" align="center">
                <Tooltip label={f.away.join(", ")} disabled={!f.away.length}>
                  {f.key
                    ? <Anchor component={Link} to={`/events/${encodeURIComponent(f.key)}`} onClick={guardClick(`/events/${encodeURIComponent(f.key)}`)} size="sm" fw={600}>{f.when}</Anchor>
                    : <Text size="sm" fw={600}>{f.when}</Text>}
                </Tooltip>
                <ShortIcons meta={meta} short={f.short} />
                {!f.key && f.can_open && <Button size="compact-sm" variant="light" color="red" loading={opening === f.occurrence} onClick={() => openEarly(f)}>Open this run early</Button>}
                {!f.key && !f.can_open && f.why && <Text size="xs" c="dimmed">{f.why}</Text>}
                {said[f.occurrence] && <Text size="xs" c="dimmed">{said[f.occurrence]}</Text>}
              </Group>
            ))}
          </Stack>
        )}
        {r.rows.length > 0 && (
          <div className={css.people}>
            {r.rows.map((x) => (
              <Group key={x.uid} gap={6} wrap="nowrap">
                <GameIcon meta={meta} kind="spec" id={`${x.cls}:${x.spec}`} size={18} title={x.spec} />
                <Tooltip label={x.display_name}><Text size="sm" fw={600} c={classColour(meta, x.cls)} truncate style={{ flex: 1, minWidth: 0 }}>{x.character}</Text></Tooltip>
                <Tooltip label="Remove"><ActionIcon variant="subtle" color="gray" aria-label={`Remove ${x.character}`} onClick={() => drop(x)}><IconX size={14} /></ActionIcon></Tooltip>
              </Group>
            ))}
          </div>
        )}
      </Stack>
      {removeDialog}
      {openDialog}
    </ViewSection>
  );
}

/** What is short, by role: the role icon and the number (the Iconography rule — no word beside an icon). */
function ShortIcons({ meta, short }: { meta: Meta; short: Record<string, number> }) {
  return (
    <Group gap={10} wrap="nowrap" component="span">
      {Object.entries(short).map(([role, n]) => (
        <Group key={role} gap={3} wrap="nowrap" component="span">
          <GameIcon meta={meta} kind="role" id={role} size={16} title={role} />
          <Text span size="sm" fw={700} c="red">{n}</Text>
        </Group>
      ))}
    </Group>
  );
}

/** A removed schedule's leftover regulars (an older file's — a removal takes them off now): who, the schedule in words,
 *  and Remove with the server's box. Shown under the list until none is left. */
export function RemovedRegulars({ orphans, meta, onReload }: { orphans: OrphanRegular[]; meta: Meta; onReload: () => Promise<{ orphans: OrphanRegular[] } | null> }) {
  const [remove, dialog] = useRemove(() => { onReload(); });
  const drop = (o: OrphanRegular) => remove({ box: o.box, rev: o.rev }, { schedule: o.key, uid: o.uid }, async () => {
    const x = (await onReload())?.orphans.find((y) => y.key === o.key && y.uid === o.uid);
    return x ? { box: x.box, rev: x.rev } : null;
  });
  return (
    <Stack gap={4} mt="md" aria-label="Regulars of a removed schedule">
      <Text size="xs" fw={700} c="dimmed" tt="uppercase" style={{ letterSpacing: ".08em" }} px={12}>Regulars of a removed schedule</Text>
      {orphans.map((o) => (
        <Group key={o.key + o.uid} gap={6} wrap="nowrap" px={12}>
          <GameIcon meta={meta} kind="spec" id={`${o.cls}:${o.spec}`} size={16} title={o.spec} />
          <MBox style={{ flex: 1, minWidth: 0 }}>
            <Tooltip label={o.display_name}><Text size="sm" fw={600} c={classColour(meta, o.cls)} truncate>{o.character}</Text></Tooltip>
            <Text size="xs" c="dimmed" truncate>{o.label}</Text>
          </MBox>
          <Tooltip label="Remove"><ActionIcon variant="subtle" color="gray" aria-label={`Remove ${o.character}`} onClick={() => drop(o)}><IconX size={14} /></ActionIcon></Tooltip>
        </Group>
      ))}
      {dialog}
    </Stack>
  );
}
