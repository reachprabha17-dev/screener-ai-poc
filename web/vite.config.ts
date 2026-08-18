/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

/**
 * The reviewer interface is served by the API process at `/ui/` in a deployment
 * (`screener/api/app.py` mounts `web/dist` there), so the dev server uses the
 * same base path. Dev and production then resolve every asset and every route
 * identically — a base that only differs in production is a class of bug that
 * only appears after the build.
 */
const BASE = '/ui/';

/**
 * The API paths this app is allowed to reach, proxied in development so the
 * browser sees one origin.
 *
 * Same-origin is a deliberate choice, not a convenience: it means the API needs
 * no CORS middleware, and there is no configurable "API URL" for an operator to
 * point at another host. The UI can only ever talk to the process that served
 * it, which is the browser-side half of decision #11.
 */
const API_PATHS = [
  '/positions',
  '/runs',
  '/rubrics',
  '/candidates',
  '/audit',
  '/health',
  '/ready',
  '/dashboard',
];

const API_TARGET = process.env.SCREENER_API_URL ?? 'http://127.0.0.1:8010';

export default defineConfig({
  base: BASE,
  plugins: [react(), tailwindcss()],
  server: {
    port: Number(process.env.SCREENER_UI_PORT ?? 5173),
    // Loopback only, matching the API and the Streamlit config it replaces:
    // auth is stubbed, so anything that can reach this port can read every
    // candidate in the system.
    host: '127.0.0.1',
    proxy: Object.fromEntries(
      API_PATHS.map((path) => [path, { target: API_TARGET, changeOrigin: false }]),
    ),
  },
  build: {
    outDir: 'dist',
    // Reviewers keep this open for hours across a deploy; hashed filenames mean
    // a stale index.html cannot pull a mismatched chunk.
    sourcemap: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
