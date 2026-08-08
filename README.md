# Canvas → Obsidian Study System

A daily-syncing bridge between Canvas LMS and Obsidian, plus a provider-neutral
academic knowledge agent layered on top. It preserves what was learned in each
course, acts as a study buddy and teaching aid, and exports learning signals to
LifeOS without taking control of the calendar. It is also a portfolio project
for exploring what an LLM agent can do against a real personal dataset rather
than a toy demo.

This repo is the code only. The actual Obsidian vault (notes, PDFs,
coursework) lives in a separate private repository, since it contains real
academic content.

## What it does

**Sync** (`sync.py`) — pulls assignment due dates from a Canvas calendar feed
and creates/updates one Obsidian note per assignment, with a matching card on
a Kanban board. Runs on a schedule (Windows Task Scheduler, every 24h) with no
manual intervention.

**Study agent** (`agent.py`) — a CLI on top of the synced vault that:

- generates a validated step-by-step outline and key-concepts explanation from
  indexed course evidence, with optional local web discovery from reviewed
  public concept topics
- answers ad-hoc questions about synced tasks ("what's due this week in my CS
  classes?") through Claude's tool-calling loop
- fetches YouTube lecture transcripts and saves them as notes
- scaffolds a dedicated study note for a professor-provided PDF lecture, ready
  for PDF++ annotation
- scaffolds a study note per Canvas module for courses that don't publish a
  clean lecture-deck PDF - any mix of readings (or none yet, if the module
  hasn't opened), addable incrementally as material unlocks
- explicitly analyzes selected material into global concept notes, with
  difficulty, evidence, relationships, sources, and a four-lens explanation of
  why each concept matters
- exports compact assignment and learning signals for LifeOS to read; it never
  creates or changes calendar blocks itself

Both pieces treat the vault as the single source of truth: automation only
ever adds information or refreshes fields it owns — it never overwrites a
note body, a status you changed, or a card you dragged.

## Why it exists

Canvas exposes an ICS calendar feed with no login required, but this school's
Canvas instance has personal access token generation disabled at the admin
level, so there's no way to pull assignment descriptions or attached files
through the full REST API. Rather than stall on that, the agent leans into
the constraint: it works from what a student can plausibly gather by hand
(the title/due date from the feed, a one-time textbook/topics blurb per
course, PDFs saved off Canvas manually) plus bounded local discovery, instead of
assuming perfect scraped context. That's closer to how an agent actually has
to behave in most permission-constrained real-world settings anyway.

## Product contract and roadmap

This README is the project plan as well as the operating guide. The system has
three durable knowledge layers:

1. **Source layer** — assignments, course metadata, lecture files, transcripts,
   links, and page-scoped source records.
2. **Personal knowledge layer** — the student's notes, corrections, examples,
   reflections, and diagnostic evidence.
3. **Concept intelligence layer** — global concept notes that explain what a
   concept means, why it matters, what it connects to, where it is used, and
   what prerequisites or downstream ideas surround it.

Course folders preserve historical semester context. Global concept notes live
outside individual courses so a concept such as recursion accumulates evidence
and connections across the entire degree. Connections use a controlled
vocabulary: `prerequisite_of`, `builds_on`, `applies_to`, `contrasts_with`,
`example_of`, `used_in`, and `related_to`.

The vault-wide knowledge repository has stable homes at
`Knowledge/Knowledge Hub.md`, `Knowledge/Inbox`, `Knowledge/Concepts`,
`Knowledge/Sources`, and `Knowledge/Maps`. Its global `Sources` notes are durable
references; they are distinct from the course-scoped source artifacts used as
hashed assignment evidence.

### Operating rules

- Routine Canvas sync is deterministic and spends zero LLM tokens.
- `refresh-knowledge` is local, deterministic, idempotent, transactional, and
  makes no model or network call.
- `capture-knowledge` is also fully local. Exact retries are idempotent, and a
  capture plus its navigation-index updates commit as one transaction.
- AI runs only after the user explicitly invokes `analyze-concepts`, `prep`, or
  `prep-open`. That action is the approval checkpoint; validated output is saved
  automatically.
- **Study mode** teaches with explanations, analogous examples, an approach,
  and optional diagnostics without completing assigned problems.
- **Expert mode** creates a complete private worked solution in a linked
  Solution Archive note. It is explicitly invoked per assignment, includes
  reasoning and sources, and never submits anything to Canvas.
- Familiarity is evidence-based: `unknown`, `recognizes`, `explains`, `applies`,
  or `transfers`. Missing notes mean unknown, not weak. Diagnostics are optional
  and user-initiated; assignment completion does not change familiarity.
- Concept difficulty uses a sourced 1-5 score. Assignment effort, familiarity,
  and confidence remain separate signals; LifeOS decides priority and schedule.
- Grades and instructor feedback are never ingested or used as evidence.
- Markdown/YAML and standard links are canonical. Embeddings and indexes are
  optional, disposable, and rebuildable.
- User-authored sections are preserved. AI-managed updates retain their date
  and sources, validate before commit, and use reversible file history.
- Voice-first use is a core accessibility requirement. Commands are designed as
  agent-callable operations, local dictation is validated, and native in-process
  microphone capture remains optional.

### Delivery phases

- **Phase 1 — reliable ingestion (complete):** idempotent Canvas sync, assignment
  notes, Kanban/TaskNotes views, course/material scaffolding.
- **Phase 2 — concept pilot (complete):** provider-neutral analysis interface,
  source indexing, global concepts, Study/Expert modes, validation, guarded vault
  writes, token budgets, per-attempt telemetry, context compilation, and LifeOS
  export. COP3410C Assignment 6 is the pilot.
- **Phase 3 — diagnostics and accessibility (complete):** voice-first concept
  diagnostics, concise assessment records, protected conversational corrections,
  and stale evidence reassessment.
- **Phase 4 — scale and retrieval (current):** deterministic broken-link checks,
  metadata-only course archival, local SearXNG, the model-free research seam,
  migration of `prep` behind `AnalysisEngine`, and the general-knowledge
  repository interface are complete; optional embeddings remain. The knowledge
  repository contract has not yet received a live-vault acceptance run.

Phase 2 is accepted when one real module can be analyzed in one action; concepts
are sourced, deduplicated, linked, and difficulty-rated; unknown familiarity
remains explicit; LifeOS can read compact signals; routine sync remains
token-free; the configured analysis budget is enforced; and personal notes
survive every update. Running and recording a diagnostic belongs to Phase 3.

### Current collaborator handoff (2026-07-26)

Phase 2 and its accepted real-vault pilot are implemented. The token-free Phase 3
diagnostic and accessibility slice is implemented, hardened, and accepted with a
real local-dictation canary. Phase 4 is current; its token-free link audit,
metadata-only course archival, provider-neutral research seam, localhost-only
SearXNG deployment, and `prep` migration are implemented and accepted. Live
research remains explicitly opt-in. The token-free general-knowledge repository
interface is implemented, but no live-vault canary is claimed yet. The combined
handoff is not committed. Both repositories are on `main`: code is
based on `4935fe5`, and the vault is based on `e611655`.

**Implemented:**

- `study_analysis.AnalysisEngine.analyze` is the deep module interface. It owns
  source collection, optional reviewed-topic research, input budgeting, model
  invocation, schema validation, and guarded rollback-capable vault commit.
- `providers.py` has Anthropic, OpenAI-compatible/local, and saved-JSON adapters.
  Provider imports are lazy, so deterministic and local operations do not load
  or require Anthropic.
- `schema.py` validates modes, 1-5 difficulty, effort, confidence, controlled
  relationships, resources, canonical concept identity, and Study/Expert output
  rules before any write.
- `sources.py` creates hashed, page-scoped source records, confines record paths
  to the course `Sources` folder, requires exact content hashes, and rejects
  missing, changed, unsupported, or linked source files before analysis. Each run
  compiles from immutable validated source bytes and rechecks the assignment,
  source records, and evidence before external model use and before commit.
- `context.py` provides model-free page-scoped and selective-retrieval adapters.
  Contract-backed Study runs now select retrieval internally; uncontracted Study
  and all Expert runs retain the page-scoped fallback. Long text sources are split
  into stable, citable sections instead of one indivisible file.
- `AnalysisEngine` now resolves every generated citation against the exact PDF
  page or text section supplied to that call before any vault commit.
- Assignment and source text are marked as untrusted evidence in the production
  prompt, and the default context compiler now enforces its input budget as a
  hard upper bound.
- `contract.py` loads a compact, trusted assignment contract before any provider
  call. When a contract is present, every reviewed canonical note must already
  exist; generated output must preserve that exact concept set and remain inside
  allowed difficulty and effort signals before the vault can change.
- Every analysis attempt is logged, including preparation, provider,
  truncation, refusal, JSON, domain-validation, and commit failures. Model usage
  and research requests, cost, source, and result counts are reported separately;
  provider metadata includes thinking and cache counters when available.
- Across Assignment 6, a binary-tree discussion, and a mixed PDF/text JavaScript
  activity, successful retrieval outputs reduced input 36.5-60.1% and call cost
  20.4-38.7% while passing the calibrated automatic gates. One initial binary-tree
  retrieval response failed domain validation before a successful bounded retry.
  The user blind review preferred retrieval. A first controlled real-vault canary
  used 7,677 input and 4,371 output tokens (estimated `$0.059064`) but failed the
  post-run acceptance check: it created five semantic duplicates, omitted tree
  reconstruction as a distinct concept, and underestimated effort. The vault was
  restored byte-for-byte. After adding the reviewed contract, one corrected canary
  used 7,887 input and 4,365 output tokens (`$0.059424`) and passed: exact six-note
  reuse, distinct reconstruction coverage, difficulty `5`, effort `very_large`,
  canonical links, Study-only output, and unchanged sources. Selective retrieval
  is therefore the internal default only for contract-backed Study runs. The
  temporary CLI canary switch has been removed.
- `vault.py` preserves user sections, removes duplicate managed headers, updates
  global concept notes, reuses canonical notes through aliases, emits canonical
  assignment links, rejects multiple outputs targeting one canonical note, and
  writes private Expert solutions only in Expert mode.
- `diagnostics.py` exposes a preferred one-call `diagnose` interface plus compact
  `prepare`/`record` adapters for one canonical concept. It creates 1-3
  voice-friendly prompts, requires typed current-answer evidence and explicit
  human confirmation, applies a confidence threshold, projects only contiguous
  evidence. Common grade/completion-only summaries and raw transcript fields are
  rejected, while the low-level record interface remains explicitly privileged.
- Diagnostic corrections are immutable linked amendments. They never rewrite an
  evidence record and mark familiarity for reassessment until the ladder is
  demonstrated again.
- `transaction.py` provides a vault-scoped lock, expected-original conflict
  checks, guarded multi-file replacement, and rollback used by analysis and
  diagnostics. This prevents cooperating simultaneous writers from losing a
  record reference or overwriting a late Personal Notes edit.
- Diagnostic paths are confined below `Knowledge/Diagnostics`; unsafe names,
  symlinks, junctions, stale plans, reordered observations, and unnormalized
  Markdown are rejected before familiarity can change.
- Diagnostic success/failure telemetry is content-free and explicitly reports
  zero model attempts and zero tokens.
- The terminal/dictation adapter explains every assessment choice, accepts full
  words or abbreviations, gives evidence-summary guidance, previews the compact
  record, and states that the raw answer was discarded before confirmation.
- Portable Handy with a local Parakeet TDT 0.6B model is validated as the Phase 3
  speech-capture layer. A 33-word canary matched all 33 normalized words; after
  the real diagnostic, Handy retained zero history rows and zero recording files.
  The app and model remain local environment dependencies rather than repository
  artifacts.
- The real Tree Traversal diagnostic produced two immutable compact records
  across UX calibration and the corrected rerun. The corrected run recorded
  `partial` evidence at `0.50`, so familiarity correctly remained `unknown`.
  Both runs attempted no model and used zero input/output tokens.
- `lifeos.py` exports compact read-only assignment and concept signals while
  excluding sources, grades, Personal Notes, diagnostic evidence/corrections,
  and Solution Archive content.
- `refresh-knowledge` defines the token-free vault-wide repository contract. It
  creates or refreshes the standard knowledge locations transactionally while
  preserving existing notes and the user's `Personal Navigation` section.
- The accepted live refresh indexed six concepts and created exactly four
  navigation notes: the hub plus Inbox, Sources, and Maps landing pages. All
  eight pre-existing Knowledge files remained byte-identical, the immediate
  rerun changed zero files, and the full vault audit resolved 112/112 links.
  Both runs attempted no model or network request and used zero tokens at
  configured cost `$0.00`.
- `capture-knowledge` defines the token-free Inbox capture interface. It accepts
  one interactive line, explicit `--text`, or a local `--file`; exact retries do
  not duplicate notes, while the same title with different content creates a
  distinct capture. A successful capture becomes user-owned and is not rewritten
  by later refreshes.
- The accepted live canary captured `Integrated Context Management (ICM)` in
  Inbox. Only the four managed navigation files changed, all existing
  non-managed Knowledge notes remained byte-identical, and both the exact retry
  and following refresh changed zero files. The resulting audit resolved
  114/114 links with zero model/network attempts, tokens, or configured cost.
- `link_integrity.py` exposes one read-only `audit_vault_links` interface. It
  resolves wikilinks, embeds, aliases, headings/blocks, relative paths, and
  managed analysis/diagnostic references; ignores external links, code examples,
  and HTML-comment placeholders; reports missing, ambiguous, invalid, and unsafe
  references deterministically; and invokes no model.
- Course attachments retain their documented external-storage seam: only a
  direct file inside `School/<course>/Attachments` may resolve through that
  course's junction. Other symbolic-link/junction traversal remains unsafe. The
  missing COP3410C Attachments junction was restored locally to its existing
  OneDrive lecture folder.
- The current accepted live link audit scanned 79 notes, indexed 88 files, and
  resolved all 114 references. It attempted no model and used zero input/output
  tokens. An earlier read-only audit also confirmed an identical before/after
  SHA-256 map across 130 physical vault files.
- `course_archive.py` exposes a guarded `prepare`/`apply` interface that changes
  only `School/<course>/_Course Info.md`. Archival requires every Canvas task in
  the course to be complete, an explicit human confirmation, a clean link audit,
  and an unchanged plan; restore remains available even if new work appears.
  Writes use the shared transaction and roll back if the post-write audit fails.
- Archived courses keep their original folders and links. The managed
  `course_archive` metadata records only `status` and `changed_at`, while the
  compact LifeOS projection adds a `course_archived` boolean without reading or
  exporting course-note content.
- A live archive preview for COP3410C correctly blocked on its two open Canvas
  tasks, moved zero folders, attempted no model, used zero tokens, and left all
  130 physical vault-file hashes unchanged.
- `research.py` exposes one bounded `ResearchEngine.search` interface plus saved
  JSON and SearXNG adapters. It validates and deduplicates HTTPS results, rejects
  private/credentialed URLs, caps response size and result text, records search
  requests separately from model tokens, and caches only normalized public
  metadata outside the repository and vault. The cache is still sensitive because
  result text can echo query terms. Live search remains opt-in by configuration;
  this workstation now has a localhost-only SearXNG instance in Ubuntu WSL with
  a hidden, reversible keeper documented under `ops/local-search`.
- The accepted local-search canary returned three HTTPS results with one provider
  request, configured direct cost `$0`, no model attempt, and zero tokens. The
  identical repeat came from the application cache with zero provider requests.
  A full stop/status/start cycle passed, and the before/after aggregate SHA-256
  of all 129 non-`.git` vault files remained exactly unchanged.
- `prep` and `prep-open` are thin Study-mode callers of `AnalysisEngine`; there
  is no second coursework-analysis or provider-specific search path. Research
  terms come only from reviewed `analysis_contract.required_concepts` or an
  explicit `research_topics` list. `Helpful Links` is rendered deterministically
  from normalized HTTPS hits. Search snippets never enter the model prompt or
  vault, and model-generated web URLs are rejected.
- COP3410C Assignment 6 is the real Study-mode pilot. Its three supplied PDFs
  are indexed with narrow page ranges; six global concept notes were generated
  and safely refreshed by the accepted live canary. No Expert solution exists.
  The initial pilot used the zero-token
  `tests/fixtures/assignment6_study_analysis.json`; both live canaries and the
  first rollback are recorded above.

**Verify from the code repository:**

```powershell
python -m unittest discover -s tests -v
python agent.py prep "Assignment 6" --response-file tests/fixtures/assignment6_study_analysis.json --research-response-file tests/fixtures/assignment6_research_results.json
python agent.py analyze-concepts "Assignment 6" --mode study --response-file tests/fixtures/assignment6_study_analysis.json
python agent.py diagnostic-plan "Tree Traversal" --pretty
python agent.py export-lifeos --pretty
python agent.py export-concepts --pretty
python agent.py check-vault-links --pretty
python agent.py research "binary tree traversal" --response-file tests/fixtures/research_results.json --no-cache --pretty
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\start.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\verify.ps1
```

Expected result: 124 tests pass. The saved-response `prep` command makes no model
or network request, reports model and research telemetry separately, and reruns
without duplicate notes or sections. The diagnostic plan spends zero model
tokens and writes nothing, and the link audit resolves all 114 live-vault
references without invoking a model.

**Do not overlook:**

- Anthropic structured-output calls have been smoke-tested. OpenAI-compatible
  live generation is now smoke-tested too (2026-08-08): `OpenAICompatibleAdapter`
  was exercised against Anthropic's OpenAI SDK compatibility endpoint
  (`https://api.anthropic.com/v1/`, `claude-haiku-4-5`, 48 in / 17 out,
  `$0.000133`). Auth, `max_tokens`, `temperature`, response shape, usage
  accounting, and `_parse_json` all verified; `prompt_tokens_details` and
  `completion_tokens_details` are always empty on that endpoint, which the
  adapter already handles. Two caveats: the compatibility layer ignores
  `response_format`, so valid JSON still depends on the prompt rather than
  transport-level schema enforcement, and testing through Anthropic validates
  the code path but not provider independence — a genuine third-party
  OpenAI-compatible endpoint is still unexercised.
- **The smoke test found a real defect.** `OpenAICompatibleAdapter` hardcoded
  `temperature: 0.1`, which current Claude models reject outright
  (`` `temperature` is deprecated for this model ``, HTTP 400) — making every
  Claude 5-family model unreachable through this adapter while older models
  still worked. Temperature is now opt-in via `MODEL_TEMPERATURE` and omitted
  by default. `tests/test_providers.py` covers the request body directly; the
  saved-response suite could never have caught this, because it never builds a
  live request.
- **Full-pipeline run (2026-08-08), `claude-sonnet-5` at default effort `high`:**
  transport carried a real Study analysis end to end — 6,693 input / 7,605
  output tokens, `stop_reason: stop`, 62.1s, ≈`$0.09`. It was then correctly
  **rejected at the citation gate**: the model produced citations to PDF pages
  that were not among the 10 evidence excerpts selected for that call
  (`selected_evidence: 10` of `available_evidence: 32`, `input_truncated: true`).
  The guarded commit held — `files_changed: []`, vault byte-identical, audit
  still 114/114. So the adapter is proven, but this model/prompt combination is
  not yet fit for unattended Study runs: the citation discipline that the
  Anthropic adapter satisfies does not transfer for free.
- **Known gap:** `estimated_cost` is `null` for this provider/model pair — the
  cost self-reporting has no pricing entry for models reached via
  `openai-compatible`, so `prep`'s dollar figures under-report on that path.
- Expert mode remains interface-tested but its real-vault rollout is explicitly
  deferred until all non-Expert implementation is complete.
- The token-free diagnostic flow is voice-agent callable and has a validated
  human-confirmed Handy/terminal dictation path. Native in-process microphone
  capture is optional and deferred. The model-independent research seam, local
  SearXNG provider, and `prep` migration are accepted.
- The vault already contains an unrelated modification to
  `School/CJE4663 001 11907/Module 12 Assignment- Statistical Application.md`.
  It was not made by this implementation and must not be folded into a handoff
  commit without explicit review.
- Commit code and vault changes separately. Never commit `.env`, source PDFs,
  API keys, or the local `analysis_runs.log`.

The no-vault-write multi-assignment benchmark is complete under
`prototypes/icm_token_experiment`. Selective retrieval passed the user's blind
review and the corrected contract-aware real-vault canary. It is now the internal
default for explicit-contract Study runs; broader or Expert promotion requires
separate evidence.

## Architecture

```
canvas-obsidian-sync/
├── sync.py              # entrypoint: ICS feed -> vault notes + Kanban board
├── canvas_ics.py         # ICS feed parsing
├── vault_notes.py        # note upsert logic (machine-owned vs user-owned fields)
├── kanban_board.py        # Kanban card sync
├── config.py             # env-driven config
├── agent.py              # entrypoint: study agent CLI
├── study_analysis/       # validated, provider-neutral concept-analysis module
│   ├── engine.py         # one deep interface: collect -> analyze -> validate -> commit
│   ├── contract.py       # trusted canonical-concept and signal acceptance criteria
│   ├── providers.py      # Anthropic, OpenAI-compatible/local, and saved-JSON adapters
│   ├── schema.py         # difficulty, effort, relationship, and mode contract
│   ├── sources.py        # hashed, page-scoped local source records
│   ├── context.py        # internal selective and page-scoped context adapters
│   ├── diagnostics.py    # confirmed token-free diagnosis and correction interface
│   ├── link_integrity.py # deterministic, read-only vault reference audit
│   ├── course_archive.py # confirmed metadata-only course lifecycle workflow
│   ├── research.py       # bounded model-free discovery, replay, and live adapters
│   ├── knowledge.py      # deterministic global knowledge-repository refresh
│   ├── transaction.py    # shared lock, conflict guard, commit, and rollback
│   ├── vault.py          # managed-section and canonical concept-note planning
│   └── lifeos.py         # compact assignment and concept signal exports
├── tests/                # interface-level analysis/diagnostic tests and fixtures
└── agent/
    ├── cli.py            # subcommands: setup-course, prep, ask, transcript, check-files, new-lecture, new-module
    ├── vault_query.py    # reads tasks/notes out of the vault
    ├── vault_write.py    # header-scoped note section writes (never touches other sections)
    ├── tools.py          # @beta_tool definitions exposed to the agent's tool-calling loop
    └── transcripts.py    # YouTube transcript fetching
```

## Setup

### 1. Obsidian plugins

- **[Kanban](https://github.com/mgmeyers/obsidian-kanban)** — renders
  `Boards/Assignments.md` as a drag-and-drop board.
- **[Git](https://github.com/vinzent03/obsidian-git)** — auto-commits/pushes
  the vault repo so it stays in sync across machines.
- **[TaskNotes](https://github.com/callumalpass/tasknotes)** — calendar and
  time-blocking view over the same synced frontmatter (`due`, `status`,
  `priority`, `task` tag).
- **[PDF++](https://github.com/RyotaUshio/obsidian-pdf-plus)** — annotate
  lecture PDFs and pull highlights into notes as backlinked callouts.

### 2. Get your Canvas ICS feed URL

Canvas → Calendar → Calendar Feed (also under Account → Settings). Treat this
URL like a password — anyone with it can read your calendar — so it only
ever goes in `.env`, never in a commit.

### 3. Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`: `CANVAS_ICS_URL` and `VAULT_PATH` (absolute path to your vault
on this machine). For concept analysis, also set `MODEL_PROVIDER` and
`MODEL_NAME`, plus the selected adapter's API key/base URL. The deterministic
sync and LifeOS export do not require a model or API key. Live web discovery is
separately opt-in through `RESEARCH_PROVIDER=searxng` and a trusted
`SEARXNG_BASE_URL`; saved-response replay needs neither setting.

### 4. Run

```
python sync.py
```

Run it manually at first. Once you trust it, put it on a schedule (Task
Scheduler → run `python sync.py` daily) so the vault stays current without
you remembering to run it.

## Agent commands

| Command | What it does |
|---|---|
| `setup-course <course>` | One-time interactive prompt for a course's textbook/topics, saved to `_Course Info.md` |
| `prep <assignment>` | Thin Study-mode alias over `AnalysisEngine`; writes validated assignment/concept views and optionally discovers links from reviewed public topics. |
| `prep-open [--max-attempts N]` | Preps open assignments in due order; defaults to one provider attempt and counts failed calls against the hard attempt cap. |
| `index-source <file> --course --title --pages --assignment` | Creates/refreshes a hashed source record and links it to an assignment |
| `analyze-concepts <assignment> [--mode study\|expert]` | Runs the provider-neutral, validated concept pipeline. Context selection is internal: contracted Study runs use selective retrieval, while uncontracted and Expert runs use page-scoped evidence. |
| `diagnose-concept <concept> [--target ...]` | Runs a token-free terminal/dictation diagnostic, discards the raw answer, and requires human confirmation before recording. |
| `diagnostic-plan <concept> [--target ...]` | Builds a token-free 1-3 question diagnostic plan and compact submission template; reads but does not write the vault. |
| `record-diagnostic <submission.json>` | Reconstructs the plan, requires confirmation outside the JSON, validates ordered typed evidence, and transactionally writes one immutable record plus the familiarity projection. |
| `correct-diagnostic <concept> <record-id>` | Appends a confirmed immutable correction and marks familiarity for reassessment. |
| `export-lifeos` | Prints compact machine JSON for LifeOS; add `--pretty` for human-readable output |
| `export-concepts` | Prints compact familiarity and reassessment signals without evidence, corrections, or Personal Notes. |
| `check-vault-links` | Audits internal links and managed file references without writing the vault or invoking a model. |
| `research <query> [--response-file ...]` | Returns a bounded provider-neutral result bundle with separate search-cost telemetry; saved replay uses zero network calls/model tokens and never writes the vault. |
| `refresh-knowledge` | Locally and transactionally creates/refreshes the standard global knowledge repository without model or network use. |
| `capture-knowledge <title> [--text ...\|--file ...]` | Captures local text into `Knowledge/Inbox`; without an option, prompts for one line of dictation. |
| `archive-course <course>` | Previews and, after confirmation, marks a fully completed course archived without moving folders. |
| `restore-course <course>` | Previews and, after confirmation, restores an archived course to active without moving folders. |
| `ask <question>` | Answers a free-form question against synced vault tasks through Claude's tool-calling loop. It does not read or change the LifeOS schedule. |
| `transcript <youtube-url> --course --title` | Fetches a lecture transcript and saves it as a note |
| `new-lecture <pdf> --course [--title]` | Scaffolds a dedicated study note for a PDF lecture already saved under that course's `Attachments/` folder |
| `new-module <module> --course [--files] [--title]` | Scaffolds (or updates) a study note for a Canvas module - zero or more readings, addable incrementally as they unlock |
| `check-files <course>` | Diffs a pasted Canvas file listing against what's saved locally, to catch missing downloads |

```
python agent.py setup-course "COP3410C 042 12962"
python agent.py prep "Assignment 6" --response-file tests/fixtures/assignment6_study_analysis.json --research-response-file tests/fixtures/assignment6_research_results.json
python agent.py index-source "C:\path\M11A.pdf" --course "COP3410C 042 12962" --title "M11A - General Trees" --pages 2-9 --assignment "Assignment 6"
python agent.py analyze-concepts "Assignment 6" --mode study
python agent.py diagnose-concept "Tree Traversal" --source voice --pretty
python agent.py diagnostic-plan "Tree Traversal" --pretty
python agent.py export-lifeos
python agent.py export-concepts
python agent.py check-vault-links --pretty
python agent.py research "binary tree traversal" --response-file tests/fixtures/research_results.json --no-cache --pretty
python agent.py refresh-knowledge --pretty
python agent.py capture-knowledge "Idea title"
python agent.py capture-knowledge "Idea title" --text "A thought to organize later."
python agent.py capture-knowledge "Reading notes" --file "C:\path\notes.txt"
python agent.py archive-course "COP3410C 042 12962" --pretty
python agent.py restore-course "COP3410C 042 12962" --pretty
python agent.py ask "what's due this week?"
python agent.py new-lecture M10A_linkedlists.pdf --course "COP3410C 042 12962"
python agent.py new-module "Module 9" --course "CJE4663 001 11907" --files "Telep & Weisburd.pdf"
```

`prep` and `prep-open` are convenience callers of the same provider-neutral
`AnalysisEngine` used by `analyze-concepts`; they do not implement another
analysis or search path. `ask` remains the separate legacy Anthropic task-query
command.

`research` is the read-only Phase 4 discovery boundary. It never invokes a
model, fetches result pages, or writes the vault. Saved JSON proves the interface
without network spend; live SearXNG is installed locally and remains explicitly
opt-in. Analysis builds compact queries only from reviewed
`analysis_contract.required_concepts` or `research_topics`, never assignment text
or personal data. `Helpful Links` uses normalized HTTPS hit titles and URLs only;
snippets do not enter the model prompt or vault, and page contents are not fetched
or independently verified.

### General knowledge repository

`refresh-knowledge` maintains the navigation contract for `Knowledge/Knowledge
Hub.md` plus `Inbox`, `Concepts`, `Sources`, and `Maps`. Repeated runs converge on
the same state, use the shared guarded transaction, preserve all existing notes,
and preserve the user-owned `Personal Navigation` section. The command is fully
local: it invokes no model or network service.

`Knowledge/Sources` is for durable global reference notes. Course source
artifacts remain under their course and continue to provide the hash- and
page-scoped evidence boundary for coursework analysis; the two source types are
not interchangeable. See `study_analysis/KNOWLEDGE.md` for the ownership and
layout contract.

`capture-knowledge "Idea title"` prompts for one line of interactive dictation;
`--text` and `--file` provide non-interactive alternatives. Capture is local and
token-free. An exact title/content retry returns the existing capture, including
after a rename or move that preserves its identity metadata, while the same title
with different content creates a separate note. The captured note and updated
navigation indexes commit atomically, then the note becomes user-owned and is
never overwritten by refresh. Unsafe titles, input files, or content fail before
any vault write.

Run the interactive command directly in PowerShell and dictate with Handy for a
routine zero-model capture. Prefer that prompt or `--file` for sensitive text;
inline `--text` values may remain in shell history or process arguments.

Moving an Inbox capture into `Concepts`, `Sources`, or `Maps` is intentionally
not automatic. Promotion remains a later, deliberate workflow.

### LifeOS seam

This project identifies academic work and learning difficulty; it does not
schedule time. `export-lifeos` returns a read-only, token-efficient JSON contract
containing stable assignment ID, title, course, course-archived state, due date,
status, concepts, difficulty, effort, confidence, and analysis date. LifeOS pulls
that data and combines it with the rest of the student's life. `export-concepts`
separately returns canonical concept, familiarity, confidence, assessment date,
diagnostic count, and reassessment status. Solution Archive content, grades,
source documents, diagnostic evidence/corrections, and personal notes are never
exported.

## Cost

This runs against a real student's daily usage, so cost is an interface
constraint rather than a provider-specific pricing guess. The core accepts any
model adapter that can return the required JSON contract. Built-in adapters
cover Anthropic and OpenAI-compatible hosted or local endpoints; saved JSON can
replay or verify a run with zero model spend.

Each analysis has explicit input and output budgets. Source extraction is
page-scoped and truncated at the configured input limit. Oversized work requires
an explicit higher limit, and interrupted or invalid analysis changes no
canonical note. `analysis_runs.log` stores compact local usage metadata but no
full prompt or coursework content. Model tokens/cost and research provider
requests/cost are separate fields. Routine sync, knowledge capture/refresh,
browsing existing knowledge, and LifeOS export remain token-free.

`prep-open` uses a hard provider-attempt cap (`--max-attempts`, default `1`)
plus the per-call input/output ceilings. It deliberately does not claim a hard
dollar cap: provider prices can change, so billing must be checked separately
against the measured usage telemetry.

Diagnostic planning, human-confirmed terminal/dictation assessment, recording,
correction, vault-link auditing, and course lifecycle changes are deterministic
and use zero model tokens.
Research also uses zero model tokens. A live miss can make one separately metered
search request; a valid cache hit and saved-response replay make none. Unknown
provider cost remains `null` rather than being reported as free.
Only typed, confidence-thresholded current-answer summaries can affect
familiarity. Raw answer/transcript fields are rejected, common
grade/completion-only wording is guarded, and the trusted human adapter remains
the authority boundary.

## Design decisions worth calling out

**Idempotent, frontmatter-keyed sync.** Assignments are matched by a
`canvas_uid` stored in frontmatter, not by title or file path — running
`sync.py` once or a hundred times against the same feed produces the same
result. It has to be designed this way from the start since it's re-run on
every scheduled sync, not patched in after the fact.

**Machine-owned vs. user-owned fields.** Note frontmatter splits into fields
refreshed every sync (`due`, `canvas_url`, `synced_at`, ...) and fields set
once at creation and never touched again (`status`, `priority`) — so marking
a task done in TaskNotes, or moving a Kanban card, is never silently reverted
by the next sync.

**One guarded analysis path.** `prep` delegates to `AnalysisEngine`, so its
managed assignment and concept-note writes use the same validation, conflict
checks, rollback, and user-section preservation as `analyze-concepts`.

**No fabricated web resources.** Model output may reference only an exact
supplied evidence locator, never a URL. Optional `Helpful Links` are a
deterministic projection of normalized HTTPS research hits; search snippets are
discarded and discovered pages are not presented as independently verified.

**PDFs live outside git.** Lecture PDFs are synced onto disk via a Windows
directory junction into an existing OneDrive folder, and `*.pdf` is
git-ignored in the vault repo. Obsidian/PDF++ can still embed and annotate
them locally, OneDrive handles cross-device sync, and the git repo never
bloats with binaries.

**"Lecture" vs. "module" study notes.** `new-lecture` assumes a clean,
uniform per-module PDF slide deck — true for a course like Data Structures,
where every module has exactly one downloadable deck. It doesn't generalize:
other courses organize content into Canvas "modules" that bundle a few
chapters plus other material under a placeholder title, with no slide deck
at all - readings, case studies, or nothing yet, since some professors don't
open a module's materials until that portion of the syllabus starts. `new-module`
handles that shape instead: it's keyed by the module name rather than a
filename, accepts zero or more attachments of any type, and re-running it
later with newly available files appends them to `## Materials` through its
own header-scoped write - so a module note can be created the moment
it appears in Canvas, empty, and filled in incrementally as the professor
uploads material, without ever touching whatever the student has already
written under `## Study Notes`.

## Known limitations

- Canvas's ICS feed doesn't include assignment descriptions or attachments —
  see "Why it exists" above. PDFs currently have to be saved off Canvas by
  hand into each course's `Attachments/` folder.
- Zoom/Panopto/Kaltura-embedded lecture transcripts aren't supported yet,
  only standalone YouTube URLs.
- No mobile Obsidian sync — intentionally out of scope for now.
- Local speech capture is validated through Handy plus the terminal/dictation
  adapter. Native in-process microphone capture is optional rather than a Phase 3
  blocker; model-driven assessment remains deliberately deferred.
- Local SearXNG provides discovery metadata, not analyzed evidence: result pages
  are not fetched, and links remain explicitly labeled as discovered rather than
  independently verified.
- Inbox promotion into `Concepts`, `Sources`, or `Maps` is not automated yet;
  that move remains a later deliberate workflow.
