# Frontend rules

Applies when touching `apps/dashboard/` (Next.js App Router + Clerk).

- **Never touch Postgres directly.** The dashboard calls the FastAPI HTTP API through `src/lib/api-client.ts`. No DB drivers, no Python logic in the dashboard.
- **Client vs server boundary.** Client components start with `"use client"`. Secrets, database URLs, Clerk secret keys, and Pino server logging stay server-side, never shipped to the browser.
- **Auth** is Clerk. Middleware in `src/proxy.ts`. Sign-in/up routes under `src/app/sign-in` / `sign-up`.
- **UI** uses Tailwind + shadcn/ui primitives under `src/components/ui`. Feature UI in `src/components/dashboard`. Route-level pages in `src/app`.
- **Extract only on real reuse.** Keep one-off UI inside the component that uses it. Don't build shared abstractions for single-use code.
- **Logging:** client uses `src/lib/client-logger.ts` (also persists recent JSON to `localStorage`, exportable via "Export logs"); server uses Pino via `src/lib/server-logger.ts`. Never log secrets or Clerk tokens.
- **State:** local React state until shared complexity justifies a library. Persist only safe client state in `localStorage` — never raw Clerk tokens.
- Don't add a landing page when the request is app functionality — the dashboard is the primary experience.
- Keep forms accessible: labels, disabled states, clear errors, keyboard-operable. Don't rely on color alone for critical state.

## Build check

```bash
cd apps/dashboard && NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_dummy CLERK_SECRET_KEY=sk_test_dummy npm run build
```
