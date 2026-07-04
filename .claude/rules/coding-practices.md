# Coding practices

Applies to all code changes. This is the canonical detail behind CLAUDE.md's "Working in this repo" summary. The one test that catches most violations: **every changed line should trace directly to the request.** If you can't say which asked-for thing a line serves, cut it.

## Don't over-engineer (YAGNI)

Build the minimum that solves the stated problem. Nothing speculative.

- No feature, endpoint, flag, or option that wasn't asked for.
- No error handling for cases that can't occur. Trust internal callers and framework guarantees; validate only at real boundaries (extension/API input, LLM output, DB reads).
- No config knob for a value that has exactly one caller and one sensible default. Inline it.
- No "we might need it later." Add it later, when "later" arrives.
- If a bug fix is 3 lines, ship 3 lines. Don't refactor the surrounding function because you're in there.

Ask before writing: *would a senior reviewer call this more than the task needs?* If yes, cut.

## Don't over-modularize

Abstraction is a cost (indirection, a file to open, a name to learn). Pay it only when it buys reuse or removes real duplication.

- **Don't extract a function/component/helper used once.** Inline code the reader can see beats a jump to a one-caller helper.
- **Don't add a layer to "separate concerns"** when the concern lives in one place. This repo already has its boundaries (`apps/`, `services/`, `packages/core`); work within them, don't invent sub-layers inside them.
- **Don't split a file because it's "getting long."** Split when parts change independently or get reused — not by line count.
- **Rule of three:** extract on the third real duplication, not the first. Two similar blocks are often cheaper left apart than fused under a wrong abstraction.
- A premature/wrong abstraction is worse than duplication — it's harder to unwind. When unsure, duplicate and wait for the pattern to prove itself.

## Don't duplicate (the other direction)

Once a thing is genuinely shared, define it once.

- Cross-service Python helpers → `packages/core`. Don't copy a function between `apps/api` and `services/agent-worker`.
- Command sequences (verify, migrate, run) → the Makefile, referenced by CI and docs. Don't restate the command list in a slash command, a doc, and CI — they drift.
- Facts (env flags, table lists, invariants) → one authoritative file; other files link to it, not re-describe it.
- Config values → env-driven via `config.py` / `.env`, never hardcoded at call sites.

Duplication and over-abstraction are opposite failures. The target is the middle: share what's proven shared, inline what's used once.

## Surgical changes

- Touch only what the request requires. Don't reformat, rename, or "improve" adjacent code.
- Match the surrounding style, naming, and comment density even if you'd personally do it differently.
- Remove imports/vars/functions **your** change orphaned. Leave pre-existing dead code alone — mention it, don't delete it, unless asked.
- Don't add dependencies without a clear, stated reason. A few lines of local code beats a new package for a trivial need.

## Good practice, house style

- **Reuse before writing.** Grep for an existing helper/repository method/component before adding one.
- **Verify, don't assert.** Before claiming done, run `make verify` (or the relevant subset) and report real output — see `.claude/commands/verify.md`.
- **Follow the domain rules** for the area you touch — `database.md`, `api.md`, `frontend.md`, `agent-worker.md`, `extension.md`, `security.md`.
- **Name for the reader:** explicit scope in repository methods (`household_id`, `child_id`), clear component/handler names. No invented abbreviations.
- **Comment the why, not the what.** Only where the reason isn't obvious from the code.
- **Small, reviewable diffs.** Preserve existing behavior unless the change is the point.
