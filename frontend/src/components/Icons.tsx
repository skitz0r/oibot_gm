import { Box } from "@mantine/core";
import type { Meta } from "../api";

// Blizzard render-CDN art proxied by the bot (/img/icon/<name>.jpg); generated badges as fallback.
export function iconUrl(meta: Meta | null, kind: "class" | "role" | "spec", key: string): string {
  const icons = meta?.icons;
  const name = kind === "class" ? icons?.classes[key] : kind === "role" ? icons?.roles[key] : icons?.specs[key];
  if (name) return `/img/icon/${name}.jpg`;
  return kind === "spec" ? `/img/role/melee.png` : `/img/${kind}/${key}.png`;
}

export function GameIcon({ meta, kind, id, size = 22, title, dim }: { meta: Meta | null; kind: "class" | "role" | "spec"; id: string; size?: number; title?: string; dim?: boolean }) {
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
