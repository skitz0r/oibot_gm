import { useRef, useState } from "react";
import { Select } from "@mantine/core";
import { api, type Box, type EventView } from "../../api";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { WhenPicker, wallTime } from "../WhenPicker";
import { boxOf } from "./shared";

/** What the picker holds: the new day ('YYYY-MM-DD'), the start ('HH:MM' guild wall time) and the lock (hours before). */
type When = { day: string | null; time: string | null; lock: string };


/** The box's fields: New day from a calendar (its lower bound the GUILD's date from the server), Starts from quarter
 *  hours, and Locks while the event is open (once locked the sheet timing is fixed, so it is not offered). */
function RescheduleFields({ ev, first, onPick }: { ev: EventView; first: When; onPick: (w: When) => void }) {
  const [w, setW] = useState(first);
  const set = (x: Partial<When>) => { const next = { ...w, ...x }; setW(next); onPick(next); };
  return (
    <WhenPicker label="When the event starts now" dayLabel="New day" day={w.day} time={w.time} minDate={ev.today}
      onDay={(day) => set({ day })} onTime={(time) => set({ time })}>
      {ev.state === "open" && ev.locks.length > 0 && (
        <Select label="Locks" w={180} allowDeselect={false} value={w.lock} comboboxProps={{ withinPortal: true }}
          data={ev.locks.map((l) => ({ value: String(l.hours), label: l.words }))} onChange={(v) => { if (v) set({ lock: v }); }} />
      )}
    </WhenPicker>
  );
}

/** Reschedule (box 3, design §7b): the SERVER's box with the picker inside it. It opens on the server's pick box (the
 *  event named, nothing to press yet); every pick asks POST /api/run/{key}/reschedule `preview: true` again and the box
 *  takes the answer — a refusal (a past time, a clash) is the refusal box, only Back, the picker still there to pick
 *  again. Move sends the picked start / lock with the fp of the box on screen as `seen`: a 409 brings the fresh box under
 *  the "changed" line. A live reload of the page never closes the box or drops the pick (the pick lives here). */
export function useRescheduleBox(event: EventView, onDone: () => void): [() => void, React.ReactNode] {
  const [ask, dialog, replace] = useConfirmBox(["run:"]);
  const latest = useRef(event);
  latest.current = event;

  async function open() {
    const ev = latest.current;
    if (!ev.boxes.reschedule) return;
    const own: When = { day: ev.start_day, time: ev.start_time, lock: String(ev.timing.lock_hours_before ?? "") };
    const now: When = { ...own };
    const url = `/api/run/${encodeURIComponent(ev.key)}/reschedule`;
    const change = () => {
      const moved = !!now.day && !!now.time && (now.day !== own.day || now.time !== own.time);
      const locks = ev.state === "open" && now.lock !== own.lock;
      return moved || locks ? { ...(moved ? { start: wallTime(now.day!, now.time!) } : {}), ...(locks ? { timing: { lock_hours_before: Number(now.lock) } } : {}) } : null;
    };
    const preview = async (): Promise<Box> => {
      const body = change();
      if (!body) return latest.current.boxes.reschedule || ev.boxes.reschedule!;
      try { return (await api.post<{ box: Box }>(url, { ...body, preview: true })).box; } catch (x) { const b = boxOf(x); if (b) return b; throw x; }
    };
    let seq = 0;
    let said = "";
    const done = await ask({
      box: ev.boxes.reschedule, wide: true,
      fields: <RescheduleFields ev={ev} first={own} onPick={(w) => { Object.assign(now, w); const n = ++seq; preview().then((b) => { if (n === seq) replace(b); }).catch(fail); }} />,
      refresh: preview,
      act: async (fp) => {
        const body = change();
        if (!body) return preview();
        try { said = (await api.post<{ message: string }>(url, { ...body, seen: fp })).message; return null; }
        catch (x) { const b = boxOf(x); if (b) return b; throw x; }
      },
    });
    if (done) { ok(said); onDone(); }
  }
  return [() => { open().catch(fail); }, dialog];
}
