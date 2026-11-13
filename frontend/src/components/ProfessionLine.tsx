import { Group, Stack, Text, Tooltip } from "@mantine/core";
import type { Meta, Profession } from "../api";
import { GameIcon } from "./Icons";

/** One character's professions (design §5.60): a profession icon with its skill beside it (an icon plus a number, like a
 *  role count — never a text label; the tooltip carries the name), the icon alone while the skill is not said yet, green at the cap. No recipes on the row (2026-11-13: the armory brings whole recipe books — the tooltip carries the
 *  specialization its recipes show and their count; the Professions page lists them), or the empty state in words. `member` = the Members page's row (the empty state names who fills it); without it the line is
 *  the reader's own on Me. `synced` = the armory keeps them (armory link §5.4): that sentence ("Updated from the armory
 *  <when>. Keep professions by hand to edit them.") under the line, and an empty list is the armory's, not a hand gap.
 *  A game version without professions renders nothing. */
export function ProfessionLine({ meta, professions, compact, member, synced }: { meta: Meta; professions?: Profession[]; compact?: boolean; member?: string; synced?: string }) {
  const line = <Line meta={meta} professions={professions} compact={compact} member={member} synced={!!synced} />;
  if (!synced || !meta.professions?.list.length) return line;
  return <Stack gap={2}>{line}<Text size="xs" c="dimmed">{synced}</Text></Stack>;
}

function Line({ meta, professions, compact, member, synced }: { meta: Meta; professions?: Profession[]; compact?: boolean; member?: string; synced: boolean }) {
  const defs = meta.professions?.list || [];
  if (!defs.length) return null;
  const def = (id: string) => defs.find((d) => d.id === id);
  const name = (id: string) => def(id)?.name || id;
  const cap = meta.professions?.skill_cap || 300;
  if (!professions || professions.length === 0)
    return <Text size="xs" c="dimmed">{synced ? "the armory shows no professions for this character" : member
      ? `no professions on record — ${member} adds them on the Me page or with /me professions`
      : "no professions on record — add them with Professions… in this row's ⋮"}</Text>;
  const tip = (p: Profession) => [p.skill == null ? `${name(p.id)} · skill not said yet` : p.skill >= cap ? `${name(p.id)}, at the cap` : name(p.id),
    p.specialization, p.recipes.length ? `${p.recipes.length} recipe${p.recipes.length === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ");
  return (
    <Group gap={compact ? 8 : 14} wrap="wrap" align="center">
      {professions.map((p) => (
        <Tooltip key={p.id} label={tip(p)}>
          <Group gap={5} wrap="nowrap">
            <GameIcon meta={meta} kind="profession" id={p.id} size={compact ? 20 : 24} title={name(p.id)} />
            {p.skill != null && <Text fw={700} size={compact ? "sm" : "md"} c={p.skill >= cap ? "teal.3" : undefined}>{p.skill}</Text>}
          </Group>
        </Tooltip>
      ))}
    </Group>
  );
}
