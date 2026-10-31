import { Badge, Group, Text, Tooltip } from "@mantine/core";
import type { Meta, Profession } from "../api";
import { GameIcon } from "./Icons";

/** One character's professions (design §5.60): a profession icon with its skill beside it (an icon plus a number, like a
 *  role count — never a text label; the tooltip carries the name), green at the cap, the notable recipes as chips, or the
 *  empty state in words. `member` = the Members page's row (the empty state names who fills it); without it the line is
 *  the reader's own on Me. A game version without professions renders nothing. */
export function ProfessionLine({ meta, professions, compact, member }: { meta: Meta; professions?: Profession[]; compact?: boolean; member?: string }) {
  const defs = meta.professions?.list || [];
  if (!defs.length) return null;
  const def = (id: string) => defs.find((d) => d.id === id);
  const name = (id: string) => def(id)?.name || id;
  const cap = meta.professions?.skill_cap || 300;
  if (!professions || professions.length === 0)
    return <Text size="xs" c="dimmed">{member
      ? `no professions on record — ${member} adds them on the Me page or with /me professions`
      : "no professions on record — add them with Professions… in this row's ⋮"}</Text>;
  const recipes = professions.flatMap((p) => p.recipes.map((r) => ({ ...r, prof: name(p.id) })));
  const makesThings = professions.some((p) => def(p.id)?.makes);  // a gathering trade or Fishing has no recipes to wait for
  return (
    <Group gap={compact ? 8 : 14} wrap="wrap" align="center">
      {professions.map((p) => (
        <Tooltip key={p.id} label={p.skill >= cap ? `${name(p.id)}, at the cap` : name(p.id)}>
          <Group gap={5} wrap="nowrap">
            <GameIcon meta={meta} kind="profession" id={p.id} size={compact ? 20 : 24} title={name(p.id)} />
            <Text fw={700} size={compact ? "sm" : "md"} c={p.skill >= cap ? "teal.3" : undefined}>{p.skill}</Text>
          </Group>
        </Tooltip>
      ))}
      {recipes.length
        ? recipes.map((r) => <Badge key={r.prof + r.name} variant="outline" color="gray" radius="xl" size={compact ? "sm" : "md"} tt="none" fw={500} title={r.prof}>{r.name}</Badge>)
        : makesThings && <Text size="xs" c="dimmed">No notable recipes yet — add the ones worth telling the guild about in Professions…</Text>}
    </Group>
  );
}
