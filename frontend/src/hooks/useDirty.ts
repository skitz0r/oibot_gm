import { useEffect } from "react";

/** Edit modes that hold an unsaved draft (design §3.5 S-01): the server selector asks "Save or discard your edits first"
 *  instead of reloading the page under the draft. EVERY edit mode calls `useDirty(key, editing)` — Settings, Channels,
 *  Raids rules, the schedule editor, comp targets, Auras, Recruitment, Plain permissions, the error alerts, the Members
 *  and Me character tables, the wishlist, tiers, policy, the test bench (tests/test_frontend_source.py lints it). */
const dirty = new Set<string>();

export function setDirty(key: string, on: boolean) {
  if (on) dirty.add(key); else dirty.delete(key);
}

/** Whether any edit mode on the page holds an unsaved draft. */
export function anyDirty(): boolean {
  return dirty.size > 0;
}

export function useDirty(key: string, on: boolean) {
  useEffect(() => {
    setDirty(key, on);
    return () => setDirty(key, false);
  }, [key, on]);
}
