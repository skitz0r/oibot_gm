import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built bundle is served by the bot's FastAPI at /app; in dev, API/auth/icons proxy to the running bot.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "../src/oibot_gm/web/static/app", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8788",
      "/img": "http://127.0.0.1:8788",
      "/auth": "http://127.0.0.1:8788",
    },
  },
});
