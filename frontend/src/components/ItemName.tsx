import type { CSSProperties } from "react";

/** The game version's item page address with `{id}` in it (/api/meta `item_url`, the profile's item link template), set
 *  by App when /api/meta answers; null = this version links no items. */
let template: string | null = null;
export function setItemUrl(t: string | null | undefined) {
  template = t || null;
}

/** An item's page address, or null (no id, or a version without the template). The address is built from the server's
 *  template alone. */
export function itemHref(id: number | null | undefined): string | null {
  return template && id ? template.replace("{id}", String(id)) : null;
}

/** An item's name, everywhere the site prints one (auction prices §4.6): with an id and the version's template, a plain
 *  outbound link to the item's page (a new tab, no referrer, no third-party script or tooltip); else the name as text.
 *  Inline: it sits inside the caller's Text, which sets the size and weight. A press opens the page only, never the row
 *  it sits in (Loot › Items' rows open the item's tiers). */
export function ItemName({ id, name, style }: { id?: number | null; name: string; style?: CSSProperties }) {
  const href = itemHref(id);
  if (!href) return <span style={style}>{name}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()} style={{ color: "inherit", textDecoration: "underline", textDecorationColor: "var(--mantine-color-dimmed)", textUnderlineOffset: 2, ...style }}>
      {name}
    </a>
  );
}
