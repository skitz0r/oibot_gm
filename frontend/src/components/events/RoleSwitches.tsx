import { useState } from "react";
import { Badge, Button, Group, Stack, Text, VisuallyHidden } from "@mantine/core";
import { api, ApiError, type Box, type EventView, type Meta, type SwitchRow, type Switches } from "../../api";
import { fail, ok } from "../Page";
import { useConfirmBox } from "../ConfirmBox";
import { ViewSection } from "../Facts";
import { GameIcon } from "../Icons";
import { ShortRoles } from "./EventBoard";
import { boxOf } from "./shared";
import css from "../../pages/listpage.module.css";

const FOLD = "oibot.events.switches";  // the viewer's own fold (a per-viewer convenience: localStorage)
const same = (a: SwitchRow, b: SwitchRow) => a.uid === b.uid && a.character === b.character && a.spec === b.spec && a.part === b.part;

/** THE press of one Role switches row — the section's button and a Signups row's ⋮ item (SignupsSection) both call it:
 *  the row's own box from the server; the verb sends the row with its raid's plan rev (`view.switches.revs`) and the
 *  box's fp as `seen`: a box that changed comes back fresh, a list that changed brings the row's fresh box (or, when the
 *  row is gone, says so in the server's words and the page reloads). `dialog` is mounted by the caller, which stays
 *  mounted while the box is open (a menu that closes never takes the box with it). */
export function useSwitchPress(view: EventView, onReload: () => void): [(row: SwitchRow) => Promise<void>, React.ReactNode] {
  const [ask, dialog] = useConfirmBox(["run:", "member"]);
  const switches = view.switches;
  async function press(first: SwitchRow) {
    let row = first;
    let said = "";
    let gone = "";
    let revs: Record<string, string> | null = null;  // the plan revs a 409 brought, once one did
    const url = () => `/api/run/${encodeURIComponent(row.part)}/switch`;
    const body = (extra: object) => ({ uid: row.uid, character: row.character, spec: row.spec, part: row.part, seat: row.seat, option: row.option, rev: revs ? revs[row.part] : switches.revs[row.part], ...extra });
    /** a 409 that carries the fresh list: the same person's row there (its fresh box waits), else the list's sentence */
    const fresh = (e: unknown): Box | null => {
      const b = e instanceof ApiError ? (e.body as { error?: string; switches?: Switches } | undefined) : undefined;
      if (!b?.switches) return null;
      revs = b.switches.revs;
      const again = b.switches.rows.find((r) => same(r, row));
      if (again) { row = again; return again.box; }
      gone = b.error || "";
      return null;
    };
    const done = await ask({
      box: row.box,
      refresh: async (): Promise<Box> => (await api.post<{ box: Box }>(url(), body({ preview: true }))).box,
      act: async (fp) => {
        try { said = (await api.post<{ message: string }>(url(), body({ seen: fp }))).message; return null; }
        catch (e) {
          const b = boxOf(e); if (b) return b;
          const f = fresh(e); if (f) return f;
          if (gone) return null;
          throw e;
        }
      },
    });
    if (gone) { fail(new Error(gone)); onReload(); return; }
    if (done) { ok(said); onReload(); }
  }
  return [press, dialog];
}

/** Role switches (Main.dc.html): drawn only while a raid of the event is short of a minimum — one fact per short raid
 *  (`{Raid} is short`, role icon + number) with the server's `why` when it has no switch to offer, then the fold
 *  `Show {n} possible switches` / `Hide the {n} switches` (folded until opened; the viewer's choice kept), one line per
 *  switch: the role it fills, `{Name}: {from} → {to}`, a chip per raid it helps and the server's button word (Switch ·
 *  Answer for them · Ask). A press opens the row's own box from the server; the verb sends the row with its raid's plan
 *  rev and the box's fp as `seen`: a box that changed comes back fresh, a list that changed brings the row's fresh box
 *  (or, when the row is gone, says so in the server's words and the section redraws from the fresh rows). */
export function RoleSwitches({ meta, view, onReload }: { meta: Meta; view: EventView; onReload: () => void }) {
  const switches = view.switches;
  const [open, setOpen] = useState(() => { try { return localStorage.getItem(FOLD) === "1"; } catch { return false; } });
  const [press, dialog] = useSwitchPress(view, onReload);
  if (!switches || switches.short.length === 0) return null;
  const words = meta.page_words;
  const n = switches.rows.length;
  const fold = () => setOpen((o) => { try { localStorage.setItem(FOLD, o ? "0" : "1"); } catch { /* private window: per visit */ } return !o; });

  return (
    <ViewSection title="Role switches" action={n > 0 ? (
      <Button variant="default" size="sm" aria-expanded={open} aria-controls="event-switches" onClick={fold}>
        {(open ? words.hide_switches : words.show_switches).split("{n}").join(String(n))}
      </Button>
    ) : undefined}>
      <dl className={css.facts}>
        {switches.short.map((s) => (
          <div key={s.raid}>
            <dt>{words.raid_short.split("{raid}").join(s.raid)}</dt>
            <dd>
              <ShortRoles meta={meta} roles={s.roles} />
              {s.why && <Text size="sm" c="dimmed" fw={400} mt={4}>{s.why}</Text>}
            </dd>
          </div>
        ))}
      </dl>
      {open && n > 0 && (
        <Stack id="event-switches" gap={0} mt="md">
          {switches.rows.map((row, i) => (
            <Group key={`${row.uid}:${row.character}:${row.spec}:${row.part}`} justify="space-between" wrap="wrap" gap="xs" py={6}
              style={{ borderTop: i ? "1px solid var(--mantine-color-slate-5)" : undefined }}>
              <Group gap={10} wrap="wrap" style={{ minWidth: 0 }}>
                <GameIcon meta={meta} kind="role" id={row.role} size={15} title={row.role} />
                <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                  <Text span fw={700} size="sm">{row.display_name}</Text>: {row.from}{" "}
                  <Text span c="dimmed" size="sm" aria-hidden="true">→</Text><VisuallyHidden>to</VisuallyHidden>{" "}{row.to}
                </Text>
                {row.raids.map((r) => <Badge key={r} variant="light" color="gray" tt="none" radius="xl">{r}</Badge>)}
              </Group>
              <Button variant="default" size="sm" aria-label={`${row.button}: ${row.display_name}, ${row.to}`} onClick={() => { press(row).catch(fail); }}>{row.button}</Button>
            </Group>
          ))}
        </Stack>
      )}
      {dialog}
    </ViewSection>
  );
}
