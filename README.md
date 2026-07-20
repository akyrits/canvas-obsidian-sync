# Canvas → Obsidian Study System

A daily-syncing bridge between Canvas LMS and Obsidian, plus a small
Claude-powered study agent layered on top. Built as a portfolio project to
explore what an LLM agent can actually do against a real, personal dataset —
my own coursework — instead of a toy demo.

This repo is the code only. The actual Obsidian vault (notes, PDFs,
coursework) lives in a separate private repository, since it contains real
academic content.

## What it does

**Sync** (`sync.py`) — pulls assignment due dates from a Canvas calendar feed
and creates/updates one Obsidian note per assignment, with a matching card on
a Kanban board. Runs on a schedule (Windows Task Scheduler, every 24h) with no
manual intervention.

**Study agent** (`agent.py`) — a CLI on top of the synced vault that:

- generates a step-by-step outline + key-concepts explanation for any
  assignment, grounded in that course's actual textbook and live web search
  rather than invented sources
- answers ad-hoc questions about synced tasks ("what's due this week in my CS
  classes?") through Claude's tool-calling loop
- fetches YouTube lecture transcripts and saves them as notes
- scaffolds a dedicated study note for a professor-provided PDF lecture, ready
  for PDF++ annotation

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
course, PDFs saved off Canvas manually) plus live web search, instead of
assuming perfect scraped context. That's closer to how an agent actually has
to behave in most permission-constrained real-world settings anyway.

## Architecture

```
canvas-obsidian-sync/
├── sync.py              # entrypoint: ICS feed -> vault notes + Kanban board
├── canvas_ics.py         # ICS feed parsing
├── vault_notes.py        # note upsert logic (machine-owned vs user-owned fields)
├── kanban_board.py        # Kanban card sync
├── config.py             # env-driven config
├── agent.py              # entrypoint: study agent CLI
└── agent/
    ├── cli.py            # subcommands: setup-course, prep, ask, transcript, check-files, new-lecture
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

Fill in `.env`: `CANVAS_ICS_URL`, `VAULT_PATH` (absolute path to your vault
on this machine), and — only if you want the agent — `ANTHROPIC_API_KEY`.

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
| `prep <assignment>` | Writes `## How to Approach This` + `## Key Concepts` on a synced assignment note, using web search and that course's textbook context |
| `ask <question>` | Answers a free-form question against your synced tasks, via Claude's tool-calling loop |
| `transcript <youtube-url> --course --title` | Fetches a lecture transcript and saves it as a note |
| `new-lecture <pdf> --course [--title]` | Scaffolds a dedicated study note for a PDF lecture already saved under that course's `Attachments/` folder |
| `check-files <course>` | Diffs a pasted Canvas file listing against what's saved locally, to catch missing downloads |

```
python agent.py setup-course "COP3410C 042 12962"
python agent.py prep "LinkedList Worksheet"
python agent.py ask "what's due this week?"
python agent.py new-lecture M10A_linkedlists.pdf --course "COP3410C 042 12962"
```

## Cost

This runs against a real student's actual daily usage, not a demo, so cost
was a design constraint from the start rather than an afterthought.

**Model: `claude-sonnet-5` throughout, not Opus.** At list price Sonnet 5 is
$3 / $15 per million input/output tokens versus $5 / $25 for Opus 4.8 — and
Anthropic's Sonnet 5 introductory pricing ($2 / $10 per MTok through
2026-08-31) narrows the gap further. None of the five commands (`prep`,
`ask`, `setup-course`, `transcript`, `check-files`) need Opus-tier reasoning:
outlining an assignment, answering "what's due this week," or transcribing a
video are bounded, well-specified tasks, not open-ended research. Sonnet is
the right tier for that shape of work, not a compromise.

**Prompt caching and the Batch API were deliberately not adopted.** Both are
real levers — cached tokens cost roughly a tenth as much on a hit, and Batch
API requests run at 50% off — but both pay off on *volume*: caching needs a
large, stable prefix reused across many requests, and Batch API is for
bulk/non-interactive jobs. A student running `prep` on a handful of
assignments a week doesn't generate that volume, so reaching for either here
would be complexity added for a discount that never materializes. If this
ever became a multi-course, prep-everything-at-once workflow (e.g. batch
`prep` runs at the start of a semester), Batch API would be the first thing
to add.

**No hard usage numbers are published here** — this is a single-user tool,
not a metered product, and log-scraping a personal API bill isn't worth the
readme space. The bounded, per-command nature of every call (a few thousand
tokens in, a few hundred to a couple thousand out) is what keeps this cheap
in practice, not caching or batching tricks.

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

**Header-scoped writes.** The agent's `prep` command rewrites only the
`## How to Approach This` / `## Key Concepts` sections it owns
(`vault_write.set_section`) — content under `## Notes` is left alone,
verified by re-running `prep` against a note with real user-written content
and confirming it survives unchanged.

**No fabricated quotes.** `prep`'s prompt explicitly bans quoting anything
attributed to the course textbook, since a paid textbook's exact wording
can't be verified through web search. An early version only said "quote
sparingly," which still produced a confidently-fabricated quote — banning
quotation marks entirely for textbook-attributed content was what actually
fixed it.

**PDFs live outside git.** Lecture PDFs are synced onto disk via a Windows
directory junction into an existing OneDrive folder, and `*.pdf` is
git-ignored in the vault repo. Obsidian/PDF++ can still embed and annotate
them locally, OneDrive handles cross-device sync, and the git repo never
bloats with binaries.

## Known limitations

- Canvas's ICS feed doesn't include assignment descriptions or attachments —
  see "Why it exists" above. PDFs currently have to be saved off Canvas by
  hand into each course's `Attachments/` folder.
- Zoom/Panopto/Kaltura-embedded lecture transcripts aren't supported yet,
  only standalone YouTube URLs.
- No mobile Obsidian sync — intentionally out of scope for now.
