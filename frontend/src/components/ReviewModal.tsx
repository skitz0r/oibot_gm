import { useCallback, useRef, useState } from "react";
import { Alert, Badge, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { ApiError, opsApi, type Box, type Described, type OpsChange, type OpsReply } from "../api";
import touch from "../pages/listpage.module.css";
import { fail, ok } from "./Page";
import { BoxBody, BoxButtons, boxWords } from "./ConfirmBox";

/** The ONE review step of the site's edit modes (design §2.3, D51 tiered review): the server's Described preview as structure —
 *  one block per write (a buff, a raid, a schedule…), its settings current → new, the refusal its apply would give, the
 *  consequence sentences — then Apply N changes / Back to edits. After Apply it becomes the RESULT SHEET: one ✅/⛔ line per
 *  write, "nothing else was changed". The browser composes none of these words: they come from configops.describe_ops.
 *
 *  Props: `preview` (the reply holding `described`), `result` (after apply: the result sheet), `busy`, `notice` (a line above
 *  the preview: "Updated since you looked" / "The bot restarted — review again"), `onApply`, `onClose` (Back to edits keeps the
 *  draft; after a result it closes). Most pages use `useOpsSave()` instead of driving it by hand. */
export interface ReviewModalProps {
  opened: boolean;
  title?: string;
  preview: OpsReply | null;
  result: OpsReply | null;
  busy?: boolean;
  notice?: string | null;
  onApply: () => void;
  onClose: () => void;
  /** the phase 4 pages: the server's box (described.box) is printed instead of the lines, with ITS verb */
  boxed?: boolean;
}

export function DescribedView({ d }: { d: Described }) {
  return (
    <Stack gap="sm">
      {d.lines.map((ln, i) => (
        <Stack key={i} gap={2} p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 6 }}>
          <Group gap="xs" justify="space-between" wrap="nowrap">
            <Text size="sm" fw={600}>{ln.label}</Text>
            {ln.warn && <Badge size="xs" variant="light" color="yellow">check this</Badge>}
          </Group>
          {ln.parts.map((p, j) => (
            <Text key={j} size="sm">
              {p.label ? <Text span c="dimmed">{p.label}: </Text> : null}
              {p.before !== null ? <><Text span td="line-through" c="dimmed">{p.before}</Text>{" → "}</> : null}
              {p.after !== null ? <Text span fw={600}>{p.after}</Text> : null}
            </Text>
          ))}
          {ln.notes.map((n, j) => <Text key={`n${j}`} size="xs" c="dimmed">{n}</Text>)}
          {ln.error && <Text size="xs" c="red">⛔ {ln.error}</Text>}
        </Stack>
      ))}
      {d.refused.map((r, i) => <Text key={`r${i}`} size="sm" c="red">⛔ {r}</Text>)}
      {d.consequences.length > 0 && (
        <Alert color="yellow" variant="light" p="xs">
          <Stack gap={4}>{d.consequences.map((c, i) => <Text key={i} size="sm">{c}</Text>)}</Stack>
        </Alert>
      )}
    </Stack>
  );
}

export function ResultSheet({ r }: { r: OpsReply }) {
  const rows = r.results || [];
  return (
    <Stack gap="xs">
      {rows.map((x, i) => <Text key={i} size="sm" c={x.ok ? undefined : "red"}>{x.ok ? "✅" : "⛔"} {x.label}: {x.line}</Text>)}
      {(r.refused || []).filter((x) => !rows.some((row) => !row.ok && x.endsWith(row.line))).map((x, i) => <Text key={`r${i}`} size="sm" c="red">⛔ {x}</Text>)}
      {r.note && <Text size="sm" c="dimmed">{r.note}</Text>}
      {!r.note && rows.length > 0 && rows.every((x) => x.ok) && <Text size="sm" c="dimmed">Nothing else was changed.</Text>}
    </Stack>
  );
}

