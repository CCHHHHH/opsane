# Shell Agent Vue workbench

This directory contains the incremental Vue replacement for the legacy single-file UI.

```bash
npm install
npm run dev
npm test
npm run build
```

The development server proxies `/api` and `/ws` to `http://127.0.0.1:8010`.
Set `VITE_BACKEND_URL` to override that target.
Production builds use the `/next/` base path and are emitted to
`shell_agent/web/static/next/`; the Python runtime does not need Node.js.

UI components consume Pinia stores only. HTTP and WebSocket transport live in
`src/api`, while `src/api/protocol.ts` is the typed mirror of the frozen backend
protocol contract.

## Browser E2E tests

The Playwright suite uses the production Vue build with a deterministic local
FastAPI HTTP/WebSocket fixture. The fixture contains no SSH executor or LLM and
cannot connect to configured servers. It covers confirmation idempotency and
task-state restoration across a real browser refresh.

Install Chromium once, then run the suite:

```bash
npm run test:e2e:install
npm run test:e2e
```

For interactive debugging use `npm run test:e2e:ui`. To run the complete local
frontend gate (type checking, Vitest, production build and Playwright), run:

```bash
npm run test:quality
```

The fake service lives only under `e2e/`; do not point E2E tests at port 8010 or
at a real inventory. Browser traces, screenshots and videos are retained only
for failed tests in `test-results/` and `playwright-report/`.
