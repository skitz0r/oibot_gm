import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Button, Checkbox, Group, Stack } from "@mantine/core";
import { api, type Board, type Box, type Meta } from "../../api";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { BoardPreview } from "../board/BoardPreview";
import type { LockPreview } from "../board/types";
import { boxOf } from "./shared";

/** The box on screen, for the fields inside it: every box the dialog shows passes through `set` (the first one, a
 *  change of the choice, a live refresh, a 409's fresh box), so the fields never act on a stale copy. */
type BoxStore = { get: () => Box; set: (b: Box) => void; sub: (f: () => void) => () => void };
function boxStore(first: Box): BoxStore {
  let cur = first;
  const subs = new Set<() => void>();
  return { get: () => cur, set: (b) => { cur = b; subs.forEach((f) => f()); }, sub: (f) => { subs.add(f); return () => { subs.delete(f); }; } };
}

/** The box's own fields: the server's one choice (`box.choice` — Auto-fill empty roster slots, unticked) as a checkbox
 *  between the diff and the sentences, and Preview lock with the roster the CURRENT choice would build under it. A change
 *  of the choice asks the server's box again (`onFill` → replace; only the latest answer lands) and redraws a preview
 *  already shown. */
function LockFields({ url, meta, store, startPreview, onFill }: { url: string; meta: Meta; store: BoxStore; startPreview: boolean; onFill: (fill: boolean) => Promise<Box | null> }) {
  const box = useSyncExternalStore(store.sub, store.get);
  const fill = !!box.choice?.value;
  const [board, setBoard] = useState<Board | null>(null);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  async function preview(f: boolean) {
    const n = ++seq.current;
    setBusy(true);
    try {
      const p = await api.post<LockPreview>(`${url}/preview`, { fill: f });
      if (n === seq.current) setBoard(p.board);
    } catch (e) { if (n === seq.current) setBoard(null); fail(e); } finally { if (n === seq.current) setBusy(false); }
  }
  useEffect(() => { if (startPreview && box.verb) preview(fill); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  // a box with nothing to press (a refusal, an event that locked meanwhile) has no roster to preview
  useEffect(() => { if (!box.verb) setBoard(null); }, [box.verb]);

  async function pick(f: boolean) {
    const n = ++seq.current;
    try {
      const b = await onFill(f);
      if (!b || n !== seq.current) return;
      if (board && b.verb) preview(f); else setBoard(null);
    } catch (e) { fail(e); }
  }

  return (
    <Stack gap="sm">
      {box.choice && <Checkbox label={box.choice.label} checked={fill} onChange={(e) => pick(e.currentTarget.checked)} />}
      {box.verb && <Group><Button variant="light" size="xs" loading={busy} onClick={() => preview(fill)}>{meta.run_labels.preview_lock}</Button></Group>}
      {board && <BoardPreview meta={meta} board={board} />}
    </Stack>
  );
}

/** Lock (boxes 1–2, issue 14): ONE box for the Events page's Lock now and the sheet card's Lock / Preview lock — the
 *  SERVER's box (POST /api/run/{key}/lock `preview`, consequences.lock_box) with its choice inside it (RescheduleBox's
 *  fields + replace). Unticked, the lock rosters the board as it stands; ticking it asks the box again for `fill: true`
 *  (two quick toggles: only the last answer lands). A refusal (nobody on the board) is the box with Back alone, the
 *  choice still there to tick. Lock sends the choice of the box on screen with its fp as `seen`: a 409 brings the
 *  fresh box under the "changed" line. */
export function useLockBox(meta: Meta, onDone: () => void): [(key: string, opts?: { box?: Box | null; preview?: boolean }) => void, React.ReactNode] {
  const [ask, dialog, replace] = useConfirmBox(["run:", "member"]);

  async function open(key: string, opts: { box?: Box | null; preview?: boolean } = {}) {
    const url = `/api/run/${encodeURIComponent(key)}/lock`;
    const get = async (fill: boolean): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { preview: true, fill })).box; } catch (x) { const b = boxOf(x); if (b) return b; throw x; }
    };
    const first = opts.box && !opts.box.choice?.value ? opts.box : await get(false);
    const store = boxStore(first);
    const fillOf = () => !!store.get().choice?.value;
    let seq = 0;
    let said = "";
    const done = await ask({
      box: first, size: "lg",
      fields: <LockFields url={url} meta={meta} store={store} startPreview={!!opts.preview}
        onFill={async (f) => { const n = ++seq; const b = await get(f); if (n !== seq) return null; store.set(b); replace(b); return b; }} />,
      refresh: async () => { const n = seq; const b = await get(fillOf()); if (n !== seq) return store.get(); store.set(b); return b; },
      act: async (fp) => {
        try { said = (await api.post<{ message: string }>(url, { seen: fp, fill: fillOf() })).message; return null; }
        catch (x) { const b = boxOf(x); if (b) { store.set(b); return b; } throw x; }
      },
    });
    if (done) { ok(said); onDone(); }
  }
  return [(key, opts) => { open(key, opts).catch(fail); }, dialog];
}
