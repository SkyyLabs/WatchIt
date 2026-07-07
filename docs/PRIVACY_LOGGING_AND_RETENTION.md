# Privacy, Logging & Retention

WatchIt watches children browse. The privacy bar is therefore higher than for a normal SaaS:
the product must be able to explain *why it acted* without becoming a surveillance archive.

## Data inventory (what we hold today, on `dev`)

| Data | Where | Sensitivity | Current retention |
| --- | --- | --- | --- |
| Visit URL, title, domain, DOM text sample | `events` (`data_json`/`raw_json`), `event_jobs.event_json` | HIGH (child browsing content) | forever |
| Decisions + LLM rationale | `decisions.details_json` | MEDIUM | forever |
| Screenshots (PNG) | worker-local disk, `screenshots/…` | HIGH | forever (flag-gated) |
| OCR text | `screenshot_files.ocr_text` | HIGH | forever |
| Device tokens / parent PIN | hashes only | — | ok |
| Pairing codes | hash, 15-min TTL | — | ok |
| Guardian identity (Clerk id, email) | `guardians` | MEDIUM | account lifetime |
| Activity log (JSONL incl. event payloads, raw LLM output) | `logs/` on disk | HIGH | forever |
| Dashboard client logs | localStorage + re-emitted to stdout | LOW–MEDIUM | bounded buffer |

## Policy

### Logging (enforced rules)
- Never log: secrets, bearer/device tokens, pairing codes (post-creation), raw screenshots,
  OCR text bodies, raw LLM responses, full DOM samples.
- Always log: request/household/child/device/event/job ids, action, reason class, timings,
  confidence. That is enough to debug the pipeline without replaying a child's browsing.
- **Status:** activity-log persistence (`log_step`, raw LLM captures) is gated behind
  `WATCHIT_AGENT_TRACE_FILES` (off by default, dev-only). `llm_judge` structlog output
  carries `raw_len` only — never raw model text. `screenshots/` and `logs/` are gitignored.

### Retention (implemented — `db.run_retention_sweep`, hourly in the worker, `WATCHIT_RETENTION_SWEEP_ENABLED`)
| Data | Retention | Mechanism |
| --- | --- | --- |
| DOM samples inside `events.data_json`/`raw_json` | 7 days (strip field, keep row) | sweep |
| `event_jobs` completed/failed rows (full payload copies) | 14 days | sweep |
| Screenshots + `ocr_text` | 30 days (`retention_expires_at`, stamped at insert) | sweep |
| Events + decisions/analysis/overrides | 12 months, one window — decisions cascade from events (`ON DELETE CASCADE`), so the original 90-day URL target applies only once decisions are self-contained | sweep |
| Audit log | 24 months | sweep |
| Expired `url_decision_cache` rows | on expiry | sweep |
| Activity trace logs | dev-only (`WATCHIT_AGENT_TRACE_FILES` off by default) | flag |

Sweeps run in the worker on a timer (same process as the queue consumer; no new deployable).

### Guardian-facing privacy controls (dashboard)
- Per-household data-retention view: what is stored, for how long.
- Delete: per-decision screenshot deletion; per-child "delete history"; household export
  (JSON) before delete for account-closure flows.
- Screenshot capture remains opt-in (`WATCHIT_SAVE_SCREENSHOTS` today; becomes a household
  setting with the child-visible privacy statement).

### Child-facing transparency
Blocked/warned pages already show WatchIt branding. The interstitial should state what was
recorded (URL + reason), not pretend to be a network error. Monitoring without notice is a
trust and, in several jurisdictions, a legal problem.

### Tenancy invariants (unchanged, load-bearing)
Every read/write is scoped by the authenticated household (guardian path) or device-derived
household (extension path). The two gaps found on dev — unscoped
`update_event_data_json` via `/v1/event/upgrade`, and household-wide device SSE — are fixed as
part of the security slice (see `DEVICE_SECURITY_AND_PAIRING.md`).
