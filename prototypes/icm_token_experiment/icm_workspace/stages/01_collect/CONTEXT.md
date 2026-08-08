# 01_collect — verify selected evidence

One job: turn the assignment's explicit source manifest into a hash-verified,
page-scoped evidence packet without invoking a model.

## Inputs

- Working: the assignment note's `analysis_sources` list.
- Working: the exact source records and their `relevant_pages`.

Do NOT load the rest of the course, the whole vault, or prior model outputs.

## Process

1. Verify every source path and hash.
2. Extract only selected pages.
3. Preserve source title and page labels.

## Outputs

- An in-memory evidence packet for `02_analyze`.

## Human check

Confirm the selected sources and pages cover the assignment prompt.

