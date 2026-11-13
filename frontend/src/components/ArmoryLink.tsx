import { useEffect, useRef, useState } from "react";
import { Badge, Button, Group, Modal, Stack, Text, TextInput, Tooltip } from "@mantine/core";
import { IconShield, IconShieldCheck } from "@tabler/icons-react";
import { api, ApiError, type ArmoryFound, type Character, type Meta } from "../api";
import { useDirty } from "../hooks/useDirty";
import { useConfirm } from "./ConfirmModal";
import { GameIcon } from "./Icons";
import { fail, ok } from "./Page";
import { classColour } from "../theme";

/** The two faces of the armory verbs (web/api_armory.py): a member's own characters, or an officer's for anyone (`uid`). */
export type ArmoryBase = "/api/me/armory" | "/api/members/armory";

/** A named, active character can be linked; a planned one has nothing to look up. */
export const linkable = (c: Character) => c.status === "active" && !!c.name;
/** The armory keeps this character's professions: they are shown, never edited by hand. */
export const synced = (c: Character) => !!c.armory?.linked && c.profession_sync !== false;

/** "Linked to the armory · Updated <when> · in the guild" (spec §3, §5.4): the server's words joined — `when` is already
 *  12-hour words, `in_guild` null (not known) says nothing; then "professions kept by hand" when the switch is off. */
export function armoryWords(c: Character): string {
  const a = c.armory;
  if (!a || !a.linked) return "Not linked to the armory yet.";
  const bits = ["Linked to the armory", a.when ? `Updated ${a.when}` : "Not updated from the armory yet"];
  if (a.in_guild !== null) bits.push(a.in_guild ? "in the guild" : "not in the guild");
  if (c.profession_sync === false) bits.push("professions kept by hand");
  return bits.join(" · ");
}

/** Why a synced character's professions can't be edited here, and what to do (spec §5.4): the server's "Updated from the
 *  armory <when>" then the action's own label. */
export const syncedSentence = (c: Character) => `${c.armory?.updated || "Not updated from the armory yet"}. Keep professions by hand to edit them.`;

/** Officers' flags as short worded badges (spec §5.4): the armory shows another class, the armory no longer finds it, not in
 *  the guild (information, not an error — grey). */
export function ArmoryFlags({ c }: { c: Character }) {
  const fl = c.flags || [];
  if (!fl.length) return null;
  return (
    <>
      {fl.includes("class") && <Badge size="sm" variant="light" color="red" tt="none" title="The record keeps the class it was registered with.">{`the armory shows a ${c.armory?.cls || "different class"}`}</Badge>}
      {fl.includes("missing") && <Badge size="sm" variant="light" color="red" tt="none" title="For a day or more. The link stays until someone unlinks it.">the armory no longer finds it</Badge>}
      {fl.includes("outside") && <Badge size="sm" variant="outline" color="gray" tt="none" title="On the armory, but not on the linked guild's roster.">not in the guild</Badge>}
    </>
  );
}

/** Members' character line: the armory mark alone (a shield; the tooltip carries the words — the Iconography rule), then
 *  the flags as badges. Nothing where the version has no armory, for a puppet or a planned character. */
export function ArmoryMark({ c }: { c: Character }) {
  if (!c.armory || !linkable(c)) return null;
  const linked = c.armory.linked;
  return (
    <>
      <Tooltip label={armoryWords(c)} multiline maw={280} events={{ hover: true, focus: true, touch: true }}>
        <span style={{ display: "inline-flex" }} aria-label={armoryWords(c)}>
          {linked ? <IconShieldCheck size={16} color="var(--mantine-color-teal-4)" /> : <IconShield size={16} color="var(--mantine-color-gray-6)" />}
        </span>
      </Tooltip>
      <ArmoryFlags c={c} />
    </>
  );
}

/** One armory action for a character's ⋮ (Me: a Menu.Item; Members: its Item with a why). */
export type ArmoryAction = { key: string; label: string; color?: string; run: () => void };

/** The ⋮ spinner's key: the member and the character (labels are unique, but the key never leans on it). */
const busyKey = (uid: string | undefined, label: string) => `${uid || "me"}\u0000${label}`;

