# Assignment 6 context experiment — gated follow-up

Date: 2026-07-25  
Model policy: `claude-sonnet-5`, adaptive thinking, medium effort, 8,000 output cap  
Safety: paired calls wrote outside the vault; before/after canonical hashes matched

## Implementation under test

- Production still defaults to `PageScopedContext` behind
  `AnalysisEngine.analyze(AnalysisRequest)`.
- `SelectiveContext` is an injected benchmark candidate, not a CLI choice.
- Anthropic receives the structured schema once through transport; the textual
  prompt no longer duplicates it.
- The selector uses local BM25-style ranking, source diversity, adjacent-page
  continuity, and an exercise-page penalty under a 12,000-character target.
- Both benchmark arms receive the same six-topic coverage contract and the same
  generation policy.

## Final paired result

| Metric | Full evidence | Retrieval 12k | Retrieval change |
| --- | ---: | ---: | ---: |
| Input tokens | 19,027 | 7,695 | -59.56% |
| Output tokens | 4,998 | 3,936 | -21.25% |
| Total tokens | 24,025 | 11,631 | -51.59% |
| Estimated cost | $0.088034 | $0.054750 | -37.81% |
| Latency | 45.553 s | 34.463 s | -24.34% |
| Thinking tokens | 0 | 0 | — |
| Automatic gates | failed | passed | candidate advances |

Retrieval passed all automatic gates:

1. Exactly six bounded concepts.
2. Distinct coverage of terminology, depth/height, proper-tree bounds,
   traversal, expression trees, and traversal reconstruction.
3. All 11 citations resolved to a supplied source and supplied page; every
   concept had resolved evidence.
4. No completed Assignment 6 solution or Expert-mode payload.
5. Assignment difficulty 4 and effort `large`.

The full-evidence arm covered all topics but abbreviated five citation titles
instead of copying their indexed names exactly and rated effort `medium`.

## Human-support review

The retrieval response teaches methods and uses analogous examples. It does not
draw either assigned tree, produce the requested traversal result, or complete
the assigned proofs. Mentioning the inequalities already printed in the
assignment is not a completed proof. Assistant review found no critical factual
error, unsupported supplied-page citation, or direct-answer leakage. User blind
review remains the final human gate.

## Cost accounting

- Diagnostic pair before the clarified coverage contract: $0.140518.
- Final paired run: $0.142784.
- New paid benchmark cost in this phase: **$0.283302**.
- Combined with the first three-arm experiment: approximately **$0.485152**,
  excluding the earlier pre-benchmark Assignment 6 attempts.

## Decision

Selective retrieval advances to a multi-assignment canary. It is not enabled in
production because Assignment 6 is currently the only assignment with indexed
source records. The next adoption gate is two assignments with different task
and evidence shapes, followed by user blind review. No full ICM vault migration
is indicated; the lightweight routing overlay and internal context seam are the
useful pieces.
