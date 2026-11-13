import { useEffect, useState } from "react";
import { Box, Button, Group, Stack, Text } from "@mantine/core";
import { api, ApiError, type Meta, type RosterRow, type Seed, type SeedResult, type SeedRow } from "../../api";
import { useConfirm } from "../ConfirmModal";
import { useLive } from "../../hooks/useLive";
import { GameIcon } from "../Icons";
import { fail, ok } from "../Page";
import type { LinkTarget } from "../ArmoryLink";
import { classColour } from "../../theme";
import css from "../../pages/members.module.css";

type Pick = { uid: string; label: string; armory_id: number };

/** A roster character as the review shows it: class icon, the name in its class colour, the level. */
export function RosterChar({ meta, row }: { meta: Meta; row: RosterRow }) {
  return (
    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
      {row.cls && <GameIcon meta={meta} kind="class" id={row.cls} size={20} title={row.cls} />}
      <Text size="sm" fw={600} c={row.cls ? classColour(meta, row.cls) : undefined} className={css.charName}>{row.label}</Text>
      <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>{`level ${row.level}`}</Text>
    </Group>
  );
}

/** Link characters to the armory (spec §5.5, officers): the registered characters set against the linked guild's roster,
 *  in three groups, each explained in a sentence. **Sure**: one confirm links them all. **Likely**: each row with its best
 *  match and up to two others — **Confirm** or **Pick another…** (another of the matches, or a name looked up through the
 *  link dialog). **No match**: **Link to the armory…** per row. Every link previews first: the confirm prints ONLY the
 *  server's sentences and each pick it would refuse (its one sentence); the answer names each refusal. A review that
 *  moved since it was read (409) shows the new one with the server's sentence. The proposal reloads after every write. */
