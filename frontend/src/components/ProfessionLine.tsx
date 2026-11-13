import { Badge, Group, Stack, Text, Tooltip } from "@mantine/core";
import type { Meta, Profession } from "../api";
import { IconQuestionMark } from "@tabler/icons-react";
import { GameIcon } from "./Icons";

/** One character's professions (design §5.60): a profession icon with its skill beside it (an icon plus a number, like a
 *  role count — never a text label; the tooltip carries the name), the icon alone while the skill is not said yet, green at the cap, the notable recipes as chips
 *  (one typed before the game's list that the list lacks — `unmatched` — marked with a ?, §5.64), or the
 *  empty state in words. `member` = the Members page's row (the empty state names who fills it); without it the line is
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
  const recipes = professions.flatMap((p) => p.recipes.map((r) => ({ ...r, prof: name(p.id) })));
  const makesThings = professions.some((p) => def(p.id)?.makes);  // a gathering trade or Fishing has no recipes to wait for
  return (
    <Group gap={compact ? 8 : 14} wrap="wrap" align="center">
      {professions.map((p) => (
        <Tooltip key={p.id} label={p.skill == null ? `${name(p.id)} · skill not said yet` : p.skill >= cap ? `${name(p.id)}, at the cap` : name(p.id)}>
          <Group gap={5} wrap="nowrap">
            <GameIcon meta={meta} kind="profession" id={p.id} size={compact ? 20 : 24} title={name(p.id)} />
            {p.skill != null && <Text fw={700} size={compact ? "sm" : "md"} c={p.skill >= cap ? "teal.3" : undefined}>{p.skill}</Text>}
          </Group>
        </Tooltip>
      ))}
      {recipes.length
        ? recipes.map((r) => {
          const off = !!r.unmatched;  // typed before the game's list, and the list lacks it (§5.64)
          return <Badge key={r.prof + (r.id ?? r.name)} variant="outline" color={off ? "yellow" : "gray"} radius="xl" size={compact ? "sm" : "md"} tt="none" fw={500}
            leftSection={off ? <IconQuestionMark size={11} /> : undefined} title={off ? `${r.prof} · not in the game's list` : r.prof}>{r.name}</Badge>;
        })
        : makesThings && !synced && <Text size="xs" c="dimmed">No notable recipes yet — add the ones worth telling the guild about in Professions…</Text>}
    </Group>
  );
}
