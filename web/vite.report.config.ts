import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Put the built script and stylesheet inside the HTML, so the report is one file. */
function inlineIntoHtml(): Plugin {
  return {
    name: "ghost-inline-into-html",
    enforce: "post",
    generateBundle(_options, bundle) {
      const page = Object.values(bundle).find((f) => f.type === "asset" && f.fileName.endsWith(".html"));
      if (!page || page.type !== "asset") return;
      let html = String(page.source);
      for (const [name, file] of Object.entries(bundle)) {
        if (file.type === "chunk") {
          const tag = new RegExp(`<script[^>]*src="[^"]*${escapeRegExp(name)}"[^>]*></script>`);
          // "</script" inside the code would end the element early.
          const code = file.code.replace(/<\/script/gi, "<\\/script");
          html = html.replace(tag, () => `<script type="module">${code}</script>`);
          delete bundle[name];
        } else if (name.endsWith(".css")) {
          const tag = new RegExp(`<link[^>]*href="[^"]*${escapeRegExp(name)}"[^>]*>`);
          html = html.replace(tag, () => `<style>${String(file.source)}</style>`);
          delete bundle[name];
        }
      }
      page.source = html;
    },
  };
}

// The shareable report viewer: built into the Python package as one HTML template that
// ghost_api/export/html.py fills with a site's data.
export default defineConfig({
  plugins: [react(), tailwindcss(), inlineIntoHtml()],
  base: "./",
  publicDir: false,
  build: {
    outDir: "../ghost_api/export/viewer",
    emptyOutDir: true,
    assetsInlineLimit: Number.MAX_SAFE_INTEGER,
    cssCodeSplit: false,
    modulePreload: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      input: "report.html",
      output: { inlineDynamicImports: true },
    },
  },
});
