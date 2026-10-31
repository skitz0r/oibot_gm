import { useState } from "react";
import { ActionIcon, Badge, Box, Button, Group, NumberInput, Select, Stack, Text, TextInput } from "@mantine/core";
import { IconPlus, IconTrash, IconX } from "@tabler/icons-react";
import type { Meta, PeoplePreview, Profession, TableSaved } from "../api";
import { useDirty } from "../hooks/useDirty";
import { useConfirm } from "./ConfirmModal";
import { GameIcon } from "./Icons";
import { ok } from "./Page";

type Saved = PeoplePreview | TableSaved;
/** A row being edited: the skill box's raw text while typing (a cleared box stays empty), a number again on Save. */
type DraftRow = Omit<Profession, "skill"> & { skill: number | string };
const RECIPE_MAX = 60;
const COUNT_WORDS = ["No", "One", "Two", "Three", "Four", "Five"];
const GROUP_WORDS: Record<string, string> = { crafting: "Crafting", gathering: "Gathering", secondary: "Secondary" };

/** The confirm words of a preview: flat strings (Members' /api/members/set, the Me page's table save), never a page's guess. */
function sentencesOf(p: Saved): string[] {
  return ((p.sentences || []) as unknown[]).flatMap((x) => (typeof x === "string" ? [x] : (x as { sentences?: string[] })?.sentences || []));
}

