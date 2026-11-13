import { useEffect, useRef } from "react";
import { live } from "../components/board/shared";
import { guildQuery } from "../api";

/** Reload when the server says something changed (server-sent events on /api/events: `run:<key>`, `member`, `config`, `ops`)
 *  in the guild this tab shows.
 *  `topics` = prefixes this page cares about (default: all). Debounced, and held back while a drag is in progress so the
 *  board never re-renders under the cursor. `every` (ms) = at most one reload per that long, the last event still heard
 *  (a card that refetches something heavy on every `member` event). The 45 s poll stays as the fallback. */
export function useLive(load: () => unknown, topics?: string[], opts?: { every?: number }) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  const want = (topics || []).join("|");
  const every = opts?.every || 0;
  const g = guildQuery();  // the stream is this guild's only (EventSource can't send the header, so ?g=)
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const prefixes = want ? want.split("|") : [];
    let timer: ReturnType<typeof setTimeout> | undefined;
    let last = 0;
    const fire = () => {
      clearTimeout(timer);
      const wait = Math.max(400, last + every - Date.now());
      timer = setTimeout(() => { if (live.dragging) fire(); else { last = Date.now(); ref.current(); } }, wait);
    };
    const es = new EventSource("/api/events" + g, { withCredentials: true });
    es.onmessage = (m) => { if (!prefixes.length || prefixes.some((p) => String(m.data).startsWith(p))) fire(); };
    return () => { clearTimeout(timer); es.close(); };
  }, [want, g, every]);
}

/** A card's live reload that waits while it is `held` (a dialog of that card open, a press in progress): the event is
 *  remembered and the card reloads once when it is free again. At most one reload every `every` ms. */
export function useHeldLive(load: () => unknown, topics: string[], held: boolean, every = 3000) {
  const pending = useRef(false);
  useLive(() => { if (held) pending.current = true; else load(); }, topics, { every });
  useEffect(() => { if (!held && pending.current) { pending.current = false; load(); } }, [held]);  // eslint-disable-line react-hooks/exhaustive-deps
}
