import { Box } from "@mantine/core";
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
