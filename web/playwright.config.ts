import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  retries: 0,
  use: { baseURL: process.env.WEB_URL ?? "http://web:80" },
});
