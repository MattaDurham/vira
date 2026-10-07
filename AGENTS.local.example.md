# AGENTS.local.md - how this Vira install is operated and changed

This is the owner's private operating file. It started as a copy of the
public template `AGENTS.local.example.md` and belongs to this install from
then on: edit it freely. Git ignores it, so nothing here is ever published.
AGENTS.md sends every agent that adds content or changes code here.

Keeping it in step with the public template is always a deliberate act,
never automatic:

- **Template updates.** `python -m server.agentslocal diff` shows what the
  public template changed since this copy was made. Take what you want by
  hand, then `python -m server.agentslocal adopt` records the template you
  are now in step with.
- **Promoting a rule.** `python -m server.agentslocal promote` lists the
  lines this copy carries that the template does not. Any that have proven
  general are candidates for the public template, changed on a branch like
  any other code.
- **Owner-specific notes** (machine paths, service names, personal release
  duties) go in the last section, "This install". They never belong in the
  public template.

Sections: 1. Adding local content. 2. Changing the code. 3. Finishing a
branch. 4. Public code and private state. 5. Coding rules. 6. This install.

---

## 1. Adding local content

This path is for research, Markdown, HTML dossiers, plans, and other material
the owner wants available in their installed Vira. Do not create a feature
branch or copy the content into tracked files unless the owner also asked to
change the public product.

| Content | Canonical local home | How Vira finds it |
|---|---|---|
| Searchable Markdown or research | The configured `vault_root` in the appropriate vault folder | Find indexes it on the normal vault scan |
| A self-contained HTML dossier | `python -m server.sitedocs add "<title>" <directory>` | The command copies it into git-ignored `static/docs/`, records it, and registers it with Reader |
| Documents that should remain in an existing external folder | A configured `reader_sources` folder | Reader keeps a soft pointer to the source file |
| Generated indexes, queues, thumbnails, and application state | `data/` | Vira owns and regenerates these; they are never the canonical document |

For a paired HTML and Markdown artifact, use both relevant homes: register the
portable HTML dossier with `server.sitedocs`, and place the Markdown edition
in the connected vault. Preserve existing vault frontmatter, links, and local
organization when refreshing a page. Personal or owner-only content never
enters git, even when every individual sentence would be harmless in public.

If a request could reasonably mean either local content or a public product
change, ask the owner which destination they intend before writing anything.
"Safe to publish" is not permission to publish. A mixed request can use both
paths, but keep its local content and public implementation separate.

If the requested content cannot be added through an existing local path, stop
and explain the missing capability. A new public feature needs a topic-neutral,
native setup path that another user can discover and use with their own
content. Do not turn one owner's document into a hard-coded product feature
merely to make that document appear locally.

---

## 2. Changing the code

- **Branch-first.** The live checkout (the primary working tree, serving
  port 8377) only ever changes at a merge. Work on your own branch in its
  own worktree: `scripts/branch.sh start <slug>`. If Vira dispatched you,
  you are already placed in one; stay there. Never create or change a file
  in the live checkout. Merging and pushing happen only on the owner's word
  (section 3).
- **Anchor every shell command in your worktree** with an absolute path.
  The tell that one slipped is `git status` in the live checkout showing
  modified tracked files; check that before trusting a test result or
  concluding "the fix did not take".
- **Tests.** `.venv/bin/python -m unittest discover tests`: stdlib
  unittest, no pytest needed; CI runs the suite on macOS and Windows. In a
  worktree with no `.venv` symlink, run the live checkout's interpreter by
  absolute path from your own cwd. A green suite in a fresh worktree is not
  proof: some tests read the real config or checkout, so before calling a
  failure pre-existing, confirm it also fails on `main`.
- **Never restart, stop, or kill the Vira server** or its service. A
  dispatched session runs as a child process inside it; a restart kills
  you mid-task. If a restart is needed, name it in your handoff for the
  owner to run. A detached session that survives a restart keeps running
  the old code until it finishes.
