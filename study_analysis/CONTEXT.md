# Coursework analysis context

## Job

Turn one assignment plus explicitly indexed evidence into validated, globally
named concept notes and compact LifeOS learning signals.

## Exact inputs

- `AnalysisRequest.assignment_path`
- The assignment's first non-empty `## Assignment Details` section
- Frontmatter `analysis_sources`, including recorded paths, pages, and hashes
- Optional frontmatter `analysis_contract`; selects the contract-backed Study path
- Optional reviewed public topics from `analysis_contract.required_concepts` or
  explicit frontmatter `research_topics`
- The selected `ContextCompiler` adapter and its hard character budget

## Stable references

- Domain/output contract: `schema.py`
- Assignment acceptance contract: `contract.py`
- Provider seam and usage telemetry: `providers.py`
- Evidence selection seam: `context.py`
- Independent read-only web discovery: `research.py`
- Guarded conflict-checked managed writes: `vault.py`, `transaction.py`

## Process

`snapshot assignment + indexed sources → validate hashes → compile immutable
evidence → optional
reviewed-topic research → generate without research snippets → validate domain,
citations, canonical identities, signals, and resource locators → guarded commit
→ append content-free model + research telemetry`

Assignment, source-record, and source-file snapshots are rechecked before an
external model call and again before commit. Context compilers read the validated
in-memory source snapshots rather than reopening mutable evidence paths.

`SelectiveContext` is the internal default for Study runs with an explicit valid
contract. `PageScopedContext` remains the fallback for uncontracted Study runs and
all Expert runs. Benchmarks and tests may still inject either compiler directly;
there is no permanent CLI strategy switch.

`prep` and `prep-open` are thin Study-mode callers of `AnalysisEngine`; they do
not create a second analysis interface. Optional research runs before generation,
but only its normalized HTTPS hits reach the vault as deterministic
`Helpful Links`. Hit snippets never enter the model prompt or vault.

## Outputs

- Managed assignment analysis fields and `## Concept Analysis`
- Canonical `Knowledge/Concepts/*.md` notes
- Deterministic `Helpful Links` from normalized HTTPS research hits, labeled as
  discovered rather than independently verified
- Expert-only private solution archive when explicitly requested
- One compact `analysis_runs.log` record for success or failure, with model and
  research usage recorded separately

## Exclusions

- No model call during Canvas sync, source indexing, or LifeOS export
- No unindexed source or omitted page may be represented as analyzed
- Every concept must cite at least one exact supplied PDF page or text section
- No ICM routing file is injected into the coursework prompt
- No search snippet enters the model prompt or vault
- No model-generated URL is accepted; web links come only from normalized
  research hits built from reviewed public topics
- No benchmark may write canonical vault state

## Human check

Expert rollout remains deferred. Before extending selective retrieval to
uncontracted or Expert runs, review blinded benchmark outputs for topic
completeness, source support, citation
resolution, canonical identity, and mode leakage.
