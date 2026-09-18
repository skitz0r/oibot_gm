import { useEffect, useRef } from "react";
import { live } from "../components/board/shared";

/** Reload when the server says a run changed (server-sent events on /api/events). Debounced, and held back while a
 *  drag is in progress so the board never re-renders under the cursor. The 45 s poll stays as the fallback. */
export function useLive(load: () => unknown) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const fire = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (live.dragging) fire(); else ref.current(); }, 400);
    };
    const es = new EventSource("/api/events", { withCredentials: true });
    es.onmessage = fire;
    return () => { clearTimeout(timer); es.close(); };
  }, []);
}
