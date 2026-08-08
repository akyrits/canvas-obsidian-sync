---
name: canvas-study-analysis
description: Explicit-only router for Canvas-Obsidian coursework analysis. Invoke for evidence checks, analysis or replay, context benchmarks, usage, or canary decisions. Not for sync, vault editing, scheduling, or coursework submission.
---

# Canvas Study Analysis

Treat repository `AGENTS.md` invariants as active. Never put this skill or
routing text in a coursework prompt.

1. Route production through `AnalysisEngine.analyze(AnalysisRequest)` or
   `python agent.py analyze-concepts "<assignment>" --mode study`. Route
   missing/stale evidence to `index-source`. Use only hashed/scoped
   `analysis_sources`; never crawl the vault.
2. Require nonempty Assignment Details. Study is default and teaches without
   solving. Expert needs explicit per-assignment approval and stays private.
3. Read `study_analysis/CONTEXT.md` only for code/contract changes or
   ambiguity; `README.md` for CLI/status; the prototype README for
   benchmark commands.
4. Before paid work, disclose model/effort, limits, pairs, maximum USD, output
   path, and writes; require explicit approval and cap. Never read `.env`.
5. After changes, run the `AGENTS.md` verification. Report paths, mode/strategy,
   tokens, cost, latency, gates, hashes, and writes; state when no paid call or
   vault write occurred.