export function ArmorySeed({ meta, openLink, onChanged }: { meta: Meta; openLink: (t: LinkTarget) => void; onChanged: () => void }) {
  const [data, setData] = useState<Seed | null>(null);
  const [failed, setFailed] = useState(false);
  const [changed, setChanged] = useState<string | null>(null);  // the server's "it changed since you looked" sentence
  const [busy, setBusy] = useState<string | null>(null);
  const [other, setOther] = useState<string | null>(null);  // the likely row whose other matches are open
  const [ask, confirmDialog] = useConfirm();
  const load = () => api.get<Seed>("/api/armory/seed").then((d) => { setData(d); setFailed(false); }).catch((e) => { setFailed(true); fail(e); });
  useEffect(() => { load(); }, []);
  useLive(() => { if (busy === null) load(); }, ["member"]);

  /** Preview (the server's sentences and each refused pick's) → confirm → link → the fresh proposal from the answer. */
  async function apply(picks: Pick[], key: string, title: string, confirmLabel: string) {
    if (!data) return;
    setBusy(key); setChanged(null);
    try {
      const p = await api.post<{ sentences: string[]; results: SeedResult[] }>("/api/armory/seed/apply", { rev: data.rev, picks, preview: true });
      const refused = p.results.filter((r) => !r.ok).map((r) => r.text);
      const said = [...p.sentences, ...refused];  // the server's: its count, then each pick it would refuse
      if (!p.results.some((r) => r.ok)) { fail(new Error(said.join("\n"))); return; }
      if (!(await ask({ title, message: said.join("\n"), confirmLabel }))) return;
      const x = await api.post<Seed & { message: string; linked: number; results: SeedResult[] }>("/api/armory/seed/apply", { rev: data.rev, picks });
      setData(x); setOther(null);
      if (x.results.every((r) => r.ok)) ok(x.message); else fail(new Error(x.message));
      onChanged();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.body && typeof e.body === "object" && "rev" in e.body) {
        setData(e.body as Seed); setChanged(e.message);
      } else { fail(e); load(); }
    } finally { setBusy(null); }
  }

  if (!data) {
    return failed
      ? <Group gap="sm" p="md"><Text size="sm">The proposed links couldn't be loaded. Reload to try again.</Text><Button size="xs" variant="light" onClick={() => load()}>Reload</Button></Group>
      : <Text size="sm" c="dimmed" p="md">Reading the guild's roster…</Text>;
  }
  if (data.roster.words) return <Text size="sm" p="md">{data.roster.words}</Text>;

  const pick = (r: SeedRow, row: RosterRow): Pick => ({ uid: r.uid, label: r.label, armory_id: row.id });
  const target = (r: SeedRow): LinkTarget => ({ uid: r.uid, label: r.label, name: r.label.split(" ")[0], surname: null });
  const who = (r: SeedRow) => (
    <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
      <GameIcon meta={meta} kind="class" id={r.cls} size={20} title={r.cls} />
      <Text size="sm" fw={600} c={classColour(meta, r.cls)} className={css.charName}>{r.label}</Text>
      <Text size="xs" c="dimmed" className={css.charName}>{r.member}</Text>
    </Group>
  );
  const head = (title: string, n: number, why: string) => (
    <Box className={css.seedHead}>
      <Text fw={600} size="sm">{`${title} · ${n}`}</Text>
      <Text size="xs" c="dimmed">{why}</Text>
    </Box>
  );

  return (
    <Stack gap={0}>
      {changed && <Text size="sm" c="red" px="md" pt="sm">{changed}</Text>}

      {head("Sure", data.sure.length, "Same first name and class as exactly one character in the guild, and nobody else here could be it.")}
      {data.sure.map((r) => (
        <div key={`${r.uid}:${r.label}`} className={css.seedRow}>
          {who(r)}{r.candidate && <RosterChar meta={meta} row={r.candidate} />}<span />
        </div>
      ))}
      {data.sure.length ? (
        <Group px="md" py="sm" justify="flex-end">
          <Button loading={busy === "sure"} disabled={busy !== null && busy !== "sure"}
            onClick={() => apply(data.sure.filter((r) => r.candidate).map((r) => pick(r, r.candidate as RosterRow)), "sure", `Link ${data.sure.length} sure ${data.sure.length === 1 ? "match" : "matches"} to the armory?`, "Link them")}>
            Link the sure matches…
          </Button>
        </Group>
      ) : <Text size="sm" c="dimmed" px="md" py="sm">No sure matches right now. Check the likely ones below.</Text>}

      {head("Likely", data.likely.length, "Same class and a close name, so check each one: confirm it or pick another.")}
      {data.likely.map((r) => {
        const k = `${r.uid}:${r.label}`;
        return (
          <div key={k} className={css.seedRow}>
            {who(r)}
            <Stack gap={2} style={{ minWidth: 0 }}>
              {r.candidate && <RosterChar meta={meta} row={r.candidate} />}
              {r.why && <Text size="xs" c="dimmed">{r.why}</Text>}
              {other === k && (
                <Stack gap={4} mt={4}>
                  {r.others.map((o) => (
                    <Group key={o.id} gap="xs" wrap="wrap">
                      <RosterChar meta={meta} row={o} />
                      <Button size="compact-sm" variant="light" loading={busy === `${k}:${o.id}`} disabled={busy !== null}
                        onClick={() => apply([pick(r, o)], `${k}:${o.id}`, `Link ${r.label} to ${o.label}?`, "Link it")}>Link this one</Button>
                    </Group>
                  ))}
                  <Button size="compact-sm" variant="subtle" px={0} onClick={() => openLink(target(r))}>Look it up by name…</Button>
                </Stack>
              )}
            </Stack>
            <Group gap="xs" wrap="wrap" justify="flex-end">
              {r.candidate && <Button size="compact-sm" loading={busy === k} disabled={busy !== null}
                onClick={() => apply([pick(r, r.candidate as RosterRow)], k, `Link ${r.label} to ${r.candidate?.label}?`, "Link it")}>Confirm</Button>}
              <Button size="compact-sm" variant="default" aria-expanded={other === k} onClick={() => setOther(other === k ? null : k)}>Pick another…</Button>
            </Group>
          </div>
        );
      })}
      {!data.likely.length && <Text size="sm" c="dimmed" px="md" py="sm">No likely matches to check right now.</Text>}

      {head("No match", data.none.length, "Nothing on the guild's roster looks like these. Link one by name here, or its member links it from the Me page.")}
      {data.none.map((r) => (
        <div key={`${r.uid}:${r.label}`} className={css.seedRow}>
          {who(r)}<span />
          <Group justify="flex-end"><Button size="compact-sm" variant="default" onClick={() => openLink(target(r))}>Link to the armory…</Button></Group>
        </div>
      ))}
      {!data.none.length && <Text size="sm" c="dimmed" px="md" py="sm">Every registered character has a match on the roster or is linked already.</Text>}
      {confirmDialog}
    </Stack>
  );
}
