import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import type { Box, Meta } from "../api";
import { anyDirty, clearDirty, dirtyWhat } from "../hooks/useDirty";
import { useConfirmBox } from "./ConfirmBox";

/** The site's navigation under an unsaved draft (design §7b box 20): `guardNav(to)` goes at once when no edit mode holds
 *  a draft, else asks "Leave without saving?" with the server's sentence (consequences.LEAVE_TEMPLATE through
 *  /api/meta.leave: "Your 2 edits to Hyjal Summit are not saved yet."), Keep editing / Discard edits (red). The rail's
 *  links and every ListPage pick call it. Closing or reloading the tab while a draft is open asks the browser's own
 *  question (beforeunload). Rendered once, in App. */
let go: ((to: string) => void) | null = null;

export function guardNav(to: string) {
  if (go) go(to);
  else window.location.assign(`/app${to}`);
}

/** The leave box from the meta's words (the server's template, filled here as consequences.leave_unsaved_sentence fills it). */
function leaveBox(meta: Meta): Box {
  const w = dirtyWhat();
  const sentence = !w ? null : w.n === 1 ? meta.leave.one.replace("{name}", w.name) : meta.leave.many.replace("{n}", String(w.n)).replace("{name}", w.name);
  return { title: meta.confirm_words.leave_title, sentences: sentence ? [sentence] : [], verb: meta.confirm_words.discard, danger: true, diff: [], fp: "leave" };
}

export function LeaveGuard({ meta }: { meta: Meta }) {
  const navigate = useNavigate();
  const [ask, dialog] = useConfirmBox();
  useEffect(() => {
    go = async (to: string) => {
      if (!anyDirty()) { navigate(to); return; }
      if (await ask({ box: leaveBox(meta), back: meta.confirm_words.keep })) {
        clearDirty();
        navigate(to);
      }
    };
    return () => { go = null; };
  }, [navigate, ask, meta]);
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (!anyDirty()) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, []);
  return <>{dialog}</>;
}
