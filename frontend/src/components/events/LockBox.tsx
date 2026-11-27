import { useEffect, useRef, useState } from "react";
import { Button, Checkbox, Group, Stack } from "@mantine/core";
import { api, type Board, type Box, type Meta } from "../../api";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { BoardPreview } from "../board/BoardPreview";
import type { LockPreview } from "../board/types";
import { boxOf } from "./shared";

/** The box's own fields: the server's one choice (`box.choice` — Auto-fill empty roster slots, unticked) as a checkbox
 *  between the diff and the sentences, and Preview lock with the roster the CURRENT choice would build under it. A change
 *  of the choice asks the server's box again (`onFill` → replace) and redraws a preview already shown. */
function LockFields({ url, meta, first, startPreview, onFill }: { url: string; meta: Meta; first: Box; startPreview: boolean; onFill: (fill: boolean) => Promise<Box> }) {
  const [fill, setFill] = useState(!!first.choice?.value);
  const [box, setBox] = useState(first);
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
  useEffect(() => { if (startPreview && first.verb) preview(fill); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  async function pick(f: boolean) {
    setFill(f);
    const n = ++seq.current;
    try {
      const b = await onFill(f);
      if (n !== seq.current) return;
      setBox(b);
      if (board && b.verb) preview(f); else setBoard(null);
    } catch (e) { fail(e); }
  }

  return (
    <Stack gap="sm">
      {first.choice && <Checkbox label={first.choice.label} checked={fill} onChange={(e) => pick(e.currentTarget.checked)} />}
      {box.verb && <Group><Button variant="light" size="xs" loading={busy} onClick={() => preview(fill)}>{meta.run_labels.preview_lock}</Button></Group>}
      {board && <BoardPreview meta={meta} board={board} />}
    </Stack>
  );
}

/** Lock (boxes 1–2, issue 14): ONE box for the Events page's Lock now and the sheet card's Lock / Preview lock — the
 *  SERVER's box (POST /api/run/{key}/lock `preview`, consequences.lock_box) with its choice inside it (RescheduleBox's
 *  fields + replace). Unticked, the lock rosters the board as it stands; ticking it asks the box again for `fill: true`.
 *  A refusal (nobody on the board) is the box with Back alone, the choice still there to tick. Lock sends the choice
 *  with the fp of the box on screen as `seen`: a 409 brings the fresh box under the "changed" line. */
export function useLockBox(meta: Meta, onDone: () => void): [(key: string, opts?: { box?: Box | null; preview?: boolean }) => void, React.ReactNode] {
  const [ask, dialog, replace] = useConfirmBox(["run:", "member"]);

  async function open(key: string, opts: { box?: Box | null; preview?: boolean } = {}) {
    const url = `/api/run/${encodeURIComponent(key)}/lock`;
    const now = { fill: false };
    const get = async (): Promise<Box> => {
      try { return (await api.post<{ box: Box }>(url, { preview: true, fill: now.fill })).box; } catch (x) { const b = boxOf(x); if (b) return b; throw x; }
    };
    const first = opts.box && !opts.box.choice?.value ? opts.box : await get();
    let said = "";
    const done = await ask({
      box: first, size: "lg",
      fields: <LockFields url={url} meta={meta} first={first} startPreview={!!opts.preview}
        onFill={async (f) => { now.fill = f; const b = await get(); replace(b); return b; }} />,
      refresh: get,
      act: async (fp) => {
        try { said = (await api.post<{ message: string }>(url, { seen: fp, fill: now.fill })).message; return null; }
        catch (x) { const b = boxOf(x); if (b) return b; throw x; }
      },
    });
    if (done) { ok(said); onDone(); }
  }
  return [(key, opts) => { open(key, opts).catch(fail); }, dialog];
}
