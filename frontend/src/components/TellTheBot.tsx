import { useEffect, useState, useSyncExternalStore } from "react";
import { Alert, Button, Group, Stack, Text, Textarea } from "@mantine/core";
import { IconMessageCircle } from "@tabler/icons-react";
import { api, ApiError, opsApi, type AskReply, type OpsReply, type TextPreview } from "../api";
import { DescribedView, ResultSheet } from "./ReviewModal";
import { fail, ok } from "./Page";

/** THE officers' "Tell the bot" box (design §3.4 OFF-20, "one box, two modes"): a question goes to POST /api/ask (the help
 *  answer, the asker's snapshot built by the server — the officer view for an officer); a request to act comes back as
 *  `{act: true}` and is previewed on POST /api/ops/change `{text}` — the same diff the Discord @mention shows (current →
 *  new, refused lines with who may and where) — then Apply N changes / Cancel and the result sheet. Nothing is written
 *  before Apply. On every OFFICER page's rail and on each SheetCard ("Tell the bot about this run"); NEVER on the Me page
 *  (owner decision D40 — tests/test_phase3_setup.py lints it).
 *
 *  The box's state lives in a store outside the card tree, keyed by guild + `scope` (the run key on a sheet), so the
 *  live reloads of the page around it (useLive at 7:55 PM) never blank a preview someone is reading. */

/** One exchange the box remembers (design §5.51): sent back with the next message so a follow-up ("him", "that run", an
 *  answer to the bot's question) is read with what it follows. Only here, while the page is open — no server memory. */
type Turn = { request: string; reply: string; questions?: string[] };
const TURNS = 4;
type BoxState = {
  text: string; busy: boolean; answer: string | null; note: string | null;
  preview: TextPreview | null; result: OpsReply | null; down: string | null; expiresAt: number | null; turns: Turn[];
};
const EMPTY: BoxState = { text: "", busy: false, answer: null, note: null, preview: null, result: null, down: null, expiresAt: null, turns: [] };
const boxes = new Map<string, BoxState>();
const listeners = new Set<() => void>();
function getBox(key: string): BoxState { return boxes.get(key) || EMPTY; }
function setBox(key: string, patch: Partial<BoxState>) { boxes.set(key, { ...getBox(key), ...patch }); listeners.forEach((l) => l()); }
function subscribe(l: () => void) { listeners.add(l); return () => { listeners.delete(l); }; }

