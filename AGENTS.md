# Canvas–Obsidian Sync Agent Guide

This repository syncs Canvas into a private Obsidian vault and derives durable
study concepts without giving the model control of routine sync or scheduling.

## Route work

- Product contract and current handoff: `README.md`
- Deterministic Canvas ingestion: `sync.py`, `canvas_api.py`, `canvas_ics.py`,
  `vault_notes.py`
- Coursework analysis contract: `study_analysis/CONTEXT.md`
- Concept diagnostic contract: `study_analysis/DIAGNOSTICS.md`
- General knowledge repository contract: `study_analysis/KNOWLEDGE.md`
- Analysis benchmark: `prototypes/icm_token_experiment/README.md`
- LifeOS has its own instructions under `../lifeos/AGENTS.md`

## Invariants

- `AnalysisEngine.analyze(AnalysisRequest)` is the sole production analysis
  interface; keep context strategy and provider details behind it.
- Routine sync, source indexing, knowledge capture/refresh, vault reads, and LifeOS
  export are token-free.
- Web discovery is separately opt-in, read-only, and model-free. Cache only
  normalized public result metadata outside the vault/repository; never raw
  queries, provider payloads, headers, credentials, or assignment text.
- Validate model output before a guarded rollback-capable vault commit.
- Preserve user-authored note sections and keep Expert solutions private.
- Course archival is metadata-only: keep course folders in place, require all
  Canvas tasks complete plus a clean link audit and explicit confirmation, and
  change only the course `_Course Info.md` lifecycle field.
- Never commit `.env`, PDFs, raw prompts/responses, or local usage logs.
- Do not move canonical `School`, `Sources`, `Knowledge/Concepts`, or `Solutions`
  paths without an approved migration and link-integrity walk test.
- Do not feed search snippets into coursework analysis or accept generated URLs
  until they can be checked against the exact validated research bundle.

## Verify

Run `python -m unittest discover -s tests -v` and `python agent.py
check-vault-links`. Context benchmarks must write outside both the code
repository and the vault and must confirm vault hashes are unchanged.