- **Address instances as `localhost:<port>`, never `127.0.0.1`**: some
  agent harnesses block the numeric loopback form. Live owns 8377; test
  instances (`scripts/branch.sh serve <slug>`) take 8378-8399 and run
  as fully functional parallel instances with their own runtime state. They
  use the same provider credentials and configured local sources. Branch
  identity is metadata, never a restriction on models, vaults, or workers.
- **Owner review uses the full current app.** Before handing over a preview,
  update the feature worktree onto the latest local `main` and refresh its
  private snapshot with `scripts/branch.sh serve <slug> --fresh --local`.
  Use the owner's actual data, configuration, and saved layout. Preserve the
  desktop arrangement and open the changed features so they are immediately
  visible. Verify the fresh snapshot is running. Plain `serve` reuses an
  already-running instance; `--fresh` stops and refreshes only that branch,
  preserving its previous snapshot. Finish or close that branch's running
  sessions before refreshing. Never refresh the instance hosting the current
  agent session or restart live. Synthetic `--fixture` previews are for
  isolated testing or an explicit sample-data request, not the owner-review
  handoff. Keep snapshots git-ignored and never replace live state.
- **Text IO carries `encoding="utf-8"` on both ends**: Windows defaults
  to cp1252 and CI runs there. No emojis in any output, code, or commit
  message. `scripts/preflight.sh --list` names every mechanically
  enforced rule alongside the incident that earned it.
- **Personal data never enters git.** `data/`, `docs/`, `AGENTS.local.md`,
  and the owner's stores are git-ignored on purpose: this repo is public,
  and a pre-commit PII guard (`sh scripts/install-hooks.sh`) backstops the
  rule. Never loosen the `.gitignore` or copy owner data into the tracked
  tree to make something easier to reach. PR descriptions, walkthroughs and
  commit messages are public prose too, and the hook does not scan PR
  bodies; walkthrough captures must pass
  `python -m walkthrough_anon scan <dir>`.
- **Instructions live in the AGENTS files only.** Never create `CLAUDE.md`
  or another model-specific instruction file: other models cannot read it.
  A rule every model must follow goes here or in the dispatch prompt.
- **A change to `branch.sh` or preflight binds from the NEXT merge**, not
  the one that lands it: `branch.sh merge` runs the live checkout's copy.
- **An artifact that keeps landing in the repo root gets a `.gitignore`
  line**, not a sentence asking people to remember it.

---

## 3. Finishing a branch

End every branch session with a short handoff, then one question:

- **What I did** - a few short bullets.
- **Where it is** - branch, worktree, latest commit.
- **The test instance** - its `localhost:<port>` URL, served with
  `scripts/branch.sh serve <slug> --fresh --local`.
- **Private state touched** - the kind of data and where it lives, or "none".
- **Then ask: keep testing, merge, or discard?**

If Vira dispatched you, do not ask that question yourself and do not merge
or push: when your turn ends Vira serves the test instance and raises the
landing card with the same three choices, and its Merge does the whole
landing.

In a session the owner runs directly, **"merge it" from the owner is the
authorization for the whole landing**, run end to end:

1. Commit everything on the branch with a real message (ASCII, no emoji).
2. If `main` moved, rebase in the worktree, never in the live tree, and
   re-run the tests. If preflight's `base` check says main's history was
   rewritten, use `git rebase --onto main <old-base>`, not `git rebase main`.
3. `scripts/branch.sh pr <slug> --ready`: the merge requires an open PR.
4. `scripts/branch.sh merge <slug>`: preflight, the merge into `main`, and
   the teardown of the spent worktree.
5. Push: `git -C <live checkout> push`.
6. If `server/` changed, name the restart for the owner; never run it.

Never merge or push without that word. "Discard" is
`scripts/branch.sh discard <slug>`. "Keep testing" leaves the branch and
its instance up.

---

## 4. Public code and private state