export function TellTheBot({ scope = "rail", prefill = "", guild = "", placeholder = "Ask or tell the bot…", autoFocus }: { scope?: string; prefill?: string; guild?: string; placeholder?: string; autoFocus?: boolean }) {
  const key = `${guild}:${scope}`;
  const st = useSyncExternalStore(subscribe, () => getBox(key));
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { if (!boxes.has(key) && prefill) setBox(key, { text: prefill }); }, [key, prefill]);
  useEffect(() => {  // "preview good for N more min" stays honest while it waits
    if (!st.expiresAt) return;
    const id = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(id);
  }, [st.expiresAt]);
  const minsLeft = st.expiresAt ? Math.max(0, Math.ceil((st.expiresAt - now) / 60_000)) : null;

  function remember(turn: Turn) { setBox(key, { turns: [...getBox(key).turns, turn].slice(-TURNS) }); }

  async function previewChange(text: string, again = false, turns: Turn[] = getBox(key).turns) {
    const p = await api.post<TextPreview>("/api/ops/change", { text, conversation: turns });
    setBox(key, { preview: p, result: null, answer: null, note: again ? "Previewed again — check the lines." : null, expiresAt: p.expires_in ? Date.now() + p.expires_in * 1000 : null });
    if (!again) remember({ request: text, reply: [p.reply || "", ...(p.describe || [])].filter(Boolean).join("\n"), questions: p.questions || [] });
  }

  async function send() {
    const text = st.text.trim();
    if (!text || st.busy) return;
    setBox(key, { busy: true, answer: null, preview: null, result: null, note: null, down: null });
    try {
      const turns = getBox(key).turns;
      const r = await api.post<AskReply>("/api/ask", { question: text, conversation: turns });
      if (r.act) await previewChange(text, false, turns);
      else {
        const answer = r.answer ?? r.message ?? "No answer.";
        setBox(key, { answer });
        remember({ request: text, reply: answer });
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) setBox(key, { down: e.message });
      else fail(e);
    } finally { setBox(key, { busy: false }); }
  }

  async function apply() {
    const p = st.preview;
    if (!p?.token) return;
    setBox(key, { busy: true });
    try {
      const r = await opsApi.apply(p.token);
      setBox(key, { result: r, preview: null, expiresAt: null });
      const nOk = (r.applied || []).length, nBad = (r.refused || []).length;  // "3 saved, 1 refused" — never a bare "saved"
      const summary = r.summary || [nOk ? `${nOk} saved` : "", nBad ? `${nBad} refused` : ""].filter(Boolean).join(", ") || "nothing changed";
      (nBad ? fail(new Error(summary)) : ok(summary));
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {  // the preview expired (or the bot restarted): preview again once, never apply unseen
        try { await previewChange(st.text.trim(), true, getBox(key).turns.slice(0, -1)); } catch (e2) { fail(e2); }
      } else if (e instanceof ApiError && e.status === 409) {
        setBox(key, { note: /board/i.test(e.message) ? "The board changed since your preview — preview again." : e.message });
      } else fail(e);
    } finally { setBox(key, { busy: false }); }
  }

  const p = st.preview;
  const n = p?.described ? p.described.lines.filter((l) => !l.error).length : (p?.ops?.length || 0);
  return (
    <Stack gap="xs">
      <Textarea value={st.text} onChange={(e) => setBox(key, { text: e.currentTarget.value })} placeholder={placeholder} autosize minRows={2} maxRows={6} maxLength={1500}
        disabled={!!st.down} autoFocus={autoFocus} aria-label={placeholder}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); }} />
      {st.down && <Text size="xs" c="dimmed">{st.down}</Text>}
      <Group justify="space-between" gap="xs">
        <Text size="xs" c="dimmed">A question gets an answer; a request shows the change first.</Text>
        <Button size="xs" leftSection={<IconMessageCircle size={14} />} loading={st.busy && !p} disabled={!st.text.trim() || !!st.down} onClick={send}>Send</Button>
      </Group>
      {st.answer && <Alert variant="light" color="teal" p="xs"><Text size="sm" style={{ whiteSpace: "pre-line" }}>{st.answer}</Text></Alert>}
      {st.note && <Alert variant="light" color="blue" p="xs"><Text size="sm">{st.note}</Text></Alert>}
      {p && (
        <Stack gap="xs" p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
          {p.reply && <Text size="sm">{p.reply}</Text>}
          {(p.questions || []).map((q, i) => <Text key={i} size="sm" c="yellow">{q}</Text>)}
          {p.described ? <DescribedView d={p.described} /> : (p.describe || []).map((l, i) => <Text key={i} size="sm">• {l}</Text>)}
          {!p.described && (p.refused || []).map((r, i) => <Text key={`r${i}`} size="sm" c="red">⛔ {r}</Text>)}
          {p.token ? (
            <Group justify="space-between" gap="xs">
              <Text size="xs" c="dimmed">{minsLeft !== null ? `Preview good for ${minsLeft} more min · nothing is saved until Apply.` : "Nothing is saved until Apply."}</Text>
              <Group gap="xs">
                <Button size="xs" variant="default" onClick={() => setBox(key, { preview: null, expiresAt: null })} disabled={st.busy}>Cancel</Button>
                <Button size="xs" color={p.warn ? "yellow" : undefined} loading={st.busy} disabled={!n} onClick={apply}>Apply {n} change{n === 1 ? "" : "s"}</Button>
              </Group>
            </Group>
          ) : <Group justify="flex-end"><Button size="xs" variant="default" onClick={() => setBox(key, { preview: null })}>Close</Button></Group>}
        </Stack>
      )}
      {st.result && (
        <Stack gap="xs" p="xs" style={{ border: "1px solid var(--mantine-color-slate-5)", borderRadius: 8 }}>
          {st.result.results?.length ? <ResultSheet r={st.result} /> : (
            // the plain-text apply answers its lines (applied / refused) rather than per-write results
            <Stack gap={2}>
              {(st.result.applied || []).map((l, i) => <Text key={i} size="sm">✅ {l}</Text>)}
              {(st.result.refused || []).map((l, i) => <Text key={`r${i}`} size="sm" c="red">⛔ {l}</Text>)}
              {(st.result.applied || []).length > 0 && !(st.result.refused || []).length && <Text size="sm" c="dimmed">Nothing else was changed.</Text>}
            </Stack>
          )}
          <Group justify="flex-end"><Button size="xs" variant="default" onClick={() => setBox(key, { result: null, text: "" })}>Done</Button></Group>
        </Stack>
      )}
    </Stack>
  );
}
