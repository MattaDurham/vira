# AGENTS.md - start here

Every agent reads this file first, so it holds only what every agent needs
and then sends you to the file for your task. Read that file before doing
anything else.

This repository is public. Personal data never enters git.

| Your task | Read |
|---|---|
| Installing Vira on a machine | [AGENTS.install.md](AGENTS.install.md). That file is the whole job. |
| Adding research, documents, or other content to an installed Vira | [AGENTS.local.md](AGENTS.local.md), section 1 |
| Changing the code: a feature, a fix, a review, a dispatched task | [AGENTS.local.md](AGENTS.local.md) |

`AGENTS.local.md` is the owner's private operating file, and git ignores
it. If it does not exist yet, create it from the public template first:

    .venv/bin/python -m server.agentslocal seed

(Windows: `.venv\Scripts\python -m server.agentslocal seed`. With no venv
yet, copy `AGENTS.local.example.md` to `AGENTS.local.md`.) In a branch
worktree it is a link to the live checkout's copy, so an edit there is the
owner's real file.

If a request could mean either adding local content or changing the public
product, ask the owner which before writing anything.

Vira keeps its instructions only in these AGENTS files, so every model reads
the same ones. Do not create `CLAUDE.md`.

## If you are changing code

These hold even before you open `AGENTS.local.md`:

- Work on your own branch in its own worktree:
  `scripts/branch.sh start <slug>`. If Vira dispatched you, you are already
  in one; stay there.
- Never create or change a file in the live checkout (the primary working
  tree, serving port 8377).
- Never restart, stop, or kill the Vira server.
- Merge and push only when the owner says so. "Merge it" from the owner
  covers the whole landing; the steps are in `AGENTS.local.md`, section 3.