/** The armory verbs on one character, for either page: Link to the armory… (the lookup dialog), Unlink…, Keep professions
 *  by hand / Follow the armory again (the switch) and Sync now. Unlink and the switch preview first and the confirm prints
 *  ONLY the server's sentences; Sync now has no confirm (a refresh) and says the server's sentence. `uid` = the member an
 *  officer acts for (Members); the Me page sends none (the session's own record). The ⋮ spins only while a request is
 *  out (`isBusy`), never while the confirm waits. `characterOf` gives the link dialog a member's record (Members: the
 *  first name and surname the review's rows lack). Render `dialogs` once. */
export function useArmoryActions(meta: Meta, base: ArmoryBase, onChanged: () => void) {
  const [ask, confirmDialog] = useConfirm();
  const [linking, setLinking] = useState<LinkTarget | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function confirmed(path: string, body: Record<string, unknown>, title: string, confirmLabel: string, color?: string) {
    const k = busyKey(body.uid as string | undefined, String(body.label));
    setBusy(k);
    try {
      const p = await api.post<{ sentences: string[] }>(path, { ...body, preview: true });
      setBusy(null);
      if (!(await ask({ title, message: p.sentences.join("\n"), confirmLabel, color }))) return;
      setBusy(k);
      const x = await api.post<{ message: string }>(path, body);
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); onChanged(); }
  }
  async function syncNow(body: Record<string, unknown>) {
    setBusy(busyKey(body.uid as string | undefined, String(body.label)));
    try {
      const x = await api.post<{ result: string; message: string }>(`${base}/sync`, body);
      ok(x.message);
    } catch (e) { fail(e); } finally { setBusy(null); onChanged(); }
  }

  /** The actions that apply to `c` now (none where the version has no armory or the character is planned). */
  function actions(c: Character, uid?: string): ArmoryAction[] {
    if (!meta.armory || !c.armory || !linkable(c)) return [];
    const who = uid ? { uid } : {};
    const body = { ...who, label: c.label };
    if (!c.armory.linked)
      return [{ key: "link", label: "Link to the armory…", run: () => setLinking({ uid, label: c.label, name: c.name, surname: c.surname }) }];
    const on = c.profession_sync === false;
    return [
      { key: "sync", label: "Sync now", run: () => syncNow(body) },
      on
        ? { key: "follow", label: "Follow the armory again", run: () => confirmed(`${base}/switch`, { ...body, on: true }, `Follow the armory for ${c.label}'s professions?`, "Follow the armory") }
        : { key: "by_hand", label: "Keep professions by hand", run: () => confirmed(`${base}/switch`, { ...body, on: false }, `Keep ${c.label}'s professions by hand?`, "Keep them by hand") },
      { key: "unlink", label: "Unlink…", color: "red", run: () => confirmed(`${base}/unlink`, body, `Unlink ${c.label} from the armory?`, "Unlink it", "red") },
    ];
  }

  const dialogs = (
    <>
      <LinkDialog meta={meta} base={base} target={linking} onClose={() => setLinking(null)} onLinked={() => { setLinking(null); onChanged(); }} />
      {confirmDialog}
    </>
  );
  return { actions, isBusy: (c: Character, uid?: string) => busy === busyKey(uid, c.label), dialogs, openLink: setLinking };
}

/** Who and what the link dialog looks up: the character (`label`, its first name and surname as the record has them) and,
 *  for an officer, the member (`uid`). */
export type LinkTarget = { uid?: string; label: string; name: string | null; surname: string | null };

type Looked = { key: string; found: ArmoryFound; sentences: string[] };

/** Link to the armory… (spec §5.2): the first name and surname prefilled from the record — prose, validated by the server —
 *  then **Look it up** shows what the armory found (name, class icon, level) with the server's sentences, or its refusal
 *  in one sentence (not found, a name already taken, too many lookups, the armory down). **Link it** links exactly the
 *  character on screen (its `armory_id`); a name that now finds another answers 409 and asks for a new look. An answer for
 *  another character or for text edited meanwhile is dropped (the key = member · character · first name · surname).
 *  Enter looks up only when no request is out. Nothing is linked without a lookup's result on screen. */
