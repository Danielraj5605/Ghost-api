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
    // Hot reload: run `ghost-api ui --no-open` with GHOST_API_UI_TOKEN set, then
    // `npm run dev` with VITE_GHOST_TOKEN set to the same value (see web/README.md).
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8800",
        changeOrigin: true,
        // The server only accepts changes from its own origin.
        configure: (proxy) =>
          proxy.on("proxyReq", (req) => {
            if (req.getHeader("origin")) req.setHeader("origin", "http://127.0.0.1:8800");
          }),
      },
    },
  },
  test: { environment: "jsdom" },
});
