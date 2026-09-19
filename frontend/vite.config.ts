import { defineConfig, type UserConfig } from "vite";
import type { UserConfig as VitestConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const config: UserConfig & { test: VitestConfig["test"] } = {
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/watchlist": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/quotes": "http://127.0.0.1:8000",
      "/push": "http://127.0.0.1:8000",
      "/anomaly": "http://127.0.0.1:8000",
      "/briefing": "http://127.0.0.1:8000",
      "/explain": "http://127.0.0.1:8000",
      "/runtime": "http://127.0.0.1:8000",
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
  },
};

export default defineConfig(config);
