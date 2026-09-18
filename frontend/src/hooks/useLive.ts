import { useEffect, useRef } from "react";
import { live } from "../components/board/shared";

/** Reload when the server says something changed (server-sent events on /api/events: `run:<key>`, `member`, `config`, `ops`).
 *  `topics` = prefixes this page cares about (default: all). Debounced, and held back while a drag is in progress so the
 *  board never re-renders under the cursor. The 45 s poll stays as the fallback. */
export function useLive(load: () => unknown, topics?: string[]) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  const want = (topics || []).join("|");
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const prefixes = want ? want.split("|") : [];
    let timer: ReturnType<typeof setTimeout> | undefined;
    const fire = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (live.dragging) fire(); else ref.current(); }, 400);
    };
    const es = new EventSource("/api/events", { withCredentials: true });
    es.onmessage = (m) => { if (!prefixes.length || prefixes.some((p) => String(m.data).startsWith(p))) fire(); };
    return () => { clearTimeout(timer); es.close(); };
  }, [want]);
}
