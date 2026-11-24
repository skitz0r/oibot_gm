import { useEffect } from "react";

/** Edit modes that hold an unsaved draft (design §3.5 S-01): the server selector asks "Save or discard your edits first"
 *  instead of reloading the page under the draft, and the leave guard (components/LeaveGuard) asks "Leave without
 *  saving?" before a rail link or a list pick drops it. EVERY edit mode calls `useDirty(key, editing, what?)` — Settings,
 *  Channels, the Raids page, the schedule editor, Auras, Recruitment, Plain permissions, the error alerts, the Members
 *  and Me character tables, the wishlist, tiers, policy, the test bench (tests/test_frontend_source.py lints it).
 *  `what` = how many edits and to what ({n: 2, name: "Hyjal Summit"}): the leave guard's sentence. */
export type DirtyWhat = { n: number; name: string };
const dirty = new Map<string, DirtyWhat | null>();

export function setDirty(key: string, on: boolean, what?: DirtyWhat | null) {
  if (on && !(what && what.n <= 0)) dirty.set(key, what || null); else dirty.delete(key);  // an edit mode with no edit yet holds nothing to lose
}

/** Whether any edit mode on the page holds an unsaved draft. */
export function anyDirty(): boolean {
  return dirty.size > 0;
}

/** What the open drafts hold, for the leave guard's sentence: the edits added up, named by the first draft that says
 *  what it edits; null when no draft names itself. */
export function dirtyWhat(): DirtyWhat | null {
  const named = [...dirty.values()].filter((w): w is DirtyWhat => w !== null);
  if (!named.length) return null;
  return { n: named.reduce((a, w) => a + w.n, 0), name: named[0].name };
}

/** Forget every draft (the leave guard's Discard edits, just before it navigates away and the edit modes unmount). */
export function clearDirty() {
  dirty.clear();
}

export function useDirty(key: string, on: boolean, what?: DirtyWhat) {
  const n = what?.n ?? 0;
  const name = what?.name ?? "";
  useEffect(() => {
    setDirty(key, on, what ? { n, name } : null);
    return () => setDirty(key, false);
  }, [key, on, n, name]);  // eslint-disable-line react-hooks/exhaustive-deps
}
