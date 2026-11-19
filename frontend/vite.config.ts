/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `vite preview` serves the same security headers as Vercel (vercel.json), so a page that the
// Content Security Policy would break shows up locally first.
const vercel = JSON.parse(readFileSync(new URL("./vercel.json", import.meta.url), "utf8")) as {
  headers: { source: string; headers: { key: string; value: string }[] }[];
};
const securityHeaders = Object.fromEntries(
  vercel.headers.flatMap((h) => h.headers).map((h) => [h.key, h.value]),
);
const apiProxy = {
  "/api": {
    target: process.env.API_PROXY_TARGET ?? "http://localhost:8000",
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};

// The browser always calls /api on its own origin, so the session cookie is first-party: here
// Vite proxies it to the API, and on Vercel a rewrite in vercel.json does the same.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy: apiProxy },
  preview: { port: 4173, proxy: apiProxy, headers: securityHeaders },
  test: {
    environment: "jsdom",
  },
});
