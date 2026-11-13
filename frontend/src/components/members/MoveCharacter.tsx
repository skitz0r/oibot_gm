import { useEffect, useState } from "react";
import { Button, Group, Loader, Modal, Select, Stack, Text } from "@mantine/core";
import { api, ApiError, type Character, type MemberRow } from "../../api";
import { fail, ok } from "../Page";
import css from "../../pages/members.module.css";

/** Which character moves, from whom. */
export type MoveTarget = { r: MemberRow; c: Character };

type Said = { to: string; sentences: string[] } | { to: string; error: string } | null;

/** Move to another member… (spec §5.4, officers, every game version — R11): pick the member from the page's list (never a
 *  typed id) → the server's preview under the pick (consequences.move_consequence, or its one-sentence refusal) → **Move
 *  it** writes. The page composes no consequence: what follows the character and who is told are the server's words. */
export function MoveCharacter({ move, rows, onClose, onDone }: { move: MoveTarget | null; rows: MemberRow[]; onClose: () => void; onDone: () => void }) {
  const [to, setTo] = useState<string | null>(null);
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setTo(null); setSaid(null); }, [move]);
  useEffect(() => {
    if (!move || !to) { setSaid(null); return; }
    let live = true;  // an answer for an earlier pick is dropped
    setSaid(null);
    api.post<{ sentences: string[] }>("/api/members/move", { uid: move.r.uid, label: move.c.label, to, preview: true })
      .then((p) => { if (live) setSaid({ to, sentences: p.sentences }); })
      .catch((e) => { if (live) setSaid({ to, error: e instanceof ApiError || e instanceof Error ? e.message : String(e) }); });
    return () => { live = false; };
  }, [to, move]);

  async function go() {
    if (!move || !to || !said || "error" in said || said.to !== to) return;
    setBusy(true);
    try {
      const x = await api.post<{ message: string }>("/api/members/move", { uid: move.r.uid, label: move.c.label, to });
      ok(x.message); onClose();
    } catch (e) { fail(e); } finally { setBusy(false); onDone(); }
  }

  // a puppet moves only to a puppet, a real member only to a real one; nobody who left; never to the same member
  const choices = move ? rows.filter((r) => r.uid !== move.r.uid && !r.left && !!r.test === !!move.r.test)
    .map((r) => ({ value: r.uid, label: r.display_name })) : [];
  return (
    <Modal opened={move !== null} onClose={onClose} title={move ? `Move ${move.c.label} to another member` : ""} centered>
      <Stack gap="md" className={css.touch}>
        <Select label="The member it moves to" placeholder="Pick a member" searchable data={choices} value={to} onChange={setTo}
          nothingFoundMessage="Nobody by that name has a record here. They register with /register first." />
        {to && said === null && <Loader size="xs" />}
        {said && "sentences" in said && <Text size="sm" style={{ whiteSpace: "pre-line" }}>{said.sentences.join("\n")}</Text>}
        {said && "error" in said && <Text size="sm" c="red">{said.error}</Text>}
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!said || "error" in said} onClick={go}>Move it</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
