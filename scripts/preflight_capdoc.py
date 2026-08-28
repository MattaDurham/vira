#!/usr/bin/env python3
"""An undocumented model-context cap is the defect, so the check is the comment.

THE INCIDENT. define.py fed a model 5 x 1800 characters against a backend
reporting a 1,000,000-token context window in its own response JSON. The two
constants carried NO comment, and they sat directly above MAX_SELECTION_WORDS,
which carries a two-line justification. The number that was reasoned about got
a reason written down; the ones nobody had thought about did not. That is the
tell this scans for.

It had already happened once - find.ASK_LIMIT was 8, "small enough that the
right note routinely sat outside it while the model answered confidently from
the wrong ones" - and it stayed invisible both times because a cap that is too
small produces confident output rather than an error.

WHAT COUNTS. A module-level integer constant whose NAME says it bounds how
much material something sees, in a module that builds prompts. Requiring a
comment is deliberately weaker than requiring the seam: plenty of these are
genuinely fine as literals (a batch size, a UI row cap), and a check that
demands the wrong fix gets routed around. What it refuses is a number nobody
has argued for - write the sentence, or route it through modelbudget.

AST, never grep: a comment on the line above is invisible to a naive line
scan, and a check that cries wolf gets ignored, which is worse than none.
"""
import ast
import io
import pathlib
import re
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

# Names that claim to bound what a MODEL sees. Deliberately narrow: a cap on
# what gets stored or rendered is a different question and is not scanned.
CAP_NAME = re.compile(
    r"^(MAX_)?[A-Z0-9_]*(CONTEXT|CHARS|PASSAGES|ANCHORS|CHUNKS?|TAIL|EXCERPT"
    r"|SNIPPET|PROMPT|WINDOW)[A-Z0-9_]*$"
)
# A module only qualifies if it actually composes a prompt.
PROMPT_MARKERS = ("suggest.complete", "suggest.suggest", "_compose", "PROMPT")
MIN_VALUE = 200          # below this it is a count of items, not a budget


def _comment_lines(text):
    """Line numbers carrying a comment, from real tokens rather than a scan
    for '#' - a hash inside a string is not documentation."""
    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


# How far above a constant a group's introducing comment may sit. Small on
# purpose: past a handful of siblings the comment is no longer about it.
GROUP_LOOKBACK = 6
_SIBLING = re.compile(r"^[A-Z_][A-Z0-9_]*\s*=|^\s*$")


def _documented(lineno, lines, comments):
    """Whether this constant has a stated reason above it."""
    if lineno in comments:
        return True
    for step in range(1, GROUP_LOOKBACK + 1):
        ln = lineno - step
        if ln < 1:
            return False
        if ln in comments:
            return True
        if not _SIBLING.match(lines[ln - 1]):
            return False           # unrelated code - the walk ends here
    return False


def findings():
    out = []
    for path in sorted(SERVER.glob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(m in text for m in PROMPT_MARKERS):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        comments = _comment_lines(text)
        lines = text.split("\n")
        for node in tree.body:                       # module level only
            if not isinstance(node, ast.Assign):
                continue
            if not (len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            val = node.value
            if not (isinstance(val, ast.Constant)
                    and isinstance(val.value, int)
                    and not isinstance(val.value, bool)):
                continue
            if val.value < MIN_VALUE or not CAP_NAME.match(name):
                continue
            # Documented if the line itself carries a comment, or a comment
            # sits above it - possibly across a few sibling constants.
            #
            # A BLOCK COMMENT INTRODUCING A GROUP DOCUMENTS THE GROUP, which
            # is this repo's own house style (vault.py explains
            # ENGINE_CHUNK_CHARS and ENGINE_PROMPT_CHARS in one block above
            # both). Looking only one line up called that undocumented - a
            # false positive, and a check that cries wolf gets ignored, which
            # is worse than no check. The walk stops at anything that is not
            # a blank line or another simple assignment, so it cannot credit
            # a comment belonging to unrelated code further up.
            if _documented(node.lineno, lines, comments):
                continue
            out.append(f"{path.relative_to(ROOT)}:{node.lineno}: "
                       f"{name} = {val.value}"
                       " has no stated reason")
    return out


def main():
    found = findings()
    if "--count" in sys.argv:
        print(len(found))
        return 0
    for line in found:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
