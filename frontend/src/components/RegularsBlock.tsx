import { useState } from "react";
import { ActionIcon, Badge, Box, Button, Group, Select, Stack, Text, Tooltip } from "@mantine/core";
import { IconUserPlus, IconX } from "@tabler/icons-react";
import { api, type Meta, type Person, type RaidRuns, type Series } from "../api";
import { GameIcon } from "./Icons";
import { useConfirm } from "./ConfirmModal";
import { classColour } from "../theme";
import { Eyebrow, fail, ok } from "./Page";
import { RoleCounts, type Act } from "./board/shared";
import { LevelChip } from "./LevelChip";

/** A raid's regulars, per recurring schedule — the Rosters page's Regulars tab, kept whole when that page became the
 *  Events page (phase 4, Task 10) and drawn under each raid on the Schedules page until its own Regulars section
 *  (Task 11) takes over: who the bot expects, the ready / thin / short chip, the next four weeks, add and remove, and a
 *  removed schedule's leftovers to clear. Every confirm prints the server's sentence (a regular's `remove`, an
 *  orphan's `clear`) and every write carries the member record's rev. */
export function RegularsSection({ r, meta, people, onReload }: { r: RaidRuns; meta: Meta; people: Person[]; onReload: () => Promise<void> | void }) {
  const [busy, setBusy] = useState<string | null>(null);
  async function act(key: string, path: string, payload?: unknown) {
    setBusy(key);
    try { const x = await api.post<{ message: string }>(path, payload ?? {}); ok(x.message); await onReload(); } catch (e) { fail(e); } finally { setBusy(null); }
  }
  return <Regulars r={r} meta={meta} people={people} onAct={act} busy={busy} />;
}

/** One recurring schedule's regulars: role icon + count against the minimum, one member per line (spec icon + character;
 *  the member's name in the tooltip), add (member, then character) and remove. Officers edit; it is the same route as
 *  Discord's /roster add|remove, plain text and MCP. */
