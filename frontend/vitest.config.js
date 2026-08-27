import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{js,jsx}"],
    setupFiles: ["./vitest.setup.js"],
  },
  esbuild: { jsx: "automatic" },
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
});
