---
description: Run the WatchIt verification suite (compile, deps, migration graph, tests, dashboard typecheck)
---

Run `make verify` and report pass/fail per step. It runs, in order: Python compile check, `pip check`, `alembic heads` (must show one head), `pytest`, and the dashboard typecheck. The command list lives only in the Makefile — do not restate it here or elsewhere.

```bash
make verify
```

On failure, quote the shortest decisive line, not the whole log. This suite is read-only/build-only — it never runs `alembic upgrade`/`downgrade` against the live Neon DB. For a full dashboard production build (dummy Clerk keys), see `.claude/rules/frontend.md`.
