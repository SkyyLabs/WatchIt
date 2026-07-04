# Browser extension rules

Applies when touching `apps/browser-extension/` (Chromium MV3).

- **Dependency-free plain JS. No bundler.** Keep it that way unless bundling is deliberately added and justified.
- Files: `manifest.json` (MV3), `background.js` (service worker), `content.js`, `popup.html` / `popup.js`.
- `background.js`: observes navigation, samples DOM, handles screenshots, redeems pairing codes, stores the device token, posts events, listens for SSE decisions.
- `content.js`: applies page-level actions — allow, warn, blur, block, notify.
- `popup.js` / `popup.html`: device pairing UI.
- **Message types are explicit and namespaced**, e.g. `watchit_pair`, `watchit_decision`. Keep the convention.
- **Auth is the paired device token**, stored in `chrome.storage.local` — never a Clerk guardian token. Ingest scopes from this token, not client-set ids.
- Never embed Clerk secret keys, server secrets, or the database URL in extension code.
- The API base is `const API` in `background.js` (default `http://127.0.0.1:4849`).
- Never log device tokens, pairing codes (after creation), or raw screenshots.
