import { useEffect, useRef, useState } from "react";
import { Select, Stack, Text, type StackProps } from "@mantine/core";
import { IconSearch } from "@tabler/icons-react";
import { api, type CatalogRecipe, type FindResult } from "../api";


/** professions.NOT_A_RECIPE — the server's own words, for the one case its answer names only recipes nobody can pick. */
const NOT_A_RECIPE = "That isn't a recipe in the game's list.";
/** A /find that failed (the bot unreachable, a dropped connection): said in the box, never "Searching…" forever. */
const SEARCH_FAILED = "The search couldn't be done just now. Change a letter to try again.";

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
  /** The answer and the (profession, query) it answers: an answer is shown only for the query and profession on screen now,
   *  so an in-flight answer for an older query or another profession is never listed or pickable. */
  const [found, setFound] = useState<{ key: string; d: FindResult } | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const key = `${profession}\n${search.trim()}`;
  const want = useRef(key);
  want.current = key;
  /** Mantine's Select resets a controlled search to "" on mount (value={null}) and on blur. Until the person has typed,
   *  an empty reset is ignored, so `initial` (Pick the recipe…'s typed name) stays in the box. */
  const typed = useRef(false);
  const onSearch = (v: string) => {
    if (!v && !typed.current) return;
    typed.current = true;
    setSearch(v);
  };

  useEffect(() => {
    const q = search.trim();
    if (!q) return;
    const asked = key;
    const t = setTimeout(() => {
      api.get<FindResult>(`/api/professions/find?q=${encodeURIComponent(q)}&profession=${encodeURIComponent(profession)}`)
        .then((d) => { if (want.current === asked) { setFound({ key: asked, d }); setFailedKey(null); } })
        .catch(() => { if (want.current === asked) setFailedKey(asked); });
    }, 250);
    return () => clearTimeout(t);
  }, [key]);

  const current = found && found.key === key ? found.d : null;  // never an answer to another query or profession
  const failed = failedKey === key;
  const listed = (current?.recipes || []).filter((r) => r.listed !== false && r.id != null);
  const open = listed.filter((r) => !held?.(r));
  const byId = new Map(open.map((r) => [String(r.id), r]));
  const waiting = !!search.trim() && !current && !failed;
  const empty = !search.trim() ? undefined
    : failed ? SEARCH_FAILED
    : waiting ? "Searching the game's list…"
    : listed.length && !open.length ? heldSentence
    : !listed.length && current?.recipes.length ? NOT_A_RECIPE  // only typed recipes the list lacks matched: none to pick
    : current?.message;

  return (
    <Stack gap={4} w={w}>
      <Select size="sm" label={label} aria-label={ariaLabel} placeholder="Part of the recipe's name, like ironfeather" leftSection={<IconSearch size={14} />}
        searchable value={null} searchValue={search} onSearchChange={onSearch} filter={({ options }) => options}
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
      {current?.more && open.length > 0 && <Text size="xs" c="dimmed">{current.more} Type more of the name to narrow it.</Text>}
    </Stack>
  );
}
