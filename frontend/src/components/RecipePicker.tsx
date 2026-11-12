import { useEffect, useRef, useState } from "react";
import { Select, Stack, Text, type StackProps } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";
import { api, type CatalogRecipe, type FindResult } from "../api";
import { fail } from "./Page";

/** professions.NOT_A_RECIPE — the server's own words, for the one case its answer names only recipes nobody can pick. */
const NOT_A_RECIPE = "That isn't a recipe in the game's list.";

/** A search over the game's recipe list within ONE profession (design §5.64): part of a name is typed, the server's finder
 *  answers (GET /api/professions/find?profession=), and a recipe is picked from the matches — never a typed name. Two
 *  recipes with one name are two options told apart by their category (R6). `held` = recipes left out (the character
 *  already has them) and `heldSentence` what to say when every match is one of them. The empty answer and "how many more"
 *  are the server's sentences; `initial` = the search to start from. Used by the professions editor and the Professions page's Pick the recipe…. */
export function RecipePicker({ profession, onPick, label, ariaLabel, held, heldSentence, w, initial }: {
  profession: string; onPick: (r: CatalogRecipe) => void; label?: string; ariaLabel: string;
  held?: (r: CatalogRecipe) => boolean; heldSentence?: string; w?: StackProps["w"]; initial?: string;
}) {
  const [search, setSearch] = useState(initial || "");
  const [found, setFound] = useState<FindResult | null>(null);
  const asked = useRef("");
  asked.current = search;

  useEffect(() => {
    const q = search.trim();
    if (!q) { setFound(null); return; }
    const t = setTimeout(() => {
      api.get<FindResult>(`/api/professions/find?q=${encodeURIComponent(q)}&profession=${encodeURIComponent(profession)}`)
        .then((d) => { if (d.q.trim() === asked.current.trim()) setFound(d); }).catch(fail);
    }, 250);
    return () => clearTimeout(t);
  }, [search, profession]);

  const listed = (found?.recipes || []).filter((r) => r.listed !== false && r.id != null);
  const open = listed.filter((r) => !held?.(r));
  const byId = new Map(open.map((r) => [String(r.id), r]));
  const waiting = !!search.trim() && (found === null || found.q.trim() !== search.trim());
  const empty = !search.trim() ? undefined
    : waiting ? "Searching the game's list…"
    : listed.length && !open.length ? heldSentence
    : !listed.length && found?.recipes.length ? NOT_A_RECIPE  // only typed recipes the list lacks matched: none to pick
    : found?.message;

  return (
    <Stack gap={4} w={w}>
      <Select size="sm" label={label} aria-label={ariaLabel} placeholder="Part of the recipe's name, like ironfeather" leftSection={<IconSearch size={14} />}
        searchable value={null} searchValue={search} onSearchChange={setSearch} filter={({ options }) => options}
        data={open.map((r) => ({ value: String(r.id), label: r.name }))} nothingFoundMessage={empty} maxDropdownHeight={320}
        renderOption={({ option }) => {
          const r = byId.get(option.value);
          return (
            <div>
              <Text size="sm">{option.label}</Text>
              {r?.category && <Text size="xs" c="dimmed">{r.category}</Text>}
            </div>
          );
        }}
        onChange={(v) => { const r = v ? byId.get(v) : undefined; if (r) { onPick(r); setSearch(""); setFound(null); } }} />
      {!waiting && found?.more && open.length > 0 && <Text size="xs" c="dimmed">{found.more} Type more of the name to narrow it.</Text>}
    </Stack>
  );
}
