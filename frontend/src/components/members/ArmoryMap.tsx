import { useEffect, useState } from "react";
import { Box, Button, Card, Collapse, Group, Loader, Modal, Select, Stack, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { api, ApiError, type ArmoryFound, type ArmoryMap as MapData, type MemberRow, type Meta, type RosterRow } from "../../api";
import { useHeldLive } from "../../hooks/useLive";
import { useDirty } from "../../hooks/useDirty";
import { linkable, type LinkTarget } from "../ArmoryLink";
import { CardHeader, fail, ok } from "../Page";
import { ArmorySeed, RosterChar } from "./ArmorySeed";
import css from "../../pages/members.module.css";

/** The armory on the Members page (spec §5.4, §5.5; officers, only when /api/meta.armory): the guild's roster as last
 *  read and how many characters are linked; **Link characters to the armory…** opens the review of proposed links
 *  (ArmorySeed) in place; the fold **In the guild, not claimed (N)** lists the roster's characters no record holds, each
 *  with **Give to a member…**. Without a roster the card says the server's sentence (no guild linked / not read yet).
 *  Each character's own link is on its row (ArmoryMark). Reloads on the `member` topic (a write, a roster read), at most
 *  every few seconds and never while Give to a member… is open. */
export function ArmoryMap({ meta, rows, openLink, onChanged }: { meta: Meta; rows: MemberRow[]; openLink: (t: LinkTarget) => void; onChanged: () => void }) {
  const [d, setD] = useState<MapData | null>(null);
  const [failed, setFailed] = useState(false);
  const [review, setReview] = useState(false);
  const [open, setOpen] = useState(false);
  const [give, setGive] = useState<RosterRow | null>(null);
  const load = () => api.get<MapData>("/api/armory/map").then((x) => { setD(x); setFailed(false); }).catch(() => setFailed(true));  // the inline sentence, never a toast on every `member` event
  useEffect(() => { load(); }, []);
  useHeldLive(load, ["member"], give !== null);  // at most every few seconds, never while Give to a member… is open

  if (!d) {
    return (
      <Card>
        {failed
          ? <Group p="md" gap="sm" justify="space-between" wrap="wrap"><Text size="sm">The armory's view of the guild couldn't be loaded. Reload to try again.</Text>
              <Button size="xs" variant="light" onClick={() => load()}>Reload</Button></Group>
          : <Text c="dimmed" p="md">Loading…</Text>}
      </Card>
    );
  }
  const r = d.roster;
  const n = d.counts;
  return (
    <Card p={0} className={css.touch}>
      <CardHeader title="The armory" hint={r.read && r.guild ? `${r.guild}'s roster, read ${r.read}` : undefined} />
      {r.words ? <Text size="sm" p="md">{r.words}</Text> : (
        <>
          <Group px="md" py="sm" gap="sm" justify="space-between" wrap="wrap">
            <Text size="sm">{`${n.linked} ${n.linked === 1 ? "character is" : "characters are"} linked to the armory, ${n.not_linked} not yet.`}</Text>
            <Button variant={review ? "light" : "default"} aria-expanded={review} onClick={() => setReview(!review)}>
              {review ? "Close the proposed links" : "Link characters to the armory…"}
            </Button>
          </Group>
          {review && <Box style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}><ArmorySeed meta={meta} rows={rows} openLink={openLink} onChanged={() => { load(); onChanged(); }} /></Box>}
          <UnstyledButton className={css.fold} aria-expanded={open} onClick={() => setOpen(!open)} style={{ borderTop: "1px solid var(--mantine-color-slate-5)" }}>
            {open ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
            <Text fw={600} size="sm">{`In the guild, not claimed (${n.unclaimed})`}</Text>
            <Text size="xs" c="dimmed">characters on the guild's roster that no record here is linked to</Text>
          </UnstyledButton>
          <Collapse expanded={open}>
            {d.unclaimed.map((x) => (
              <div key={x.id} className={css.seedRow}>
                <RosterChar meta={meta} row={x} /><span />
                <Group justify="flex-end"><Button size="compact-sm" variant="default" onClick={() => setGive(x)}>Give to a member…</Button></Group>
              </div>
            ))}
            {!d.unclaimed.length && <Text size="sm" c="dimmed" px="md" py="sm">Every character on the guild's roster belongs to a member here.</Text>}
          </Collapse>
        </>
      )}
      <GiveDialog meta={meta} row={give} rows={rows} onClose={() => setGive(null)} onDone={() => { load(); onChanged(); }} />
    </Card>
  );
}

const NEW = "\u0000new";  // the "a new character" choice (a label never holds a NUL)
type Said = { key: string; found: ArmoryFound; sentences: string[] } | { key: string; error: string } | null;

/** Give to a member… (spec §5.4): the member picked from the page's list (never a typed id), then EITHER one of their
 *  characters that could be it (same class, named, not linked yet) OR a new character with its spec picked from the
 *  class's specs (registered for them through the officers' add path). The server's preview prints under the picks (its
 *  sentences, or its one-sentence refusal); **Give it** writes. When the answer says the character was registered but not
 *  linked, that sentence is shown as it is and the page reloads. */
function GiveDialog({ meta, row, rows, onClose, onDone }: { meta: Meta; row: RosterRow | null; rows: MemberRow[]; onClose: () => void; onDone: () => void }) {
  const [uid, setUid] = useState<string | null>(null);
  const [which, setWhich] = useState<string | null>(null);
  const [spec, setSpec] = useState<string | null>(null);
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);  // the write's refusal, beside the preview it was sent for: Give it can be pressed again
  useDirty("armory-give", row !== null && uid !== null);
  useEffect(() => { setUid(null); setWhich(null); setSpec(null); setSaid(null); setRefused(null); }, [row]);

  const member = rows.find((m) => m.uid === uid) || null;
  const could = member && row ? member.characters.filter((c) => linkable(c) && !c.armory?.linked && c.cls === row.cls) : [];
  const specs = row?.cls ? Object.entries(meta.classes[row.cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` })) : [];
  const body = (): Record<string, unknown> | null => {
    if (!row || !uid || !which) return null;
    if (which !== NEW) return { uid, armory_id: row.id, label: which };
    if (specs.length && !spec) return null;
    return { uid, armory_id: row.id, ...(spec ? { spec } : {}) };
  };
  const b = body();
  const key = JSON.stringify(b);
  useEffect(() => {
    if (!b) { setSaid(null); return; }
    let live = true;  // an answer for earlier picks is dropped
    setSaid(null); setRefused(null);
    api.post<{ found: ArmoryFound; sentences: string[] }>("/api/members/armory/give", { ...b, preview: true })
      .then((p) => { if (live) setSaid({ key, found: p.found, sentences: p.sentences }); })
      .catch((e) => { if (live) setSaid({ key, error: (e as Error).message }); });
    return () => { live = false; };
  }, [key]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function go() {
    if (!b || !said || "error" in said || said.key !== key) return;
    setBusy(true); setRefused(null);
    try {
      const x = await api.post<{ registered: boolean; linked: boolean; message: string }>("/api/members/armory/give", b);
      if (x.linked) ok(x.message); else fail(new Error(x.message));  // registered but not linked: exactly the server's sentence
      onClose();
    } catch (e) {
      if (e instanceof ApiError) setRefused(e.message); else fail(e);
    } finally { setBusy(false); onDone(); }
  }

  const people = rows.filter((m) => !m.left && !m.test).map((m) => ({ value: m.uid, label: m.display_name }));
  const options = [...could.map((c) => ({ value: c.label, label: `${c.label}, their ${c.is_main ? "main" : "alt"}` })),
    { value: NEW, label: "A new character, registered for them" }];
  return (
    <Modal opened={row !== null} onClose={onClose} title={row ? `Give ${row.label} to a member` : ""} centered>
      <Stack gap="md" className={css.touch}>
        <Select label="The member" placeholder="Pick a member" searchable data={people} value={uid}
          onChange={(v) => { setUid(v); setWhich(null); setSpec(null); }}
          nothingFoundMessage="Nobody by that name has a record here. They register with /register first." />
        {member && (
          <Select label="Which of their characters it is" data={options} value={which} onChange={(v) => { setWhich(v); setSpec(null); }}
            description={could.length ? undefined : `${member.display_name} has no unlinked character of this class, so pick A new character to register it for them.`} />
        )}
        {which === NEW && specs.length > 0 && <Select label="Spec" placeholder="Pick its spec" data={specs} value={spec} onChange={setSpec} />}
        {b && said === null && <Loader size="xs" />}
        {said && "sentences" in said && <Text size="sm" style={{ whiteSpace: "pre-line" }}>{said.sentences.join("\n")}</Text>}
        {said && "error" in said && <Text size="sm" c="red">{said.error}</Text>}
        {refused && <Text size="sm" c="red" style={{ whiteSpace: "pre-line" }}>{refused}</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!said || "error" in said} onClick={go}>Give it</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
