# PROTOTYPE — ICM token experiment

Question: does an ICM workspace reduce total token use for the Assignment 6
concept-analysis workflow without weakening its validated Study-mode result?

This is throwaway code. It never writes the Obsidian vault. It compares:

1. `current` — the production prompt and all indexed page-scoped sources.
2. `icm-structure-only` — the same prompt plus ICM routing files, isolating folder overhead.
3. `icm-contract` — a concise ICM stage contract plus the same source material.
4. `selective-retrieval` — the production contract with deterministic page selection.

Run the interactive state viewer:

```powershell
.venv\Scripts\python.exe prototypes\icm_token_experiment\run.py
```

Print a non-interactive report:

```powershell
.venv\Scripts\python.exe prototypes\icm_token_experiment\run.py --report
```

Add `--count-api` for exact, free Sonnet 5 input-token counts. `--live` makes
paid, non-writing comparison calls and must be explicitly authorized.

The first measured run and its decision are recorded in
[`results-2026-07-25.md`](results-2026-07-25.md).
The gated adaptive/medium follow-up is recorded in
[`results-2026-07-25-phase2.md`](results-2026-07-25-phase2.md).
The two-assignment mixed-source canary is recorded in
[`results-2026-07-25-phase3.md`](results-2026-07-25-phase3.md).

## Gated paired benchmark

`benchmark.py` compares only the current full-evidence behavior with the
selective candidate. It uses the same model, thinking, effort, schema, and output
ceiling for both arms; saves responses and gate results outside the vault and
code repository; and verifies canonical vault hashes did not change.

Build a free local plan:

```powershell
.venv\Scripts\python.exe -m prototypes.icm_token_experiment.benchmark plan `
  --assignment "C:\path\to\vault\School\course\Assignment 6.md" `
  --output "C:\private\benchmark-runs\assignment6"
```

Add `--count-api` for Anthropic's free exact input-token count. Paid execution
requires both `compare --execute-paid` and an explicit worst-case ceiling:

```powershell
.venv\Scripts\python.exe -m prototypes.icm_token_experiment.benchmark compare `
  --assignment "C:\path\to\vault\School\course\Assignment 6.md" `
  --output "C:\private\benchmark-runs\assignment6-live" `
  --pairs 1 --execute-paid --max-cost-usd 0.22
```

Passing Assignment 6 advances the candidate to other assignment shapes; it
does not enable retrieval in production. Rescore saved responses without an API
call using `benchmark replay --run-dir <path>`.

## Isolated assignment fixtures

`fixture.py` clones an assignment and re-indexes existing source records or
direct evidence into a temporary mini-vault. It records origin hashes and refuses
to write inside the live vault or repository. This lets a benchmark exercise the
real source loader without adding `analysis_sources` to a canonical assignment.

```powershell
python -m prototypes.icm_token_experiment.fixture prepare `
  --assignment "C:\path\to\live-vault\School\course\Assignment.md" `
  --output "C:\private\benchmark-fixtures\case" `
  --source-record "C:\path\to\live-vault\School\course\Sources\Source.md"

python -m prototypes.icm_token_experiment.fixture verify `
  --fixture "C:\private\benchmark-fixtures\case"
```

Long Markdown/text evidence is split into stable numbered sections for selective
retrieval. PDF evidence retains its original page locators. A benchmark response
is persisted before domain parsing, so an invalid model result still remains
available for private diagnosis on future runs.
