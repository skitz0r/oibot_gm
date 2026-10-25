import { useCallback, useRef, useState } from "react";
import { Alert, Badge, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { ApiError, opsApi, type Described, type OpsChange, type OpsReply } from "../api";
import { fail, ok } from "./Page";

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

export function ReviewModal({ opened, title, preview, result, busy, notice, onApply, onClose }: ReviewModalProps) {
  const d = preview?.described;
  const n = d ? d.lines.filter((l) => !l.error).length : 0;
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
export function useOpsSave(): [(changes: OpsChange[], opts?: { revs?: Record<string, string>; title?: string }) => Promise<OpsReply | null>, React.ReactNode] {
  const [preview, setPreview] = useState<OpsReply | null>(null);
  const [result, setResult] = useState<OpsReply | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState<string | undefined>();
  const ctx = useRef<{ changes: OpsChange[]; revs?: Record<string, string>; done: Waiter } | null>(null);

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
      } else showResult(r);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400 && /apply needs the token/.test(e.message)) {
        try { setPreview(await opsApi.preview(c.changes, c.revs)); setNotice("The bot restarted — review again."); } catch (e2) { fail(e2); }
      } else if (e instanceof ApiError && e.status === 409) {
        const body = e.body as OpsReply;
        ctx.current = null; setPreview(null); setResult(null); setNotice(null);
        fail(e); c.done({ ...body, changed: body.changed || [] });
      } else fail(e);
    } finally { setBusy(false); }
  };

  const save = useCallback(async (changes: OpsChange[], opts?: { revs?: Record<string, string>; title?: string }): Promise<OpsReply | null> => {
    if (!changes.length) { ok("nothing changed"); return null; }
    let r: OpsReply;
    try {
      r = await opsApi.save(changes, opts?.revs);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { fail(e); return { ...(e.body as OpsReply), changed: (e.body as OpsReply).changed || [] }; }
      throw e;
    }
    if (!r.needs_review && !(r.refused?.length)) {
      ok(r.summary || "saved");
      return r;
    }
    return new Promise<OpsReply | null>((resolve) => {
      ctx.current = { changes, revs: opts?.revs, done: resolve };
      setTitle(opts?.title);
      if (r.needs_review) { setResult(null); setPreview(r); } else showResult(r);
    });
  }, []);

  const dialog = <ReviewModal opened={preview !== null || result !== null} title={title} preview={preview} result={result} busy={busy} notice={notice} onApply={apply} onClose={close} />;
  return [save, dialog];
}
