import { useEffect, useRef } from "react";

/** Re-run `load` every `ms` while the tab is visible, and once more when it becomes visible again. `load` may change between renders. */
export const REFRESH_EVENT = "oibot:refresh";

/** Reload when the rail's Refresh is pressed (the one manual escape hatch; pages otherwise update live). */
export function useRefresh(load: () => unknown) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  useEffect(() => {
    const on = () => { ref.current(); };
    window.addEventListener(REFRESH_EVENT, on);
    return () => window.removeEventListener(REFRESH_EVENT, on);
  }, []);
}

export function usePoll(load: () => unknown, ms = 45_000) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  useRefresh(load);
  useEffect(() => {
    const tick = () => { if (document.visibilityState === "visible") ref.current(); };
    const id = setInterval(tick, ms);
    const onVis = () => { if (document.visibilityState === "visible") ref.current(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVis); };
  }, [ms]);
}
