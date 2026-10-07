"""AGENTS.local.md - the owner's private operating file, and its public template.

AGENTS.md routes every agent that adds content or changes code to
AGENTS.local.md. Each install's copy is seeded from the tracked template
AGENTS.local.example.md and belongs to the owner from then on; git ignores
it. Two deliberate flows keep the pair in step, and neither ever writes the
owner's copy:

- adopt: the template improved upstream. `diff` shows what it changed since
  this copy was seeded, against a snapshot of the template taken at seed
  time. The owner takes what they want by hand, then `adopt` records the
  current template as the new baseline.
- promote: a local rule has proven general. `promote` lists the lines the
  owner's copy carries that the template does not: candidates for a public
  change, made on a branch like any other code.

Stdlib only, with no other server imports: the installers and branch.sh run
this before the app's dependencies are guaranteed, from any checkout.

    python -m server.agentslocal seed|status|diff|adopt|promote [--root DIR]
"""
import argparse
import difflib
import shutil
import sys
from pathlib import Path

LOCAL = "AGENTS.local.md"
EXAMPLE = "AGENTS.local.example.md"
# The template exactly as it was when the owner's copy was seeded (or last
# adopted). data/ is instance state, git-ignored like the copy itself.
BASE = Path("data") / "agents-local-base.md"


def paths(root=None):
    r = Path(root) if root else Path(__file__).resolve().parent.parent
    return r / LOCAL, r / EXAMPLE, r / BASE


def _lines(p):
    return p.read_text(encoding="utf-8").splitlines()


def seed(root=None):
    """Create AGENTS.local.md from the template when it is missing.

    Never overwrites: an existing copy, or a link to one, is the owner's.
    A linked git worktree is never seeded: branch.sh links the live
    checkout's copy into it, and a second, seeded copy there would quietly
    take an agent's edits away from the owner's real file.
    Returns "seeded", "present", "worktree" or "no-template"."""
    mine, example, base = paths(root)
    if mine.exists() or mine.is_symlink():
        return "present"
    if (mine.parent / ".git").is_file():
        return "worktree"
    if not example.is_file():
        return "no-template"
    shutil.copyfile(example, mine)
    base.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, base)
    return "seeded"


def status(root=None):
    """Where the owner's copy stands against the template, as a dict."""
    mine, example, base = paths(root)
    out = {"local": mine.is_file(), "template": example.is_file(),
           "baseline": base.is_file(), "template_changed": None,
           "local_only_lines": None}
    if out["template"] and out["baseline"]:
        out["template_changed"] = _lines(example) != _lines(base)
    if out["local"] and out["template"]:
        out["local_only_lines"] = len(promote(root))
    return out


def diff(root=None):
    """What the public template changed since the owner's copy was seeded.

    With no baseline (a copy made by hand), there is nothing to measure the
    template's own changes against, so this falls back to the whole
    difference between the owner's copy and the template, and says so."""
    mine, example, base = paths(root)
    if not example.is_file():
        return "", "no template in this checkout"
    if base.is_file():
        d = difflib.unified_diff(_lines(base), _lines(example),
                                 "template when your copy was made",
                                 "template now", lineterm="")
        return "\n".join(d), ""
    if not mine.is_file():
        return "", f"no {LOCAL} yet - run: python -m server.agentslocal seed"
    d = difflib.unified_diff(_lines(mine), _lines(example),
                             f"your {LOCAL}", "template now", lineterm="")
    return "\n".join(d), ("no record of which template your copy started "
                          "from, so this is the whole difference between "
                          "your copy and the template")


def adopt(root=None):
    """Record the current template as the baseline the owner is in step with.
    Touches only the snapshot, never the owner's copy."""
    _, example, base = paths(root)
    if not example.is_file():
        return False
    base.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, base)
    return True


def promote(root=None):
    """Lines in the owner's copy that the template does not carry, as
    (line number, text). Line-set containment rather than a positional diff,
    so a rule the owner moved is not reported as new."""
    mine, example, _ = paths(root)
    if not (mine.is_file() and example.is_file()):
        return []
    template = set(_lines(example))
    return [(n, line) for n, line in enumerate(_lines(mine), 1)
            if line.strip() and line not in template]


def main(argv=None):
    # A Windows console may not encode the template's punctuation; replace
    # rather than crash the one command a fresh install runs.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m server.agentslocal")
    ap.add_argument("command",
                    choices=["seed", "status", "diff", "adopt", "promote"])
    ap.add_argument("--root", help="checkout to act on (default: this one)")
    a = ap.parse_args(argv)

    if a.command == "seed":
        r = seed(a.root)
        print({"seeded": f"created {LOCAL} from {EXAMPLE}",
               "present": f"{LOCAL} already exists - left as it is",
               "worktree": ("this is a branch worktree; its copy is a link to "
                            "the live checkout's - run: scripts/branch.sh adopt"),
               "no-template": f"no {EXAMPLE} in this checkout - nothing to seed",
               }[r])
        return 0
    if a.command == "status":
        s = status(a.root)
        if not s["local"]:
            print(f"no {LOCAL} yet - run: python -m server.agentslocal seed")
            return 0
        if not s["template"]:
            print(f"no {EXAMPLE} in this checkout to compare against")
        elif s["template_changed"]:
            print("the public template changed since your copy was made - "
                  "review: python -m server.agentslocal diff")
        elif s["template_changed"] is None:
            print("no record of which template your copy started from - "
                  "python -m server.agentslocal adopt records the current one")
        else:
            print("your copy is in step with the public template")
        if s["local_only_lines"]:
            print(f"{s['local_only_lines']} line(s) are yours alone - "
                  "see: python -m server.agentslocal promote")
        return 0
    if a.command == "diff":
        text, note = diff(a.root)
        if note:
            print(f"note: {note}")
        print(text or "no changes")
        return 0
    if a.command == "adopt":
        if adopt(a.root):
            print("recorded the current template as your baseline; "
                  f"{LOCAL} itself was not touched")
            return 0
        print(f"no {EXAMPLE} in this checkout")
        return 1
    if a.command == "promote":
        rows = promote(a.root)
        if not rows:
            print("nothing in your copy that the template lacks")
        for n, line in rows:
            print(f"{n:5}: {line}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
