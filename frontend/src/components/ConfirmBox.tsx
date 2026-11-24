import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Alert, Box as MBox, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { guildQuery, type Box, type Meta } from "../api";
import { fail } from "./Page";
import touch from "../pages/listpage.module.css";

/** The confirmation box (design §7b) as the site prints it: the SERVER's box (consequences.box) and nothing composed
 *  here — its title, the "label: old → new" lines, any fields the page adds, then its sentences; two buttons, Back then
 *  the box's own verb, red only when the box says it cannot be undone. A refusal (`verb: null`) has only Back.
 *  The fixed words (Back, the "changed" line) are /api/meta.confirm_words, handed over once by App (setBoxMeta). */
let meta: Meta | null = null;
export function setBoxMeta(m: Meta) { meta = m; }
/** the fixed words, read when a box draws (App sets them before any page renders) */
export function boxWords(): Meta["confirm_words"] { return meta!.confirm_words; }

/** A box's body: the "changed" line when it was swapped while open, the title is the Modal's; the diff lines, the page's
 *  fields, the sentences. Shared with useOpsSave's review (a raid's or a schedule's Save prints `described.box`). */
export function BoxBody({ box, fields, changed }: { box: Box; fields?: React.ReactNode; changed?: boolean }) {
  return (
    <Stack gap="sm">
      {changed && <Alert color="yellow" variant="light" p="xs"><Text size="sm">{meta!.confirm_words.changed}</Text></Alert>}
      {box.diff.length > 0 && (
        <MBox p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8, background: "var(--mantine-color-slate-8)" }}>
          {box.diff.map((d, i) => <Text key={i} size="sm">{d}</Text>)}
        </MBox>
      )}
      {fields}
      {box.sentences.map((s, i) => <Text key={i} size="sm">{s}</Text>)}
    </Stack>
  );
}

/** Back then the verb (red iff `danger`); a refusal = Back alone. `back` replaces Back where the box names its own
 *  (the leave guard's Keep editing). */
export function BoxButtons({ box, busy, back, onBack, onGo }: { box: Box; busy?: boolean; back?: string; onBack: () => void; onGo: () => void }) {
  return (
    <Group justify="flex-end" gap="sm">
      <Button variant="default" size="sm" onClick={onBack} disabled={busy}>{back || meta!.confirm_words.back}</Button>
      {box.verb && <Button size="sm" color={box.danger ? "red" : undefined} loading={busy} onClick={onGo}>{box.verb}</Button>}
    </Group>
  );
}

export type AskBox = {
  box: Box;
  /** the page's own inputs inside the box (Reschedule's day and time), drawn between the diff and the sentences */
  fields?: React.ReactNode;
  /** the write, with the fingerprint of the box pressed (sent as `seen`): null when done, or the fresh box (a 409's
   *  body) — the box swaps to it under the "changed" line and waits again */
  act?: (fp: string) => Promise<Box | null>;
  /** asked again when one of the hook's live topics fires while the box is open: a different `fp` swaps the box */
  refresh?: () => Promise<Box>;
  /** a label in place of Back (the leave guard's Keep editing) */
  back?: string;
  /** a wider box, so a row of fields stays one row (Reschedule's New day · Starts · Locks, as the mockup) */
  wide?: boolean;
};
type Open = AskBox & { resolve: (done: boolean) => void };

/** `const [ask, dialog] = useConfirmBox(["config"]); if (await ask({ box, act })) …` — render `dialog` once. Resolves
 *  true when the verb was pressed and `act` finished (or there is no `act`), false on Back. `topics` = the live topics
 *  (/api/events prefixes) that make an open box ask `refresh` again; the stream is opened only while a box is open.
 *  The third value, `replace(box)`, swaps the open box for the server's answer to the box's OWN fields (Reschedule's
 *  picker, Answer for someone's member) — no "changed" line: the officer changed it, not someone else. */
/** How many boxes are open on the page now: a view that would unmount a box's owner (the Events page turning an event
 *  that ended underneath into its history) waits while one is open (useAnyBoxOpen). */
let openBoxes = 0;
const boxWatchers = new Set<() => void>();
const boxCount = (d: number) => { openBoxes = Math.max(0, openBoxes + d); boxWatchers.forEach((f) => f()); };
export function useAnyBoxOpen(): boolean {
  return useSyncExternalStore((f) => { boxWatchers.add(f); return () => { boxWatchers.delete(f); }; }, () => openBoxes > 0);
}

export function useConfirmBox(topics: string[] = []): [(o: AskBox) => Promise<boolean>, React.ReactNode, (b: Box) => void] {
  const [open, setOpen] = useState<Open | null>(null);
  const isOpen = open !== null;
  useEffect(() => { if (!isOpen) return; boxCount(1); return () => boxCount(-1); }, [isOpen]);
  const [box, setBox] = useState<Box | null>(null);
  const [changed, setChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const current = useRef<Box | null>(null);
  useEffect(() => { current.current = box; }, [box]);

  const ask = useCallback((o: AskBox) => new Promise<boolean>((resolve) => {
    setBox(o.box); setChanged(false); setBusy(false); setOpen({ ...o, resolve });
  }), []);

  const replace = useCallback((fresh: Box) => { current.current = fresh; setBox(fresh); setChanged(false); }, []);

  const swap = (fresh: Box) => {
    if (current.current && fresh.fp === current.current.fp) return;
    setBox(fresh); setChanged(true);
  };

  // the box's facts changed while it was open (someone joined, a setting moved): the fresh box, said so, waits again
  const want = topics.join("|");
  const refresh = open?.refresh;
  useEffect(() => {
    if (!open || !refresh || !want || typeof EventSource === "undefined") return;
    const prefixes = want.split("|");
    let timer: ReturnType<typeof setTimeout> | undefined;
    const es = new EventSource("/api/events" + guildQuery(), { withCredentials: true });
    es.onmessage = (m) => {
      if (!prefixes.some((p) => String(m.data).startsWith(p))) return;
      clearTimeout(timer);
      timer = setTimeout(() => { refresh().then(swap).catch(() => undefined); }, 400);
    };
    return () => { clearTimeout(timer); es.close(); };
  }, [open, refresh, want]);  // eslint-disable-line react-hooks/exhaustive-deps

  const close = (done: boolean) => { open?.resolve(done); setOpen(null); setBox(null); setChanged(false); };

  const go = async () => {
    if (!open || !box) return;
    if (!open.act) { close(true); return; }
    setBusy(true);
    try {
      const fresh = await open.act(box.fp);
      // a 409's fresh box (it has a verb: the action still stands, its facts moved) says it changed; a refusal (verb
      // null — the 400's box) is the verb's own sentence, never "This changed while the box was open"
      if (fresh) { current.current = fresh; setBox(fresh); setChanged(fresh.verb !== null); } else close(true);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const dialog = (
    <Modal opened={open !== null && box !== null} onClose={() => { if (!busy) close(false); }} title={box?.title} centered size={open?.wide ? 600 : "md"} classNames={{ content: touch.touch }}>
      {box && (
        <Stack gap="md">
          <BoxBody box={box} fields={open?.fields} changed={changed} />
          <BoxButtons box={box} busy={busy} back={open?.back} onBack={() => close(false)} onGo={go} />
        </Stack>
      )}
    </Modal>
  );
  return [ask, dialog, replace];
}