Vira's source repository is public. An owner's records, preferences, scores,
applications, messages, and local configuration are private. Public code may
read those values and apply a general rule, but it must not turn one owner's
value into a product default or a hard-coded policy. For example, the product
may read an employer's required office and compare it with configured places;
it must not assume that the required office should be any particular city.

The handoff in section 3 keeps the two apart: only the branch's commits are
ever merged or pushed. Private local state is named by kind and location,
never copied into the repository, and never merged or pushed.

---

## 5. Coding rules

Already enforced, so not repeated here: the merge gate (an open PR, clean
trees), preflight's checks (`scripts/preflight.sh --list`), the
live-checkout write guard, the merged-PR close guard, and the session
permission gate.

### Data and stores

- **External sources stay the source of truth.** The CRM's synthesized
  records are regenerated by its pipeline: Vira writes back only
  `PROFILE_EDITABLE_FIELDS`, plus `people.json` through
  `triage._read_people_backed_up` / `_write_people` (backup first), and
  never writes `master.json`. Owner corrections live in a Vira-owned overlay
  store applied at read time, never in a field a regeneration rewrites.
- **Small JSON stores go through `server/jsonstore.py`** (`read`,
  `mutate`, `write_atomic`): a fresh read every time, never cached, because
  the server, detached runners and CLI runs write them from separate
  processes. Never hold a store lock across a model or network call. Build a
  store's empty default fresh on each call; a shallow-copied template lets
  nested buckets accumulate across passes.
- **A destructive prune loads the complete set itself.** It never treats a
  list a caller passed in as "everything that exists" (one did, and deleted
  134 of 135 entries).
- **A new store that cannot be regenerated joins `backup.FILES`** in
  `server/backup.py`. Rebuildable indexes never hold the only copy of
  anything.
- **Every key read with `settings.get` needs a `settings.DEFAULTS` entry and
  a line in `config.example.json`**: a missing default raises KeyError and
  has broken startup twice. Config writes go through `onboard.config_set`.
  An empty path setting maps to a sentinel, because `Path("")` resolves to
  the working directory.
- **Agents never hand-write `data/config.json` or store files.** Agent
  writes go through Vira's validated tools (`viratools.WRITE_TOOLS`).
- **Secrets go in the Keychain through `settings.keychain_service()`**,
  namespaced per instance so a test instance can never read or overwrite
  live tokens.
- **Apple system databases are read-only** (Messages, AddressBook, Calendar,
  Photos). Contacts edits go through AppleScript with a vCard backup first.
- **Never rebuild `.venv`**: its copied Python binary holds the Full Disk
  Access grant, and worktrees symlink to it. **Never delete
  `data/whatsapp/session/`**: that unpairs the linked device.
- **Timestamps are stored in UTC** and converted to local time before
  grouping by day, never sliced with `[:10]`. A watermark against a source
  that truncates precision uses `>=` plus de-duplication by seen id.

### Server runtime

- **Every route is a sync `def` sharing one GIL with the event loop.** Wrap
  CPU-heavy request work in `admission.cpu()`, never with a model or network
  call inside it. Chores that hold the GIL for minutes run out of process
  through the module's own CLI (`python -m server.X`).
- **Every background worker `main.py` builds must be `.start()`ed.** One
  shipped built and never started.
- **Every socket client gets an explicit timeout** (IMAP, SMTP, HTTP). A
  stalled IMAP read once hung the indexer for a week with nothing logged.
- **Every torch forward pass on Apple's GPU backend (MPS) holds
  `localmodels._infer_lock`**; without it the service crash-looped.
- **Model calls.** `suggest.complete` is a full agent, not a plain
  completion: pass `tools=READ_TOOLS` whenever the prompt carries untrusted
  text. `suggest.effective_backend` is the only answer to "which model
  replies". Context budgets come from `server/modelbudget.py`, and every cap
  says why in a comment (preflight `capdoc` enforces this).
- **The Agent SDK kills a session that sends more than 1 MiB on one output
  line.** Ask `modelbudget.tool_result_cap()` before raising any tool-output
  cap.
