import { Anchor, Group } from "@mantine/core";
import { Fragment } from "react";
import { Link } from "react-router-dom";
import type { Fact } from "../api";
import { Eyebrow } from "./Page";
import { guardClick } from "./LeaveGuard";
import css from "../pages/listpage.module.css";

/** A place a Fact points at: a site path (`/schedules/tuesday`) is a router link through the leave guard (an open draft is never dropped unasked), anything else (a Discord jump link) a
 *  plain link in a new tab. */
function Place({ href, children }: { href: string; children: React.ReactNode }) {
  return href.startsWith("/")
    ? <Anchor component={Link} to={href} onClick={guardClick(href)}>{children}</Anchor>
    : <Anchor href={href} target="_blank" rel="noopener noreferrer">{children}</Anchor>;
}

/** The server's Details lines (event_view / schedule_view / raid_page `Fact`s) as a label / value grid: the value in the
 *  server's words, linked where it says (`href`, or `links` for several places). `show` lets a view draw one value
 *  itself — a chip for a level, a select in an edit mode — and returns undefined for the rest. */
export function Facts({ items, show }: { items: Fact[]; show?: (f: Fact) => React.ReactNode | undefined }) {
  return (
    <dl className={css.facts}>
      {items.map((f) => {
        const own = show?.(f);
        const value = own !== undefined ? own
          : f.links && f.links.length > 1 ? f.links.map((l, i) => <Fragment key={l.href}>{i > 0 && ", "}<Place href={l.href}>{l.name}</Place></Fragment>)
          : f.href ? <Place href={f.href}>{f.value}</Place> : f.value;
        return <div key={f.label}><dt>{f.label}</dt><dd>{value}</dd></div>;
      })}
    </dl>
  );
}

/** One section of a list page's detail: its heading, an optional action at the right, then the body. */
export function ViewSection({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className={css.sect} aria-label={title}>
      <Group justify="space-between" align="center" mb={10} wrap="wrap" gap="xs">
        <Eyebrow>{title}</Eyebrow>
        {action}
      </Group>
      {children}
    </section>
  );
}
