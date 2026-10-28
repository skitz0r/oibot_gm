import { useEffect, useState } from "react";
import { Button, Group, Loader, Modal, MultiSelect, Select, Stack, Text } from "@mantine/core";
import { api, ApiError, type Character, type MemberRow, type Meta, type PeoplePreview } from "../../api";
import { GameIcon } from "../Icons";
import { flexChoices } from "../CharacterTable";
import { fail, ok } from "../Page";
import { classColour } from "../../theme";
import css from "../../pages/members.module.css";

/** What a pick dialog changes: the member's rank (member ⋮ → Rank…), a character's spec and offspec, or its extra roles
 *  (character ⋮ → Spec… / Extra roles…). */
export type Pick = { kind: "rank"; r: MemberRow } | { kind: "spec"; r: MemberRow; c: Character } | { kind: "flex"; r: MemberRow; c: Character };

type Said = { sentences: string[]; rev?: string } | { error: string } | null;

const errorOf = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

/** One pick → the server's preview under it → the write (design §5.58). Every value change asks POST /api/members/set
 *  with `preview` and prints ONLY its sentences (consequences.rank_consequence / spec_consequence / flex_consequence) or
 *  its one-sentence refusal; the button writes with the rev that preview returned (409 when the record moved). */
export function PickModal({ meta, pick, rankChoices, onClose, onDone }: { meta: Meta; pick: Pick | null; rankChoices: string[]; onClose: () => void; onDone: () => void }) {
  const [rank, setRank] = useState<string | null>(null);
  const [spec, setSpec] = useState<string | null>(null);
  const [off, setOff] = useState<string | null>(null);
  const [flex, setFlex] = useState<string[]>([]);
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setSaid(null);
    if (!pick) return;
    if (pick.kind === "rank") setRank(pick.r.rank || null);
    else { setSpec(pick.c.spec); setOff(pick.c.offspec); setFlex([...(pick.c.flex || [])]); }
  }, [pick]);

  const body = (): Record<string, unknown> | null => {
    if (!pick) return null;
    if (pick.kind === "rank") return rank && rank !== pick.r.rank ? { rank } : null;
    if (pick.kind === "spec") return spec && (spec !== pick.c.spec || (off || null) !== (pick.c.offspec || null)) ? { character: pick.c.label, spec, offspec: off || "" } : null;
    const choices = flexChoices(meta, pick.c.cls, pick.c.spec, pick.c.offspec);
    const now = flex.filter((x) => choices.includes(x));
    const was = (pick.c.flex || []).filter((x) => choices.includes(x));
    return now.length !== was.length || now.some((x) => !was.includes(x)) ? { character: pick.c.label, flex: now } : null;
  };
  const b = body();
  const key = JSON.stringify(b);
  useEffect(() => {
    if (!pick || !b) { setSaid(null); return; }
    let live = true;
    setSaid(null);
    api.post<PeoplePreview>("/api/members/set", { uid: pick.r.uid, rev: pick.r.characters_rev, ...b, preview: true })
      .then((p) => { if (live) setSaid({ sentences: p.sentences, rev: p.rev }); })
      .catch((e) => { if (live) setSaid({ error: errorOf(e) }); });
    return () => { live = false; };
  }, [key, pick]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function go() {
    if (!pick || !b || !said || "error" in said) return;
    setBusy(true);
    try {
      const x = await api.post<{ message: string }>("/api/members/set", { uid: pick.r.uid, rev: said.rev || pick.r.characters_rev, ...b });
      ok(x.message); onClose();
    } catch (e) { fail(e); } finally { setBusy(false); onDone(); }
  }

  const title = !pick ? "" : pick.kind === "rank" ? `${pick.r.display_name}'s rank` : pick.kind === "spec" ? `${pick.c.label}'s spec` : `${pick.c.label}'s extra roles`;
  const label = !pick ? "" : pick.kind === "rank" ? "Set the rank" : pick.kind === "spec" ? "Save the spec" : "Save the extra roles";
  const specs = pick && pick.kind !== "rank" ? Object.entries(meta.classes[pick.c.cls] || {}).map(([s, role]) => ({ value: s, label: `${s} · ${role}` })) : [];
  const choices = pick && pick.kind === "flex" ? flexChoices(meta, pick.c.cls, pick.c.spec, pick.c.offspec) : [];
  return (
    <Modal opened={pick !== null} onClose={onClose} title={title} centered>
      <Stack gap="md" className={css.touch}>
        {pick?.kind === "rank" && <Select label="Rank" data={rankChoices} value={rank} onChange={setRank} allowDeselect={false} />}
        {pick?.kind === "spec" && (
          <>
            <Select label="Spec" data={specs} value={spec} onChange={(v) => setSpec(v || spec)} allowDeselect={false} />
            <Select label="Offspec" placeholder="No offspec" data={specs} value={off} onChange={setOff} clearable />
          </>
        )}
        {pick?.kind === "flex" && <MultiSelect label="Could also play" placeholder="No extra roles" data={choices} value={flex.filter((x) => choices.includes(x))} onChange={setFlex} />}
        {b && said === null && <Loader size="xs" />}
        {said && "error" in said && <Text size="sm" c="dimmed">{said.error}</Text>}
        {said && "sentences" in said && <Stack gap={4}>{said.sentences.map((s, i) => <Text key={i} size="sm">{s}</Text>)}</Stack>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" size="sm" onClick={onClose}>Back</Button>
          <Button size="sm" loading={busy} disabled={!b || !said || "error" in said} onClick={go}>{label}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** One character waiting for an officer's confirm in the walk. */
export type WalkItem = { r: MemberRow; c: Character };

/** Confirm characters… (design §5.58, finding 12): one character at a time — Skip · Confirm · Done for now — as
 *  Discord's /roster confirm walks them. Each card asks POST /api/members/set {confirm, preview} for the server's
 *  sentence and each Confirm is one write with the rev that preview returned. Your own characters are left out of the
 *  queue, with the server's reason (`skipReason`, consequences.OWN_CONFIRM). */
export function ConfirmWalk({ meta, queue, skipped, skipReason, opened, onClose }: { meta: Meta; queue: WalkItem[]; skipped: string[]; skipReason?: string; opened: boolean; onClose: () => void }) {
  const [i, setI] = useState(0);
  const [done, setDone] = useState(0);
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (opened) { setI(0); setDone(0); } }, [opened]);
  const it = queue[i];
  useEffect(() => {
    setSaid(null);
    if (!opened || !it) return;
    let live = true;
    api.post<PeoplePreview>("/api/members/set", { uid: it.r.uid, character: it.c.label, confirm: true, preview: true })
      .then((p) => { if (live) setSaid({ sentences: p.sentences, rev: p.rev }); })
      .catch((e) => { if (live) setSaid({ error: errorOf(e) }); });
    return () => { live = false; };
  }, [opened, i, it?.c.label]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function confirm() {
    if (!it || !said || "error" in said) return;
    setBusy(true);
    try {
      await api.post<{ message: string }>("/api/members/set", { uid: it.r.uid, character: it.c.label, confirm: true, rev: said.rev });
      setDone((n) => n + 1); setI((n) => n + 1);
    } catch (e) { fail(e); } finally { setBusy(false); }
  }
  return (
    <Modal opened={opened} onClose={onClose} title="Confirm characters" centered>
      <Stack gap="md" className={css.touch}>
        {skipped.length > 0 && skipReason && <Text size="xs" c="dimmed">{skipReason} Left out: {skipped.join(", ")}.</Text>}
        {it ? (
          <>
            <Text size="xs" c="dimmed">{i + 1} of {queue.length} · {done} done</Text>
            <div className={css.charLine}>
              <GameIcon meta={meta} kind="class" id={it.c.cls} size={26} title={it.c.cls} />
              <Text fw={600} c={classColour(meta, it.c.cls)}>{it.c.label}</Text>
              <GameIcon meta={meta} kind="spec" id={`${it.c.cls}:${it.c.spec}`} size={22} title={`${it.c.spec} · ${it.c.role}`} />
              <Text size="xs" c="dimmed">{it.r.display_name}'s {it.c.is_main ? "main" : "alt"}</Text>
            </div>
            {said === null && <Loader size="xs" />}
            {said && "error" in said && <Text size="sm" c="dimmed">{said.error}</Text>}
            {said && "sentences" in said && <Stack gap={4}>{said.sentences.map((s, k) => <Text key={k} size="sm">{s}</Text>)}</Stack>}
            <Group justify="space-between" gap="sm" wrap="wrap">
              <Button variant="subtle" size="sm" onClick={onClose}>Done for now</Button>
              <Group gap="sm">
                <Button variant="default" size="sm" onClick={() => setI((n) => n + 1)}>Skip</Button>
                <Button size="sm" loading={busy} disabled={!said || "error" in said} onClick={confirm}>Confirm</Button>
              </Group>
            </Group>
          </>
        ) : (
          <>
            <Text size="sm">{done} of {queue.length} confirmed. Skipped ones still wait on the Members page.</Text>
            <Group justify="flex-end"><Button size="sm" onClick={onClose}>Close</Button></Group>
          </>
        )}
      </Stack>
    </Modal>
  );
}
