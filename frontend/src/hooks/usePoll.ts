import { useEffect, useRef } from "react";

/** Re-run `load` every `ms` while the tab is visible, and once more when it becomes visible again. `load` may change between renders. */
export function usePoll(load: () => unknown, ms = 45_000) {
  const ref = useRef(load);
  useEffect(() => { ref.current = load; });
  useEffect(() => {
    const tick = () => { if (document.visibilityState === "visible") ref.current(); };
    const id = setInterval(tick, ms);
    const onVis = () => { if (document.visibilityState === "visible") ref.current(); };
    document.addEventListener("visibilitychange", onVis);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVis); };
  }, [ms]);
}
