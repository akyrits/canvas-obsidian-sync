# 02_analyze — produce concept analysis

One job: transform the assignment and verified evidence packet into validated
Study-mode concept JSON.

## Inputs

- Working: Assignment Details from the selected assignment note.
- Working: the evidence packet produced by `01_collect`.
- Reference: `../../_shared/study-policy.md`.
- Reference: the transport-enforced JSON output schema.

Do NOT load the whole vault, unrelated course notes, prior solutions, grades,
feedback, or other stages' references.

## Process

1. Identify the four to six most durable concepts required by the assignment.
2. Ground explanations, relationships, examples, and difficulty in evidence.
3. Return only the schema-conforming JSON object.

## Outputs

- Validated analysis JSON, held in memory during this prototype.

## Human check

Verify concept coverage and page-level citations before any canonical write.

