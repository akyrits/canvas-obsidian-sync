# Assignment 6 contract-aware production canary

Date: 2026-07-26  
Model policy: `claude-sonnet-5`, adaptive thinking, medium effort, 8,000 output cap  
Context: `selective-v1`, 11,954 evidence characters, 10 selected locators  
Safety: one explicitly authorized Study-mode provider attempt; no automatic retry

## First measured attempt (rejected)

- Input: **7,677 tokens**
- Output: **4,371 tokens**
- Thinking: **0 tokens**
- Estimated cost at the experiment's `$2/$10` per-million rates: **$0.059064**
- Duration: **41.722 seconds**
- Provider completion: successful structured `end_turn`

## Vault acceptance result

The provider response passed schema and exact-citation validation, but the
post-run vault check failed. Five verbose labels became semantic duplicate notes,
tree reconstruction was not retained as a distinct concept, and assignment effort
was undercalled as `medium`. The candidate therefore did **not** pass production
acceptance and selective retrieval was not promoted.

The five created notes were removed and Assignment 6 plus all six original
canonical concept notes were restored from the pre-run snapshot. All seven SHA-256
hashes matched the checkpoint after rollback. The rejected post-run state remains
available only in the private local experiment checkpoint.

## Root cause and remediation

Retrieval contained reconstruction evidence; the failure was in the control plane.
The production prompt did not carry a trusted assignment concept contract, exact
alias resolution happened too late, and the benchmark's topic/signal constraints
were not enforced before commit.

The corrected path now:

1. requires an explicit version-1 `analysis_contract` for a selective canary;
2. verifies every required canonical note exists before provider invocation;
3. places the exact canonical concept and signal contract in the prompt;
4. rejects missing, extra, renamed, merged, or split concepts before commit;
5. rejects multiple outputs resolving to one canonical note;
6. requires canonical relationship targets; and
7. records only the contract hash and concept count in content-free telemetry.

Assignment 6's contract contains the six reviewed canonical concepts, difficulty
`4-5`, and effort `large|very_large`. Its contract hash is
`99ca851f1f6043f9e59eb7b1948e85d302cc3ae1f3c1b2691abd1f35d80d7578`.

A zero-network replay of the user's preferred blind response through the corrected
deep interface updated exactly the six existing concept notes and Assignment 6,
created no new notes, and produced the same canonical result on a second pass.

## Corrected measured attempt (accepted)

The separately authorized corrected canary used the same model policy, selected
evidence bytes, and one-attempt/no-retry rule.

- Input: **7,887 tokens**
- Output: **4,365 tokens**
- Thinking: **23 tokens**
- Estimated cost at `$2/$10` per million: **$0.059424**
- Duration: **40.867 seconds**
- Provider completion: successful structured `end_turn`

The contract added 210 input tokens while output fell by 6 tokens, increasing
measured cost by only `$0.000360` (about 0.61%) versus the rejected attempt.

Post-run acceptance passed. The engine updated Assignment 6 and exactly the six
existing canonical concept notes, created no duplicate or solution file, retained
tree reconstruction distinctly, and recorded difficulty `5`, effort `very_large`,
and mode `study`. Every generated citation resolved to selected evidence, every
relationship used a canonical target, user-owned note state was preserved, and
all three source records and source-file hashes were unchanged. The pre-run and
post-run states remain in the private reversible checkpoint.

## Promotion decision

The full local suite passes **32 tests**. `SelectiveContext` is promoted as the
internal default for explicit-contract Study runs. `PageScopedContext` remains the
fallback for uncontracted Study and all Expert runs because those surfaces were
not covered by this canary. The temporary `--selective-canary` CLI flag and pinned
canary policy were removed; explicit compiler injection remains available only as
an internal benchmark/test seam.
