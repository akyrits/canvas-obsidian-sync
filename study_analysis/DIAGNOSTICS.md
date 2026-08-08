# Concept diagnostic context

## Job

Turn one explicit, voice-friendly concept check into concise, confirmed,
evidence-backed familiarity without storing raw answers or invoking a model.

## Deep module interface

- Preferred: `DiagnosticEngine.diagnose(request, conversation) -> DiagnosticOutcome`
- Advanced adapters: `prepare(request)` then `record(submission)`
- Corrections: `correct(correction) -> DiagnosticCorrectionOutcome`

The conversation adapter owns the transient learner answer. It returns only a
typed, bounded assessment and must obtain explicit user confirmation before the
engine can persist it. `ScriptedDiagnosticConversation` supports deterministic
tests and pre-reviewed assessments; the CLI supplies a terminal/dictation
adapter.

## Familiarity ladder

`unknown -> recognizes -> explains -> applies -> transfers`

Promotion requires contiguous demonstrated evidence at confidence `>= 0.60`.
Each rung requires its matching evidence kind: own definition, causal
explanation, novel application, or transfer/correction. Missing, partial, or
low-confidence lower rungs prevent higher-rung promotion. Weak new evidence
never silently lowers stronger evidence.

## Invariants

- One existing canonical concept per explicit user invocation.
- Plans are deterministic, token-free, read-only, and limited to 1-3 prompts.
- The semantic revision includes Definition, Why This Matters, Connections, and
  Examples; it excludes Personal Notes, resources, provenance, timestamps, and
  difficulty calibration.
- Record accepts a compact plan ID, concept identity, ordered typed observations,
  and explicit user confirmation; prompts and internal hashes are reconstructed.
- The trusted adapter must assess the current learner answer. Typed evidence and
  common grade/completion-only text guards prevent those signals from being used
  accidentally; `record()` remains a privileged Python interface.
- Evidence and corrections are normalized to one line, bounded, and rendered as
  escaped plaintext. Raw-answer/transcript fields are not accepted.
- A vault-scoped process lock plus expected-original comparison prevents
  cooperating simultaneous writers from dropping a reference or overwriting
  Personal Notes. Non-cooperating editor races are detected on a best-effort
  basis rather than claimed as an operating-system CAS.
- Record and amendment paths are resolved beneath `Knowledge/Diagnostics` and
  cannot traverse links, junctions, or unsafe directory segments.
- Record identity is deterministic; exact replay creates no duplicate.
- Familiarity projection plus immutable record use one guarded rollback
  transaction. Process/power-failure recovery is not a filesystem-wide atomic
  guarantee.
- Corrections create immutable linked amendments, leave the original record
  untouched, and start a new review epoch. Only post-correction evidence counts,
  and it can accumulate across multiple 1-3 question sessions.

## Vault outputs

- Immutable records and amendments under `Knowledge/Diagnostics/<concept>/`.
- Managed concept frontmatter contains familiarity, confidence, assessment time,
  semantic basis hash, record/amendment links, and review state.
- `export_concept_signals()` exposes only strict scalar projections and
  reassessment status to LifeOS.
- Content-free telemetry records success/failure, hashed concept identity, zero
  token usage, and changed-file count; it never logs questions or evidence.

## CLI adapters

- `diagnose-concept <concept>` runs one human-confirmed terminal/dictation flow.
- `diagnostic-plan <concept>` prints a read-only plan plus compact submission
  template for advanced agent callers.
- `record-diagnostic <submission.json>` is an advanced adapter that requires
  out-of-band interactive confirmation; confirmation cannot come from the JSON.
- `correct-diagnostic <concept> <record-id>` appends a confirmed amendment.
- `export-concepts` prints compact LifeOS familiarity signals.

## Validated speech transport

- Portable Handy with a local Parakeet TDT 0.6B model supplies push-to-talk
  dictation to the terminal adapter without invoking a hosted model.
- A real canary passed exact normalized transcription and retention checks. The
  CLI discards the transient answer; Handy is configured to retain neither
  transcription-history rows nor recording files.
- Handy and its model are local environment dependencies, not repository or vault
  artifacts. Native in-process microphone capture remains optional.

## Deferred

- Automatic reassessment scheduling.
- Expert rollout; it remains deferred until all non-Expert work is complete.
