import { Anchor, Group, Title } from "@mantine/core";
import { Link } from "react-router-dom";
import { guardNav } from "./LeaveGuard";
import css from "../pages/listpage.module.css";

export type ListItem = { id: string; title: string; subtitle?: string; to: string; chip?: string };
export type ListGroup = { label: string; items: ListItem[] };

/** A list page (phase 4 — Raids, Events, Schedules; design §7a): the title with its one action, the list (≤ 290 px,
 *  grouped) beside the chosen item's detail. Below sm the list alone while nothing is chosen, else the detail under a
 *  "Back to …" link. Every pick and the Back link go through the leave guard, so a draft is never dropped unasked.
 *  A group's label is printed only when the page has more than one group. `empty` stands in for an empty list. */
export function ListPage({ title, action, groups, selected, back, children, empty }: {
  title: string; action?: React.ReactNode; groups: ListGroup[]; selected: string | null; back: string;
  children?: React.ReactNode; empty?: React.ReactNode;
}) {
  const none = groups.every((g) => !g.items.length);
  const pick = (to: string) => (e: React.MouseEvent) => { e.preventDefault(); guardNav(to); };
  return (
    <div className={css.touch}>
      <Group justify="space-between" align="center" wrap="wrap" gap="sm">
        <Title order={1} size="h2">{title}</Title>
        {action}
      </Group>
      <div className={[css.split, selected !== null ? css.chosen : ""].join(" ")}>
        <nav className={css.list} aria-label={title}>
          {none ? empty : groups.map((g) => (
            <div key={g.label} style={{ display: "contents" }}>
              {groups.length > 1 && g.items.length > 0 && <div className={css.group}>{g.label}</div>}
              {g.items.map((it) => (
                <a key={it.id} href={`/app${it.to}`} data-list-item className={[css.item, it.id === selected ? css.on : ""].join(" ")}
                  aria-current={it.id === selected ? "page" : undefined} onClick={pick(it.to)}>
                  <span className={css.itemTitle}>{it.title}</span>
                  {it.subtitle}{it.chip ? ` · ${it.chip}` : ""}
                </a>
              ))}
            </div>
          ))}
        </nav>
        <div className={css.detail}>
          {selected !== null && (
            <Anchor component={Link} to={back} className={css.back} onClick={pick(back)} size="sm">Back to {title}</Anchor>
          )}
          {children}
        </div>
      </div>
    </div>
  );
}
