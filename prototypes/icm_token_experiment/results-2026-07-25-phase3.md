# Multi-assignment selective-context canary

Date: 2026-07-25  
Model policy: `claude-sonnet-5`, adaptive thinking, medium effort, 8,000 output cap  
Safety: all paid calls used isolated mini-vaults; canonical assignment,
source-record, and evidence hashes remained unchanged

## Cases

1. **Bonus Binary Tree Creation** — a short discussion/diagram task using the
   existing M11A, M11B, and Goodrich tree sources.
2. **Unit 9: Animating a Progress Bar** — a small implementation activity using a
   mixed evidence packet: CodePath Unit 6 pages 9-11, CodePath Unit 8 pages 10-12,
   and frozen MDN `setInterval`/`clearInterval` Markdown.

The Unit 9 packet deliberately excludes generated `How to Approach` and `Key
Concepts` prose from the assignment note. The assignment's first `Assignment
Details` section is the query/contract, not teaching evidence.

## Successful-output comparison

| Case | Metric | Full evidence | Retrieval 12k | Retrieval change |
| --- | --- | ---: | ---: | ---: |
| Assignment 6 | Input tokens | 19,027 | 7,695 | -59.56% |
|  | Total tokens | 24,025 | 11,631 | -51.59% |
|  | Cost | $0.088034 | $0.054750 | -37.81% |
| Bonus tree | Input tokens | 18,760 | 7,491 | -60.07% |
|  | Total tokens | 21,632 | 10,054 | -53.52% |
|  | Cost | $0.066240 | $0.040612 | -38.69% |
| Progress bar | Input tokens | 11,513 | 7,312 | -36.49% |
|  | Total tokens | 14,756 | 10,265 | -30.44% |
|  | Cost | $0.055456 | $0.044154 | -20.38% |

All six successful calls reported zero thinking tokens. That supports keeping the
current adaptive/medium policy while optimizing evidence selection; this sample
does not justify lowering effort as a separate production change.

The bonus retrieval value above is the bounded diagnostic retry. Its first paid
attempt ended normally (`end_turn`) and consumed 7,491 input plus 2,979 output
tokens, but failed domain validation. The old harness had not yet persisted model
payloads before parsing, so its exact domain error cannot be recovered. The retry
was intentionally retrieval-only and passed every automatic gate. This is recorded
as a reliability warning, not discarded as an outlier.

## Quality result

- **Bonus retrieval retry:** 4/4 distinct topics, 5/5 exact citations, calibrated
  difficulty/effort, bounded output, and no completed tree or discussion post.
- **Progress-bar retrieval:** 4/4 distinct topics, 7/7 exact citations, calibrated
  difficulty/effort, bounded output, and no completed `script.js` implementation.
- **Progress-bar full evidence:** produced a fifth lower-value concept and left it
  uncited, so it failed the compact concept and grounding gates.
- **Bonus full evidence:** covered all required topics and was Study-safe, but
  abbreviated three Goodrich source titles instead of copying the exact indexed
  title, so it failed the strict citation gate.

An independent blind assistant review preferred retrieval for both cases and
found no critical factual or Study-mode safety issue. Tree retrieval won on
explicit level/depth coverage and correct qualification of proper-tree formulas.
Progress retrieval won on concision and avoiding a duplicative fifth concept.
The user's blind preference remains the final human gate.

The rubric aliases were recalibrated once using zero-API replay: phrases such as
"ID returned by setInterval" and "named access" are semantically equivalent to
the original aliases. Frozen original case files and override hashes remain in the
private run artifacts.

## Implementation lessons

- A full ICM vault migration is still not indicated. The useful pattern is a
  lightweight routing guide, compact per-case coverage contract, hashed source
  records, and deterministic retrieval behind `AnalysisEngine`.
- Non-PDF evidence must be chunked by stable text sections. Treating a long MDN
  page as one chunk would omit the core timer contract under a 12k budget.
- Reference appendices can repeat API names and outrank definitions. Query-aware
  penalties plus preservation of introductory text corrected that failure before
  paid generation.
- Citation resolution belongs inside the deep analysis module, not only in the
  benchmark. Production now rejects omitted or fabricated locators before commit.

## Cost accounting

- Two initial Phase 3 pairs: **$0.210622**.
- Retrieval-only bonus diagnostic: **$0.040612**.
- Phase 3 paid total: **$0.251234**.
- Cumulative measured experiment total through Phase 3: approximately
  **$0.736386**, excluding pre-benchmark Assignment 6 failures.

## Decision

Selective retrieval is eligible for a controlled production canary, but it is not
the default yet. The remaining gates are the user's blind preference review and
one real assignment run with the new pre-commit citation validator. If those pass,
replace the default `PageScopedContext` internally; do not add a permanent CLI
strategy switch.
