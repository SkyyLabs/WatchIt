# OCR Pipeline Design

Current flow (works, keep the shape): ambiguous LLM judgment → interim `warn/pending_ocr` →
extension captures visible tab → `POST /v1/event/upgrade` → worker re-enters the graph at the
OCR node → Docling extracts text → second LLM judge pass → final decision.

## Problems in the current implementation

1. **No payload limits** — arbitrary base64 lands in `event_jobs.event_json` (multi-MB rows).
2. **Dedup only in service-worker memory** (`upgradedEvents` set) — SW eviction re-uploads.
3. **No retention** — `screenshot_files.retention_expires_at` exists but is never set or
   enforced; `screenshots/` grows forever; sample `metadata.json` files are committed to git.
4. **No timeout/metrics** around Docling `convert`; exceptions silently become empty text.
5. **Privacy**: `ocr_text` persisted indefinitely in Postgres; raw LLM responses and DOM
   samples written to the activity log.
6. **Docling fit**: it is a document-conversion stack (HF models, torch) — heavy cold start
   (minutes on first model download), seconds per screenshot, large deploy image.

## Target design

### Capture (extension)
- Capture **only** when a decision explicitly carries `needs_ocr` (already true).
- Downscale/compress before upload: JPEG quality ~60, max width 1280 (halves-to-quarters the
  payload with no OCR quality loss for screen text). `captureVisibleTab` supports jpeg+quality.
- One upgrade per event id, persisted in `chrome.storage.session` so SW eviction doesn't
  re-upload.

### Upload (API)
- Device-token auth (already true) + **event must belong to the device's household** (fix).
- Reject payloads over 4 MB (413) and more than 3 screenshots per event (400).
- Server-side dedup: if the event already has an upgrade job pending/completed, return the
  existing job instead of enqueueing another.

### Processing (worker)
- Hard timeout around OCR (e.g. 20 s per screenshot, `asyncio.wait_for` /
  thread + timeout). Timeout ⇒ re-judge without OCR text, decision reason carries
  `ocr_timeout` so the dashboard can distinguish it. Never allow-by-default on OCR failure
  (existing invariant, keep).
- Metrics/logs: per-screenshot OCR latency, failure counter, queue wait — logged today at
  DEBUG; promote OCR failures to WARN with a counter tag.
- Dead-letter visibility: `event_jobs` rows with `status='failed'` (and `attempts >= max`)
  surfaced through a guardian-invisible ops endpoint / dashboard admin card; stale
  `processing` jobs older than N minutes reaped back to `pending` with an attempt cap.

### Storage & retention
- Image bytes stay out of Postgres (existing invariant).
- `retention_expires_at` set at insert (default 30 days); a periodic sweep deletes expired
  files + rows. Guardian delete control comes with the privacy settings work.
- `ocr_text` gets the same retention sweep; it exists to explain decisions, not to archive
  browsing content.
- Remove committed `screenshots/**` artifacts from the repo and gitignore the directory.

### Is Docling the right tool?
For production browser screenshots: **no long-term.** Screen text OCR wants a lightweight
engine (Tesseract via a thin service, PaddleOCR-server, or a hosted vision API). Better still:
Claude is already the production judge and is **multimodal** — sending the (downscaled)
screenshot directly to the judge model removes the OCR hop entirely, cuts a full LLM round
trip, and improves judgment on image-heavy pages where OCR text is empty. Recommended path:
keep Docling as the local/offline fallback, add a `vision` judge mode when
`WATCHIT_LLM_PROVIDER=anthropic`, measure, then retire Docling from the hot path.

## Logging rules for this pipeline
Never log: image bytes, OCR text bodies, raw model responses. Log: event/job ids, screenshot
count, byte sizes, latencies, failure classes. The activity logger's `llm_raw_response` and
full-event `log_step` records must be trimmed to metadata (see
`PRIVACY_LOGGING_AND_RETENTION.md`).
