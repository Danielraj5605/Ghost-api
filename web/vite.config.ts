import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The built dashboard goes into the Python package, so `pip install` users don't need Node.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../ghost_api/web/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    // `ghost-api ui --no-open` on 8800, then `npm run dev` for hot reload.
    proxy: { "/api": { target: "http://127.0.0.1:8800", changeOrigin: true } },
  },
  test: { environment: "jsdom" },
});
