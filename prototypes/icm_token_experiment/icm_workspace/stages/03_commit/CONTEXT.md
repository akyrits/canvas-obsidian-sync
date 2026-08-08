# 03_commit — update canonical notes

One job: validate and atomically apply an approved analysis result.

## Inputs

- Working: validated output from `02_analyze`.
- Reference: vault preservation and Study/Expert invariants in production code.

Do NOT invoke a model or alter user-authored note sections.

## Process

1. Validate domain constraints.
2. Plan all assignment and concept-note changes.
3. Atomically replace planned files or roll back.

## Outputs

- Canonical assignment and concept notes.

## Human check

Review the vault diff. This prototype never executes this stage.

