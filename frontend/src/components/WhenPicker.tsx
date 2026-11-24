import { Group, Select } from "@mantine/core";
import { DatePickerInput } from "@mantine/dates";

/** A time of day ('HH:MM', 0..1439 minutes) as a person reads it — '6:30 PM', '12:00 AM' (the Clock rule: 12-hour, the
 *  hour's leading zero stripped). A LABEL for a picked wall-clock time only: no date, no timezone, no DST — the server
 *  answers every real start in its own words (Registry.local12). */
export function clock12(minutes: number): string {
  const m = ((Math.round(minutes) % 1440) + 1440) % 1440;
  const h = Math.floor(m / 60);
  return `${h % 12 === 0 ? 12 : h % 12}:${String(m % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

/** 'HH:MM' ↔ minutes of the day (the machine value a time Select holds). */
export const hhmm = (min: number) => `${String(Math.floor(min / 60) % 24).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
export const minutesOf = (value: string) => { const [h, m] = value.split(":").map(Number); return h * 60 + (m || 0); };

/** Every quarter hour of a day, labelled with clock12: the Starts select (never typed). */
export const QUARTER_HOURS = Array.from({ length: 96 }, (_, i) => ({ value: hhmm(i * 15), label: clock12(i * 15) }));

/** The day and time as the open route reads them: guild wall time 'YYYY-MM-DD HH:MM' (rc.open_when resolves it, DST-safe). */
export const wallTime = (day: string, time: string) => `${day} ${time}`;

/** When something starts (the Clock rule): the Day from a calendar (`DatePickerInput`, 'Thu 10 Dec'), the time from a
 *  select of 15-minute steps labelled with clock12 — nobody types a timestamp. `minDate` is the GUILD's date from the
 *  server ('YYYY-MM-DD'), never the browser's (west of UTC the browser's evening is already tomorrow). `children` sit on
 *  the same row (the New event page's Locks). `dayLabel`: Reschedule's "New day". `dayFormat`: the Day's look (a raid's
 *  First opens names the year: 'Wed 9 Dec 2026'). `minDate` left out = any day (First opens may be in the past). */
export function WhenPicker({ day, time, onDay, onTime, minDate, label, dayLabel = "Day", dayFormat = "ddd D MMM", children }: {
  day: string | null; time: string | null; onDay: (d: string | null) => void; onTime: (t: string | null) => void;
  minDate?: string; label: string; dayLabel?: string; dayFormat?: string; children?: React.ReactNode;
}) {
  return (
    <Group gap="md" align="flex-end" wrap="wrap" role="group" aria-label={label}>
      <DatePickerInput label={dayLabel} value={day} onChange={(d) => onDay(d ? String(d).slice(0, 10) : null)} minDate={minDate}
        valueFormat={dayFormat} placeholder="Pick a day" w={170} popoverProps={{ withinPortal: true }} />
      <Select label="Starts" value={time} onChange={onTime} data={QUARTER_HOURS} placeholder="Pick a time" w={140}
        allowDeselect={false} searchable={false} maxDropdownHeight={260} comboboxProps={{ withinPortal: true }} />
      {children}
    </Group>
  );
}
