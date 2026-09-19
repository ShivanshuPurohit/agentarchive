import { defineConfig } from "vite";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const notices = {
  name: "dependency-notices",
  generateBundle() {
    const lock = JSON.parse(
      readFileSync(join(root, "package-lock.json"), "utf8"),
    );
    const sections = [];
    for (const [path, info] of Object.entries(lock.packages)) {
      if (!path.startsWith("node_modules/") || info.dev) continue;
      const dir = join(root, path);
      if (info.optional && !existsSync(dir)) continue;
      const metadata = JSON.parse(
        readFileSync(join(dir, "package.json"), "utf8"),
      );
      const files = readdirSync(dir).filter((name) =>
        /^(licen[cs]e|copying)([-.]|$)/i.test(name),
      );
      sections.push(
        `${metadata.name}@${metadata.version}\nLicense: ${metadata.license || "See package notices"}\n${files.map((name) => readFileSync(join(dir, name), "utf8")).join("\n")}`,
      );
    }
    this.emitFile({
      type: "asset",
      fileName: "THIRD_PARTY_NOTICES.txt",
      source: sections.join("\n\n" + "=".repeat(72) + "\n\n"),
    });
  },
};

export default defineConfig({
  publicDir: false,
  plugins: [notices],
  build: {
    outDir: "../agentarchive/web",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  esbuild: { jsx: "automatic" },
});
