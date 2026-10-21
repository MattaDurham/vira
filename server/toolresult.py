"""One line about what a tool call returned, and whether it failed.

Shared by Fleet's transcript readers and the job runner's live log, so
Fleet's Activity list and a session window say the same thing about the
same call.

A shell command can fail without the tool saying so: one that ends in
`echo "exit $?"` exits 0 whatever ran before it, and the tool reports
success. Such a failure is read from the text instead: a non-zero exit
printed as the first or last line, "command not found", unittest's FAILED
summary, or output that ends in a Python traceback's exception line.
"""
import re

# One row in Fleet's Activity list, one line in a session window.
LINE_CAP = 160

_EXIT = re.compile(r"^(?:exit(?: code| status)?|exited with code)[:\s]+(-?\d+)\s*$", re.I)
_MISSING = re.compile(r"command not found|is not recognized as an internal or external command")
_TESTS_FAILED = re.compile(r"^FAILED \((?:failures|errors)=")
_RAISED = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b:?")
_SUMMARY = re.compile(r"^(?:Ran \d+ tests? in |\d+ (?:passed|failed)\b|=+ .*\b(?:passed|failed)\b)")
_TAG = re.compile(r"</?[a-z_]+>")
# Tools whose output is worth a line even on success. A file read or an
# edit returns the file or "updated successfully", which says nothing.
SHELL_TOOLS = {"Bash", "BashOutput", "PowerShell", "shell", "exec_command",
               "shell_command", "local_shell", "container.exec"}


def text_of(content):
    """A tool result's text, from a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _clip(s):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= LINE_CAP else s[: LINE_CAP - 1] + "…"


def summarize(content, is_error=False, exit_code=None):
    """(line, failed): the line worth showing beside the call, and whether
    the call failed. line is None when the call returned no text. exit_code
    is a code the tool reported on its own (Codex does), if any."""
    lines = [_TAG.sub("", ln).strip() for ln in text_of(content).splitlines()]
    lines = [ln for ln in lines if ln]
    code = str(exit_code) if exit_code not in (None, 0, "0") else None
    for ln in lines[:1] + lines[-1:]:
        m = _EXIT.match(ln)
        if m and m.group(1) != "0":
            code = code or m.group(1)
    missing = next((ln for ln in lines if _MISSING.search(ln)), None)
    tests = next((ln for ln in lines if _TESTS_FAILED.match(ln)), None)
    # Uncaught: a traceback that is the last thing the command printed.
    raised = (lines[-1] if lines and _RAISED.match(lines[-1])
              and any(ln.startswith("Traceback (most recent call last)") for ln in lines) else None)
    failed = bool(is_error or code or missing or tests or raised)
    if failed:
        why = missing or tests or raised or next((ln for ln in lines if not _EXIT.match(ln)), None)
        head = f"exit {code}" if code else "failed"
        return _clip(f"{head}: {why}" if why else head), True
    # An "exit 0" a command printed about itself says nothing.
    lines = [ln for ln in lines if not _EXIT.match(ln)]
    if not lines:
        return None, False
    for i, ln in enumerate(lines):
        if _SUMMARY.match(ln):
            after = lines[i + 1] if i + 1 < len(lines) else ""
            return _clip(f"{ln} {after}" if after.startswith("OK") else ln), False
    return _clip(lines[-1]), False


def for_feed(item, name, content, is_error=False, exit_code=None):
    """Mark a finished feed item: error, plus `out` for a shell command or
    any call that failed."""
    line, failed = summarize(content, is_error, exit_code)
    item["error"] = failed
    if line and (failed or name in SHELL_TOOLS):
        item["out"] = line
