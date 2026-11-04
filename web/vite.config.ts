import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies like the nginx `web` service, so the SPA only ever calls its own origin.
export default defineConfig({
  plugins: [react()],
  build: { sourcemap: false },
  server: {
    proxy: {
      "/api/gw": { target: "http://gateway-api:8000", rewrite: (p) => p.replace(/^\/api\/gw/, "") },
      "/api/ops": { target: "http://ops-ai-api:8000", rewrite: (p) => p.replace(/^\/api\/ops/, "") },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/__tests__/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