export function ReviewModal({ opened, title, preview, result, busy, notice, onApply, onClose, boxed }: ReviewModalProps) {
  const d = preview?.described;
  const n = d ? d.lines.filter((l) => !l.error).length : 0;
  // the phase 4 pages (`boxed`): always a §7b box, never the lines / result sheet below — the server's box (its
  // title, diff and sentences, Back then the verb); a box the server refused with (verb null), a line that can't apply,
  // a refusal or a refused write is a box with the server's words and Back alone
  if (boxed) {
    const errors = result
      ? [...(result.refused || []), ...(result.results || []).filter((x) => !x.ok).map((x) => `${x.label}: ${x.line}`)]
      : [...(d?.lines || []).filter((l) => l.error).map((l) => `${l.label}: ${l.error}`), ...(d?.refused || [])];
    const said = !result && d?.box ? d.box : null;
    // no box from the server = a refusal (line errors, `refused`, a write refused after the press): it never carries the
    // question title ("Save Hyjal Summit?") — the server's refusal title stands above the server's words
    const box: Box = said || { title: boxWords().refused, sentences: errors.length ? [] : [(result || preview)?.summary || ""].filter(Boolean), verb: null, danger: false, diff: [], fp: "" };
    const go: Box = { ...box, verb: said?.verb && !errors.length && n && preview?.token ? said.verb : null };
    return (
      <Modal opened={opened} onClose={onClose} title={box.title} centered classNames={{ content: touch.touch }}>
        <Stack gap="md">
          {notice && !preview?.updated && <Alert color="blue" variant="light" p="xs"><Text size="sm">{notice}</Text></Alert>}
          {/* the server described the write again at the press and the box's words moved (an event opened meanwhile):
              the fresh box under the "changed" line, waiting for another press with its new token */}
          <BoxBody box={box} changed={!!preview?.updated} />
          {errors.map((x, i) => <Text key={i} size="sm" c="red">{x}</Text>)}
          <BoxButtons box={go} busy={busy} onBack={onClose} onGo={onApply} />
        </Stack>
      </Modal>
    );
  }
  return (
    <Modal opened={opened} onClose={onClose} title={result ? (result.summary ? `Saved — ${result.summary}` : "Result") : title || "Review your changes"} centered size="lg">
      <Stack gap="md">
        {result ? <ResultSheet r={result} /> : (
          <>
            {notice && <Alert color="blue" variant="light" p="xs"><Text size="sm">{notice}</Text></Alert>}
            {d ? <DescribedView d={d} /> : <Text size="sm" c="dimmed">Nothing to review.</Text>}
          </>
        )}
        <Group justify="flex-end" gap="sm">
          {result ? <Button size="sm" onClick={onClose}>Close</Button> : (
            <>
              <Button variant="default" size="sm" onClick={onClose} disabled={busy}>Back to edits</Button>
              <Button size="sm" color={d?.warn ? "yellow" : undefined} loading={busy} disabled={!n || !preview?.token} onClick={onApply}>
                Apply {n} change{n === 1 ? "" : "s"}
              </Button>
            </>
          )}
        </Group>
      </Stack>
    </Modal>
  );
}

type Waiter = (r: OpsReply | null) => void;

/** `const [save, review] = useOpsSave(); const r = await save(changes, { revs })` — render `review` once.
 *  A Save posts the typed changes; a routine edit applies at once (toast "2 saved"); a batch with a warn-class op or a consequence
 *  line opens the ReviewModal and resolves after Apply (with the result) or Back to edits (null — the draft stays). A refusal opens
 *  the result sheet. A token that went stale re-previews: applied silently when the lines are the ones shown, otherwise shown
 *  again ("Updated since you looked"); a 400 for the token (the bot restarted) says so and shows the fresh preview.
 *  A 409 (someone else changed the section since the page loaded) resolves with `{ ...body, changed }` for the page to mark. */
/** `box` (the phase 4 pages): the server's box (described.box) is printed — its title, sentences and verb — with Back in
 *  place of the lines, Apply N changes and Back to edits; a write that went through closes the box with a toast (no
 *  result sheet). `always`: the box shows even where the server would apply at once (a Save is a preview first).
 *  `onShown`: called once the server answered the Save, before any box opens (the page's Save spinner stops there:
 *  it never spins behind an open box). */