/** "Cooking, First Aid and Fishing" */
function listWords(names: string[]): string {
  return names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** One character's professions edited in place (design §5.60): a row per profession — the profession picked from a grouped
 *  list (a trade already taken is greyed once `trade_slots` are full), its skill as a number 1..`skill_cap`, its notable
 *  recipes as chips with an Add box only where the profession `makes` things (a gathering trade and Fishing have none). Save asks the server first (`onSave(entries, { preview: true })`) and
 *  the confirm prints ONLY its `sentences`; then `{ preview: false }` writes. A refusal is the server's one sentence,
 *  shown in place; the draft is kept. */
export function ProfessionsEditor({ meta, label, professions, onSave, onCancel, onSaved }: {
  meta: Meta; label: string; professions?: Profession[];
  onSave: (entries: Profession[], opts: { preview: boolean }) => Promise<Saved>; onCancel: () => void; onSaved?: () => void;
}) {
  const defs = meta.professions?.list || [];
  const cap = meta.professions?.skill_cap || 300;
  const slots = meta.professions?.trade_slots ?? 2;
  const [draft, setDraft] = useState<DraftRow[]>(() => (professions || []).map((p) => ({ ...p, recipes: p.recipes.map((r) => ({ ...r })) })));
  const [typed, setTyped] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ask, dialog] = useConfirm();
  useDirty(`professions:${label}`, true);

  const def = (id: string) => defs.find((d) => d.id === id);
  const isTrade = (id: string) => ["crafting", "gathering"].includes(def(id)?.group || "");
  const tradesBut = (i: number) => draft.filter((p, j) => j !== i && isTrade(p.id)).length;
  /** The grouped list for row `i` (-1 = the Add a profession pick): each profession once; a trade greyed when the slots are full. */
  const options = (i: number) => {
    const groups: { group: string; items: { value: string; label: string; disabled?: boolean }[] }[] = [];
    for (const d of defs) {
      const taken = draft.some((p, j) => j !== i && p.id === d.id);
      const full = isTrade(d.id) && tradesBut(i) >= slots;
      const g = GROUP_WORDS[d.group] || d.group;
      let row = groups.find((x) => x.group === g);
      if (!row) groups.push((row = { group: g, items: [] }));
      row.items.push({ value: d.id, label: d.name, disabled: taken || full });
    }
    return groups;
  };
  const upd = (i: number, patch: Partial<DraftRow>) => { setError(null); setDraft(draft.map((p, j) => (j === i ? { ...p, ...patch } : p))); };
  const addRecipe = (i: number) => {
    const name = (typed[i] || "").trim().replace(/\s+/g, " ");
    if (!name) return;
    const p = draft[i];
    if (!p.recipes.some((r) => r.name.toLowerCase() === name.toLowerCase())) upd(i, { recipes: [...p.recipes, { name, id: null }] });
    setTyped({ ...typed, [i]: "" });
  };
  const full = draft.filter((p) => isTrade(p.id)).length >= slots;
  const stillOpen = defs.filter((d) => !isTrade(d.id) && !draft.some((p) => p.id === d.id)).map((d) => d.name);

  /** The draft as the server reads it: the skill a number again; an empty or unreadable box goes as 0, which the server
   *  refuses in its own sentence (never clamped here). */
  const entries = (): Profession[] => draft.map((p) => {
    const n = typeof p.skill === "number" ? p.skill : parseInt(String(p.skill).trim(), 10);
    return { ...p, skill: Number.isFinite(n) ? n : 0 };
  });

  async function save() {
    setBusy(true); setError(null);
    try {
      const p = await onSave(entries(), { preview: true });
      const said = sentencesOf(p);
      if (said.length && !(await ask({ title: `Save ${label}'s professions?`, message: said.join("\n"), confirmLabel: "Save professions" }))) return;
      const r = await onSave(entries(), { preview: false });
      ok(r.message || said.join("\n") || `${label}'s professions are saved.`);
      onSaved?.();
    } catch (e) { setError((e as Error).message || "Something went wrong"); } finally { setBusy(false); }
  }

  return (
    <Box p="md" style={{ background: "var(--mantine-color-slate-6)", borderRadius: 8, border: "1px solid var(--mantine-color-slate-5)" }}>
      <Stack gap="sm">
        <Text fw={600}>{label}'s professions</Text>
        {draft.length === 0 && <Text size="sm" c="dimmed">No professions on record. Add one below with its skill.</Text>}
        {draft.map((p, i) => {
          const d = def(p.id);
          return (
            <Stack key={i} gap={6} pb="sm" style={{ borderBottom: "1px solid var(--mantine-color-slate-5)" }}>
              <Group gap="sm" wrap="wrap" align="flex-end">
                <GameIcon meta={meta} kind="profession" id={p.id} size={30} title={d?.name || p.id} />
                <Select label="Profession" size="sm" w={{ base: "100%", xs: 200 }} data={options(i)} value={p.id} allowDeselect={false}
                  onChange={(v) => v && upd(i, { id: v, recipes: def(v)?.makes ? p.recipes : [] })} />
                <NumberInput label={`Skill, 1 to ${cap}`} size="sm" w={130} min={1} max={cap} clampBehavior="none" allowDecimal={false} allowNegative={false}
                  value={p.skill} onChange={(v) => upd(i, { skill: v })} />
                <ActionIcon variant="subtle" color="red" size="lg" aria-label={`Remove ${d?.name || p.id}`} onClick={() => { setError(null); setDraft(draft.filter((_, j) => j !== i)); }}>
                  <IconTrash size={17} />
                </ActionIcon>
              </Group>
              {!d?.makes ? (
                <Text size="xs" c="dimmed">{d?.group === "gathering" ? "A gathering trade has no recipes." : `${d?.name || p.id} has no recipes.`}</Text>
              ) : (
                <Group gap={6} wrap="wrap" align="center">
                  {p.recipes.map((r) => (
                    <Badge key={r.name} variant="outline" color="gray" radius="xl" tt="none" fw={500} pr={3}
                      rightSection={<ActionIcon size="xs" variant="transparent" color="gray" aria-label={`Remove ${r.name}`}
                        onClick={() => upd(i, { recipes: p.recipes.filter((x) => x.name !== r.name) })}><IconX size={11} /></ActionIcon>}>
                      {r.name}
                    </Badge>
                  ))}
                  <TextInput size="xs" w={{ base: "100%", xs: 220 }} placeholder="A notable recipe, e.g. Mooncloth Bag" aria-label={`Add a recipe to ${d?.name || p.id}`}
                    maxLength={RECIPE_MAX} value={typed[i] || ""} onChange={(e) => setTyped({ ...typed, [i]: e.currentTarget.value })}
                    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addRecipe(i); } }} />
                  <Button size="xs" variant="subtle" leftSection={<IconPlus size={13} />} disabled={!(typed[i] || "").trim()} onClick={() => addRecipe(i)}>Add</Button>
                </Group>
              )}
            </Stack>
          );
        })}
        <Stack gap={4}>
          <Select size="sm" w={{ base: "100%", xs: 260 }} placeholder="Add a profession" aria-label="Add a profession" data={options(-1)} value={null}
            onChange={(v) => { if (v) { setError(null); setDraft([...draft, { id: v, skill: 1, recipes: [] }]); } }} />
          {full && <Text size="xs" c="dimmed">{COUNT_WORDS[slots] || slots} trades are taken{stillOpen.length ? ` — ${listWords(stillOpen)} ${stillOpen.length === 1 ? "is" : "are"} still open.` : "."}</Text>}
        </Stack>
        {error && <Text size="sm" c="red">{error}</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={onCancel}>Cancel</Button>
          <Button size="sm" loading={busy} onClick={save}>Save {label}'s professions</Button>
        </Group>
      </Stack>
      {dialog}
    </Box>
  );
}
