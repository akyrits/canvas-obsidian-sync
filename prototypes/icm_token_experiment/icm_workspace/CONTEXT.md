# Coursework analysis pipeline

Flow: collect exact evidence → analyze concepts → validate and commit.

| Stage | Job | Input | Output | Human check |
|---|---|---|---|---|
| `01_collect` | verify selected evidence | assignment source manifest | hashed evidence packet | confirm page scopes |
| `02_analyze` | produce concept analysis | assignment + evidence packet | validated Study JSON | inspect citations and concepts |
| `03_commit` | update canonical notes | validated Study JSON | assignment and concept notes | review vault diff |

Stable policy lives in `_shared/`. Per-assignment material is working input.

