import { defineConfig } from "vite";

export default defineConfig({
  publicDir: "vendor",
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
  esbuild: { jsx: "automatic" },
});