function RegularsCard({ r, s, meta, people, onAct, busy }: { r: RaidRuns; s: Series; meta: Meta; people: Person[]; onAct: Act; busy: string | null }) {
  const [who, setWho] = useState<string | null>(null);
  const [char, setChar] = useState<string | null>(null);
  const [ask, dialog] = useConfirm();
  const person = people.find((p) => p.uid === who);
  const on = new Set(s.regulars.map((x) => x.uid));
  const level = s.level || (Object.entries(s.need).some(([role, n]) => (s.counts[role] || 0) < n) ? "red" : "green");
  const spare = s.no_spare || [];
  const levelWords = { green: "regular mains cover the tank and healer minimums, with a spare", amber: spare.length ? `no spare ${spare.join(" or ")} among regular mains — one absence leaves it short` : "covered only with offspecs, flex roles or alts", red: "regulars can't field the minimum" }[level];
  // the sentence is the server's (consequences.regular_remove), the write carries the member record's rev (stale → 409)
  async function remove(x: Series["regulars"][number]) {
    if (!(await ask({ title: `Take ${x.display_name} off the regulars?`, message: x.remove, confirmLabel: "Remove", cancelLabel: "Back", color: "red" }))) return;
    await onAct(`reg${s.key}${x.uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: s.key, uid: x.uid, on: false, rev: x.rev });
  }
  async function add() {
    if (!person) return;
    await onAct(`reg${s.key}add`, "/api/admin/raid/regulars", { raid: r.id, schedule: s.key, uid: person.uid, character: char || person.characters[0]?.label, on: true });
    setWho(null); setChar(null);
  }
  return (
    <Box p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0, flex: "1 1 300px" }}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Box style={{ minWidth: 0 }}>
          <Text fw={700} size="sm">{s.name}{!s.active && <Badge ml={6} size="xs" color="gray" variant="light" tt="none">paused</Badge>}</Text>
          <Text size="xs" c="dimmed">{s.label} · {s.regulars.length} regular{s.regulars.length === 1 ? "" : "s"} for {s.seats} seats</Text>
        </Box>
        {/* the same ready / thin / short chip as Readiness (the word from meta.level_words), why in its tooltip */}
        <Group gap="sm" wrap="nowrap"><RoleCounts meta={meta} counts={s.counts} need={s.need} size={16} /><LevelChip meta={meta} level={level} why={levelWords} size="sm" /></Group>
      </Group>
      {s.regulars.length > 0 && level === "amber" && (s.cover || []).length > 0 && <Text size="xs" c="dimmed" mt={4}>could cover: {(s.cover || []).join(", ")}</Text>}
      {s.regulars.length > 0 && level === "red" && <Text size="xs" c="red" mt={4}>recruit{(s.recruit || []).length ? `: ${(s.recruit || []).join(", ")}` : " — nobody in the guild plays the missing role"}</Text>}
      <Forecast s={s} meta={meta} />
      <Stack gap={0} mt={6}>
        {s.regulars.length === 0 && <Text size="sm" c="dimmed">No regulars yet. Every main counts as expected until you add some below.</Text>}
        {s.regulars.map((x) => (
          <Group key={x.uid} gap={6} wrap="nowrap" style={{ lineHeight: 1.8 }}>
            <GameIcon meta={meta} kind="spec" id={`${x.cls}:${x.spec}`} size={18} title={`${x.spec} · ${x.role}`} />
            <Tooltip label={x.display_name}><Text size="sm" fw={600} c={classColour(meta, x.cls)} truncate style={{ flex: 1 }}>{x.character}</Text></Tooltip>
            <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`remove ${x.character}`} loading={busy === `reg${s.key}${x.uid}`} onClick={() => remove(x)}><IconX size={13} /></ActionIcon>
          </Group>
        ))}
      </Stack>
      <Group gap="xs" mt="xs" wrap="wrap">
        <Select size="xs" w={170} searchable placeholder="add a member" value={who} onChange={(v) => { setWho(v); setChar(null); }}
          data={people.filter((p) => !on.has(p.uid)).map((p) => ({ value: p.uid, label: p.display_name }))} nothingFoundMessage="nobody left" />
        {person && person.characters.length > 1 && (
          <Select size="xs" w={160} allowDeselect={false} value={char || person.characters[0].label} onChange={setChar} aria-label="which character"
            data={person.characters.map((c) => ({ value: c.label, label: c.label + (c.is_main ? " (main)" : "") }))} />
        )}
        <Button size="xs" variant="default" leftSection={<IconUserPlus size={13} />} disabled={!person} loading={busy === `reg${s.key}add`} onClick={add}>{person ? `Add ${char || person.characters[0]?.label || person.display_name}` : "Add"}</Button>
      </Group>
      {dialog}
    </Box>
  );
}

/** The series' 4-week forecast: each upcoming start with what the regulars minus recorded absences leave short — role
 *  icon + number (icons stand alone), who is away in the tooltip. Officers see a hole a week ahead. */
function Forecast({ s, meta }: { s: Series; meta: Meta }) {
  const rows = s.forecast || [];
  if (!rows.length) return null;
  return (
    <Box mt={6}>
      <Eyebrow plain>Next 4 weeks</Eyebrow>
      <Stack gap={0}>
        {rows.map((f) => (
          <Tooltip key={f.start} label={f.away.length ? `away: ${f.away.join(", ")}` : `${f.expected} regulars expected`} withinPortal>
            <Group gap={8} wrap="nowrap" style={{ lineHeight: 1.7 }}>
              <Text size="xs" w={78} style={{ fontVariantNumeric: "tabular-nums" }}>{f.day}</Text>
              {Object.keys(f.short).length === 0 ? <Text size="xs" c="teal">covered</Text> : Object.entries(f.short).map(([role, k]) => (
                <Group key={role} gap={2} wrap="nowrap"><GameIcon meta={meta} kind="role" id={role} size={14} title={role} /><Text size="xs" c="red" style={{ fontVariantNumeric: "tabular-nums" }}>−{k}</Text></Group>
              ))}
              {f.away.length > 0 && <Text size="xs" c="dimmed">{f.away.length} away</Text>}
            </Group>
          </Tooltip>
        ))}
      </Stack>
    </Box>
  );
}

function Regulars({ r, meta, people, onAct, busy }: { r: RaidRuns; meta: Meta; people: Person[]; onAct: Act; busy: string | null }) {
  const series = r.series || [];
  const orphans = r.orphan_regulars || [];
  const [ask, dialog] = useConfirm();
  /** Clear a removed schedule's regular: asks first with the server's sentence, carries the member record's rev, and the
   *  row's button spins while it writes. */
  async function clear(o: NonNullable<RaidRuns["orphan_regulars"]>[number]) {
    if (!(await ask({ title: `Clear ${o.character} from ${o.label}?`, message: o.clear, confirmLabel: "Clear", cancelLabel: "Back", color: "red" }))) return;
    await onAct(`orph${o.key}${o.uid}`, "/api/admin/raid/regulars", { raid: r.id, schedule: o.key, uid: o.uid, on: false, rev: o.rev });
  }
  if (!series.length && !orphans.length) return <Text size="sm" c="dimmed">No weekly schedule for this raid, so it has no regulars. Add one with Add a schedule above; one-off events have no regulars.</Text>;
  return (
    <Stack gap="sm">
      <Group gap="sm" align="stretch" wrap="wrap">{series.map((s) => <RegularsCard key={s.key} r={r} s={s} meta={meta} people={people} onAct={onAct} busy={busy} />)}</Group>
      {orphans.length > 0 && (
        <Box>
          <Text size="xs" c="dimmed">Regulars of a removed schedule. Clear them:</Text>
          {orphans.map((o) => (
            <Group key={o.key + o.uid} gap={6} wrap="nowrap">
              <GameIcon meta={meta} kind="spec" id={`${o.cls}:${o.spec}`} size={16} title={o.spec} />
              <Tooltip label={o.display_name}><Text size="sm">{o.character}</Text></Tooltip>
              <Text size="xs" c="dimmed">{o.label}</Text>
              <ActionIcon size="sm" variant="subtle" color="gray" aria-label={`clear ${o.character}`} loading={busy === `orph${o.key}${o.uid}`} disabled={!!busy?.startsWith("orph")} onClick={() => clear(o)}><IconX size={13} /></ActionIcon>
            </Group>
          ))}
        </Box>
      )}
      {dialog}
    </Stack>
  );
}
