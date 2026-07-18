# Canvas → Obsidian Sync

Pulls assignment due dates from Canvas (via your personal iCal feed) into your
Obsidian vault: one note per assignment, plus a card on a Kanban board. Your
vault stays the single source of truth — this script only ever adds
information, it never overwrites notes you've written or cards you've moved.

## What it does

1. Fetches your Canvas calendar feed (ICS format).
2. For each assignment, creates or updates a note under `School/<Course>/` in
   your vault. Only the frontmatter (course, due date, links) is rewritten on
   each run — the body of the note is written once, at creation, and never
   touched again.
3. Ensures a card linking to that note exists on `Boards/Assignments.md`
   (rendered as a drag-and-drop board by the Kanban plugin) — in "This Week"
   if due soon, otherwise "Backlog". Cards you've already moved, or personal
   tasks you've typed in by hand, are left exactly where they are.

## Setup

### 1. Obsidian plugins

Install these from Settings → Community Plugins in Obsidian, if you haven't
already:

- **[Kanban](https://github.com/mgmeyers/obsidian-kanban)** — renders
  `Boards/Assignments.md` as a board instead of raw markdown.
- **[Git](https://github.com/vinzent03/obsidian-git)** — auto-commits and
  pushes/pulls your vault's git repo, which is what makes it available across
  your different PCs. Point it at a GitHub repo for your vault; on your other
  machines, just clone that repo as the vault folder and let the plugin sync
  it.

### 2. Get your Canvas ICS feed URL

In Canvas: **Calendar → Calendar Feed** (also under Account → Settings). It
looks like `https://<school>.instructure.com/feeds/calendars/user_....ics`.
This URL is a bearer credential — anyone who has it can read your calendar —
so it only ever goes in `.env`, never in a commit.

### 3. Install and configure

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

(The venv keeps these dependencies separate from anything else on your
machine — you'll need to run the `activate` line again in any new terminal
before running `sync.py`.)

Edit `.env`: paste in your `CANVAS_ICS_URL`, and set `VAULT_PATH` to your
vault's absolute path on this machine (this one value is the only thing
you'll need to change per-PC, since the vault path may differ across
machines).

### 4. Run it

```
python sync.py
```

Run it manually at first. Once you trust it, put it on a schedule (Windows
Task Scheduler → run `python sync.py` every hour or so) so your board stays
current without you remembering to run it.

## Key concepts (this is a learning project — here's the "why" behind each choice)

**Idempotent syncing.** The script matches assignments by a `canvas_uid`
stored in frontmatter, not by note title or file path. Run it 1 time or 100
times against the same feed and you get the same result — no duplicate notes,
no duplicate cards. This matters because the script *will* be re-run
constantly (every sync), so "what happens if this runs twice" has to be
designed for from the start, not patched in later.

**Machine-owned vs. user-owned content.** Frontmatter is fully regenerated
every run; the note body is written once and never again. That split is what
makes it safe to automate at all — without it, every sync would risk
overwriting notes you'd actually written. The Kanban board follows the same
rule at a coarser grain: the script only *appends* new cards, it never
rewrites existing lines, so your manual drag-and-drop organization survives
indefinitely.

**Secrets belong in `.env`, never in code or git.** `CANVAS_ICS_URL` is
effectively a password (it grants read access to your calendar). `.env` is
git-ignored specifically so a `git push` can never leak it — this is the same
pattern you'll see in essentially every real-world project.

**Markdown + YAML frontmatter as a data format.** Obsidian notes are plain
text with a structured YAML header. That's what makes this whole approach
possible: the script can parse and edit notes with simple text/YAML
libraries, no proprietary format or API to reverse-engineer. This is also why
Dataview, Kanban, and dozens of other Obsidian plugins can all interoperate —
they agree on frontmatter as the shared interchange format.

**Where this hits its limits (by design).** The ICS feed only exposes titles
and due dates - not assignment descriptions or attached files (PDFs, .py,
.docx). Getting those requires the authenticated Canvas REST API, which is a
meaningfully bigger step (auth token, HTML parsing, file downloads). Rather
than half-build that now, the note template already has a `## Resources`
section reserved for it — see **Phase 2** below.

## Phase 2 (not built yet)

Use the Canvas REST API (personal access token) to pull each assignment's
`description` HTML, cross-reference the Modules API for chapter/week
context, detect any linked files, download them into a per-assignment
`Attachments/` folder, and populate `## Resources` in the note.

## Phase 3 (later)

An LLM layer on top: summarizing the pulled resources, breaking an assignment
into subtasks, or answering questions like "what's due this week in my CS
classes" against the synced data.
