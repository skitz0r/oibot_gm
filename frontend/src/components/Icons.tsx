import { Box, Tooltip } from "@mantine/core";
import type { Meta } from "../api";

export type IconKind = "class" | "role" | "spec" | "profession";

// Blizzard render-CDN art proxied by the bot (/img/icon/<name>.jpg); generated badges as fallback.
export function iconUrl(meta: Meta | null, kind: IconKind, key: string): string {
  const icons = meta?.icons;
  const name = kind === "class" ? icons?.classes[key] : kind === "role" ? icons?.roles[key] : kind === "spec" ? icons?.specs[key]
    : meta?.professions?.list.find((p) => p.id === key)?.icon;
  if (name) return `/img/icon/${name}.jpg`;
  // a profession has no generated badge: the CDN's question-mark icon stands in
  return kind === "spec" ? `/img/role/melee.png` : kind === "profession" ? `/img/icon/inv_misc_questionmark.jpg` : `/img/${kind}/${key}.png`;
}

export function GameIcon({ meta, kind, id, size = 22, title, dim }: { meta: Meta | null; kind: IconKind; id: string; size?: number; title?: string; dim?: boolean }) {
  return (
    <Box
      component="img"
      src={iconUrl(meta, kind, id)}
      alt={title || id}
      title={title || id}
      style={{ width: size, height: size, borderRadius: size > 24 ? 8 : 5, border: "1px solid var(--mantine-color-slate-5)", flex: "none", opacity: dim ? 0.4 : 1, objectFit: "cover" }}
    />
  );
}

/** A minimum's slot icon alone — role, class or spec — with its words in the tooltip (the iconography rule). "dps" (not
 *  a game role) is drawn as the melee + ranged pair. */
export function SlotIcon({ meta, slot, label, size = 22 }: { meta: Meta; slot: string; label?: string; size?: number }) {
  if (slot === "dps") return <RoleIcon meta={meta} role="dps" size={size} />;
  const kind = meta.roles.includes(slot) ? "role" : slot.includes(":") ? "spec" : "class";
  return (
    <Tooltip label={label || slot}>
      <Box style={{ display: "inline-flex" }}><GameIcon meta={meta} kind={kind} id={slot} size={size} title={label || slot} /></Box>
    </Tooltip>
  );
}

/** A role's icon: tank and healer have one; dps is melee and ranged together (the profile has no dps icon). */
export function RoleIcon({ meta, role, size = 22 }: { meta: Meta; role: string; size?: number }) {
  if (role !== "dps") return <SlotIcon meta={meta} slot={role} size={size} />;
  return (
    <Tooltip label="dps">
      <Box style={{ display: "inline-flex" }}>
        <GameIcon meta={meta} kind="role" id="melee" size={size} title="dps" />
        <Box style={{ marginLeft: -Math.round(size / 3), display: "inline-flex" }}><GameIcon meta={meta} kind="role" id="ranged" size={size} title="dps" /></Box>
      </Box>
    </Tooltip>
  );
}
