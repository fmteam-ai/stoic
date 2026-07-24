import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import visualEdits from "@emergentbase/visual-edits/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "REACT_APP_");
  return {
    plugins: [react(), visualEdits()],
    resolve: {
      alias: { "@": path.resolve(import.meta.dirname, "src") },
    },
    // The codebase reads process.env.REACT_APP_* (CRA convention) — keep it working.
    define: Object.fromEntries(
      Object.entries(env).map(([k, v]) => [`process.env.${k}`, JSON.stringify(v)])
    ),
    envPrefix: "REACT_APP_",
    server: {
      host: "0.0.0.0",
      port: 3000,
      strictPort: true,
      allowedHosts: true,
      hmr: { clientPort: 443 },
      watch: {
        ignored: ["**/node_modules/**", "**/.git/**", "**/build/**",
          "**/dist/**", "**/coverage/**"],
      },
    },
    build: {
      outDir: "build",
      sourcemap: false,
    },
  };
});