- **Sessions always pass `can_use_tool=runner.gate`**, and no dispatch site
  hardcodes a permission level.
- **Degrade honestly.** A missing source or a down backend returns a named
  status, never an exception or a silent switch to another provider. Nothing
  is dropped silently, caps say when they were hit, and a failed read is
  never cached as a fact.

### Tests

- **Tests never read the real machine or the real stores.** Patch every
  source a module reads, not only what it writes; pin config values instead
  of reading `data/config.json`; add a guard that fails if a real store
  changed. Reproduce CI locally with `HOME=$(mktemp -d)`.
- **Test the join, not just the halves.** A write guard passed its tests
  while disarmed for four days because the record it relied on was built by
  hand. Mutation-check guards (a test stays only if it fails when the fix is
  removed), and assert the effect, never the argv spelling or source text.
- **Never `git checkout -- <file>` over uncommitted work** to mutation-check
  it; commit or stash first.
- **Use `FrozenClockCase` for anything date-relative**, and compute fixture
  dates relative to now. Fixtures are synthetic: `@example.com` addresses
  and phone numbers in the 555-01xx block.
- **Windows (CI's Windows job is the only Windows machine).** Test helpers
  are `.py` files, never a bare `#!` script (WinError 193). Before Python
  3.13, `time.monotonic()` ticks in ~15.6 ms steps, so never compare a fast
  measurement with a strict `>` against zero. Never patch
  `threading.Thread`; patch the module's spawn seam. Never write a real pid
  into `.test-instance.json` (`os.kill(pid, 0)` terminates it on Windows).
  Paths may start with `C:\`. Branch on `settings.IS_MAC` / `IS_WIN`, and
  use `settings.strf` for no-padding date formats.
- **FastAPI.** A Pydantic body model is defined above its route, or the body
  is read as query parameters and every call returns 422. `@app.get` does
  not answer HEAD requests.

### Frontend

- **`static/app.js`, `static/index.html` and `static/style.css` are large
  single files**; parallel UI branches usually conflict there, so expect
  rebase work.
- **A new window is one `WINDOWS` entry**; the command palette is built from
  it. Retiring or folding a window needs a `WORK_ALIAS` / `FIND_ALIAS` /
  `PEOPLE_ALIAS` entry plus `HASH_ROUTES`, so deep links and saved layouts
  keep working.
- **A synced UI-state key goes in both `UI_SYNC_KEYS` (app.js) and
  `uistate.KEYS` (server)**; the `SyncKeyParity` test checks it.
- **Use the shared helpers**: `lsGet`/`lsSet`, `startPoll`, `bindSheet`
  (the `.sheet-head` drag handle), `cardAction`, `longPress`,
  `showContextMenu`, `errText`, and `copyText` (the clipboard API is
  unavailable over plain HTTP). Drag code respects `CARD_CONTROL_SEL`, or a
  handle swallows a control's click.
- **No CDN**: vendor third-party libraries into `static/vendor`.
- **Floating windows are CSS-zoomed**: read window geometry from inline
  style, not `getBoundingClientRect` deltas.
- **Mobile: nothing may be wider than the viewport**, or iOS shrinks the
  whole app. Give the flexible child `min-width: 0` and truncate it; never
  clip an ancestor. Text inputs are 16px or larger.
- **Render functions take their container as an argument**; querying the
  document while a card is still detached has frozen a list twice.
- **Verifying in a browser.** Load mobile at a real 402px width (`isDesktop`
  is fixed at load) and desktop at 1100px or wider. A backgrounded Browser
  pane does not paint or run `requestAnimationFrame`, so geometry and
  animation read as broken: force a frame or use a foreground tab. Call
  `exitToFreeform()` before measuring windows under a saved layout.

---

## 6. This install

Owner-specific notes: machine paths, the service name and restart command,
personal release duties, open decisions. Nothing in this section belongs in
the public template.

- (none yet)