export function LinkDialog({ meta, base, target, onClose, onLinked }: { meta: Meta; base: ArmoryBase; target: LinkTarget | null; onClose: () => void; onLinked: () => void }) {
  const [first, setFirst] = useState("");
  const [surname, setSurname] = useState("");
  const [looked, setLooked] = useState<Looked | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"look" | "link" | null>(null);
  const typed = useRef("");  // the character and the boxes as they are now: an answer for anything else is ignored
  const startFirst = target?.name || target?.label.split(" ")[0] || "";
  const startSurname = target?.surname || "";
  const key = [target?.uid || "", target?.label || "", first.trim(), surname.trim()].join("\u0000");
  typed.current = key;
  useDirty("armory-link", target !== null && (first.trim() !== startFirst.trim() || surname.trim() !== startSurname.trim() || looked !== null));
  useEffect(() => {
    setLooked(null); setError(null); setBusy(null);
    setFirst(startFirst);
    setSurname(startSurname);
  }, [target]);  // eslint-disable-line react-hooks/exhaustive-deps

  const who = target?.uid ? { uid: target.uid } : {};
  async function lookUp() {
    if (!target || !first.trim() || busy !== null) return;
    const asked = key;
    setBusy("look"); setError(null); setLooked(null);
    try {
      const r = await api.post<{ found: ArmoryFound; sentences: string[] }>(`${base}/find`, { ...who, label: target.label, name: first.trim(), surname: surname.trim() || null });
      if (typed.current === asked) setLooked({ key: asked, found: r.found, sentences: r.sentences });
    } catch (e) {
      if (typed.current === asked) setError((e as Error).message);
    } finally { setBusy(null); }
  }
  async function link() {
    if (!target || !looked || looked.key !== key || busy !== null) return;
    setBusy("link"); setError(null);
    try {
      const r = await api.post<{ message: string }>(`${base}/link`, { ...who, label: target.label, name: first.trim(), surname: surname.trim() || null, armory_id: looked.found.id });
      ok(r.message);
      onLinked();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setLooked(null);  // the name now finds another character: look again
      setError((e as Error).message);
    } finally { setBusy(null); }
  }

  const f = looked?.key === key ? looked.found : null;
  return (
    <Modal opened={target !== null} onClose={onClose} title={target ? `Link ${target.label} to the armory` : ""} centered>
      <Stack gap="md">
        <Text size="sm" c="dimmed">Type the name as the armory shows it and look it up. Nothing is linked until you press Link it.</Text>
        <Group gap="sm" grow wrap="wrap" align="flex-start">
          <TextInput label="First name" maxLength={40} value={first} onChange={(e) => { setFirst(e.currentTarget.value); setError(null); }}
            onKeyDown={(e) => { if (e.key === "Enter") lookUp(); }} style={{ minWidth: 140 }} />
          <TextInput label="Surname" description="Leave it empty if the character has none." maxLength={40} value={surname}
            onChange={(e) => { setSurname(e.currentTarget.value); setError(null); }} onKeyDown={(e) => { if (e.key === "Enter") lookUp(); }} style={{ minWidth: 140 }} />
        </Group>
        <Group justify="flex-end">
          <Button variant="light" loading={busy === "look"} disabled={!first.trim() || busy === "link"} onClick={lookUp}>Look it up</Button>
        </Group>
        {f && looked && (
          <Stack gap="xs" p="sm" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, minWidth: 0 }}>
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              {f.cls && <GameIcon meta={meta} kind="class" id={f.cls} size={30} title={f.cls} />}
              <Text fw={700} c={f.cls ? classColour(meta, f.cls) : undefined} truncate title={f.label} style={{ minWidth: 0 }}>{f.label}</Text>
              <Text size="sm" c="dimmed" style={{ whiteSpace: "nowrap" }}>{`level ${f.level}`}</Text>
            </Group>
            <Text size="sm" style={{ whiteSpace: "pre-line" }}>{looked.sentences.join("\n")}</Text>
          </Stack>
        )}
        {error && <Text size="sm" c="red">{error}</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy === "link"} disabled={!f || busy === "look"} onClick={link}>Link it</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