export type OpsSaveOpts = { revs?: Record<string, string>; title?: string; box?: boolean; always?: boolean; onShown?: () => void;
  /** the page says a 409 itself (the schedule editor's alert beside its Save): no toast on top of it */
  staleOnPage?: boolean };

export function useOpsSave(): [(changes: OpsChange[], opts?: OpsSaveOpts) => Promise<OpsReply | null>, React.ReactNode] {
  const [preview, setPreview] = useState<OpsReply | null>(null);
  const [result, setResult] = useState<OpsReply | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState<string | undefined>();
  const [boxed, setBoxed] = useState(false);
  const ctx = useRef<{ changes: OpsChange[]; revs?: Record<string, string>; done: Waiter; boxed?: boolean; staleOnPage?: boolean } | null>(null);

  const close = () => {
    const c = ctx.current;
    const r = result;
    ctx.current = null;
    setPreview(null); setResult(null); setNotice(null);
    c?.done(r);
  };

  const showResult = (r: OpsReply) => {
    setPreview(null); setResult(r);
    if (r.summary) (r.refused?.length ? fail(new Error(r.summary)) : ok(r.summary));
  };

  const apply = async () => {
    const c = ctx.current;
    if (!c || !preview?.token) return;
    setBusy(true);
    try {
      const r = await opsApi.apply(preview.token, c.changes, preview.fingerprint, c.revs);
      if (r.needs_review) {  // the token went stale and the lines moved (or the bot restarted): look again
        setNotice(r.restarted ? "The bot restarted — review again." : "Updated since you looked — check the changes again.");
        setPreview(r);
      } else if (c.boxed && !r.refused?.length) {  // the box's write went through: close it, say so
        ctx.current = null;
        setPreview(null); setResult(null); setNotice(null);
        ok(r.summary || "saved");
        c.done(r);
      } else showResult(r);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400 && /apply needs the token/.test(e.message)) {
        try { setPreview(await opsApi.preview(c.changes, c.revs)); setNotice("The bot restarted — review again."); } catch (e2) { fail(e2); }
      } else if (e instanceof ApiError && e.status === 409) {
        const body = e.body as OpsReply;
        ctx.current = null; setPreview(null); setResult(null); setNotice(null);
        if (!c.staleOnPage) fail(e);
        c.done({ ...body, changed: body.changed || [] });
      } else fail(e);
    } finally { setBusy(false); }
  };

  const save = useCallback(async (changes: OpsChange[], opts?: OpsSaveOpts): Promise<OpsReply | null> => {
    if (!changes.length) { ok("nothing changed"); return null; }
    let r: OpsReply;
    try {
      r = await (opts?.always ? opsApi.preview(changes, opts?.revs) : opsApi.save(changes, opts?.revs));
    } catch (e) {
      opts?.onShown?.();
      if (e instanceof ApiError && e.status === 409) {
        if (!opts?.staleOnPage) fail(e);
        return { ...(e.body as OpsReply), changed: (e.body as OpsReply).changed || [] };
      }
      throw e;
    }
    opts?.onShown?.();
    // nothing differs from what is stored and the server has nothing to say about it: a toast; a box it answered with
    // (a refusal like "Nothing to save") or a refusal is shown as a box
    const said = !!r.described?.box || !!r.refused?.length || !!r.described?.lines.some((l) => l.error);
    if (opts?.always && !r.token && !said) {
      ok(r.summary || "nothing changed");
      return null;
    }
    const review = r.needs_review || (opts?.always && (!!r.token || said));
    if (!review && !(r.refused?.length)) {
      ok(r.summary || "saved");
      return r;
    }
    return new Promise<OpsReply | null>((resolve) => {
      ctx.current = { changes, revs: opts?.revs, done: resolve, boxed: opts?.box, staleOnPage: opts?.staleOnPage };
      setTitle(opts?.title); setBoxed(!!opts?.box);
      if (review) { setResult(null); setPreview(r); } else showResult(r);
    });
  }, []);

  const dialog = <ReviewModal opened={preview !== null || result !== null} title={title} preview={preview} result={result} busy={busy} notice={notice} onApply={apply} onClose={close} boxed={boxed} />;
  return [save, dialog];
}
