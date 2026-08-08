# General knowledge repository contract

## Job

Provide a stable, vault-wide home for captured material, durable references,
canonical concepts, and human-curated navigation without spending tokens or
depending on a network service.

## Public commands

```powershell
python agent.py refresh-knowledge --pretty
python agent.py capture-knowledge "Idea title"
python agent.py capture-knowledge "Idea title" --text "A thought to organize later."
python agent.py capture-knowledge "Reading notes" --file "C:\path\notes.txt"
```

The refresh is local, deterministic, idempotent, and transactional. It invokes
no model or network service. A repeated run against unchanged content converges
on the same repository state.

`capture-knowledge` is also local and invokes no model or network service. With
no input option it prompts once for a line of interactive dictation. `--text`
accepts explicit text, and `--file` reads a local file.

For routine private capture, run the command directly in PowerShell and use the
local Handy shortcut at the prompt. This avoids a Codex/model turn. Prefer the
interactive prompt or `--file` for sensitive text because an inline `--text`
value may remain in shell history or process arguments.

## Layout

- `Knowledge/Knowledge Hub.md` — top-level entry point
- `Knowledge/Inbox` — capture area for material not yet organized
- `Knowledge/Concepts` — canonical cross-course concept notes
- `Knowledge/Sources` — durable global reference notes
- `Knowledge/Maps` — topic and relationship navigation

`Knowledge/Sources` is not the coursework evidence index. Course source
artifacts remain course-scoped, hash-checked, and page- or section-bounded for
assignment analysis. A global source note does not replace that evidence
boundary.

## Inbox capture

A successful capture creates a note below `Knowledge/Inbox` and refreshes the
affected navigation indexes in the same guarded transaction.

- An exact title/content retry is idempotent and does not create a duplicate,
  even after a rename or move between managed knowledge areas when the stable
  capture identity metadata is preserved.
- The same title with different content creates a distinct capture.
- After commit, the capture is user-owned and neither refresh nor retry may
  overwrite it.
- Unsafe titles, paths, input files, or content fail before any vault write.
- A failed or conflicting capture leaves both the Inbox and navigation indexes
  at their prior state.

Capture is intentionally separate from curation. Moving a note from `Inbox`
into `Concepts`, `Sources`, or `Maps` remains a later deliberate promotion
workflow.

## Ownership and safety

- Refresh writes only `Knowledge Hub.md`, `_Inbox.md`, `_Sources.md`, and
  `_Maps.md` through the shared guarded transaction.
- Concept, capture, source, map, and diagnostic notes are read-only inputs and
  remain byte-identical.
- In the four managed navigation notes, unknown metadata values and user-owned
  sections are retained. `Personal Navigation` and each landing page's `Notes`
  section are user-owned. YAML comments/style and line-ending style in those
  four managed files may be normalized when their indexes change.
- Unsafe Obsidian link delimiters, redirected paths, duplicate canonical
  concepts, and concurrent inventory changes fail before a stale index can
  survive.
- A failed or conflicting refresh must leave the prior vault state intact.
- Refresh records no model tokens or network requests because it attempts none.

## Acceptance state

The live-vault acceptance indexed six concepts and created exactly the four
managed navigation notes. SHA-256 comparison confirmed that all eight existing
Knowledge files were unchanged. An immediate second refresh changed zero files,
and the resulting full-vault audit resolved all 112 references. Both refreshes
attempted no model or network request, used zero input/output tokens, and had
configured cost `$0.00`.

The accepted live capture then added `Integrated Context Management (ICM)` to
Inbox. Only the four managed navigation files changed; every existing
non-managed Knowledge note remained byte-identical. The exact retry and the
following refresh each changed zero files, and the full-vault audit resolved all
114 references. Capture, retry, and refresh attempted no model or network
request and reported zero tokens at configured cost `$0.00`.

This acceptance record covers repository refresh and Inbox capture. It does not
claim an automatic Inbox-promotion workflow.
