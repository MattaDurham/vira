"""Install a macOS text Service that opens Vira's live Definition window.

The installed workflow uses only AppleScript and Foundation. It does not
depend on this checkout, Python, clipboard access, or browser automation.
"""
import argparse
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit


NAME = "Define with Vira"
BUNDLE_ID = "app.vira.define-with-vira"
DEFAULT_URL = "http://localhost:8377/"
PBS = "/System/Library/CoreServices/pbs"

SOURCE = '''use framework "Foundation"
use scripting additions

property viraURL : "__VIRA_URL__"

on definitionURL(input)
    if class of input is not list then set input to {input}
    set selectedText to (current application's NSArray's arrayWithArray:input)'s componentsJoinedByString:" "
    set selectedText to selectedText's stringByTrimmingCharactersInSet:(current application's NSCharacterSet's whitespaceAndNewlineCharacterSet())
    set selectedText to selectedText's stringByReplacingOccurrencesOfString:"[[:space:]]+" withString:" " options:(current application's NSRegularExpressionSearch) range:{0, selectedText's |length|()}
    if selectedText's |length|() is 0 then error "Select a word or a short phrase to define."
    if selectedText's |length|() > 120 or (selectedText's componentsSeparatedByString:" ")'s |count|() > 8 then error "Select a word or a short phrase (up to 8 words and 120 characters)."
    set allowed to current application's NSCharacterSet's characterSetWithCharactersInString:"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    set encodedText to selectedText's stringByAddingPercentEncodingWithAllowedCharacters:allowed
    return viraURL & "#define/" & (encodedText as text)
end definitionURL

on run {input, parameters}
    open location definitionURL(input)
    return {}
end run
'''


def local_url(value):
    """Only a local Vira origin can receive selections from this Service."""
    if not re.fullmatch(r"http://localhost(?::[0-9]+)?/?", value):
        raise ValueError("Use a local Vira URL such as http://localhost:8377/")
    port = urlsplit(value).port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The Vira port must be between 1 and 65535")
    return value.rstrip("/") + "/"


def workflow_files(url=DEFAULT_URL):
    source = SOURCE.replace("__VIRA_URL__", local_url(url))
    info = {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": NAME,
        "NSServices": [{
            "NSMenuItem": {"default": NAME},
            "NSMessage": "runWorkflowAsService",
            "NSSendTypes": ["public.utf8-plain-text"],
            "NSIconName": "NSActionTemplate",
        }],
    }
    document = {
        "AMDocumentVersion": "2",
        "actions": [{"action": {
            "AMAccepts": {"Container": "List", "Optional": True,
                          "Types": ["com.apple.applescript.object"]},
            "AMProvides": {"Container": "List",
                           "Types": ["com.apple.applescript.object"]},
            "AMActionVersion": "1.0.2",
            "AMApplication": ["Automator"],
            "ActionBundlePath": "/System/Library/Automator/Run AppleScript.action",
            "ActionName": "Run AppleScript",
            "ActionParameters": {"source": source},
            "BundleIdentifier": "com.apple.Automator.RunScript",
            "Class Name": "RunScriptAction",
            "UUID": str(uuid.uuid4()),
            "InputUUID": str(uuid.uuid4()),
            "OutputUUID": str(uuid.uuid4()),
        }}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.text",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 11,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.text",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    return {"Info.plist": info, "document.wflow": document}


def check_owned(target):
    if target.is_symlink():
        raise ValueError(f"Refusing to change a linked workflow: {target}")
    if not target.exists():
        return
    try:
        info = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"An unrelated workflow already exists at {target}") from exc
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError(f"An unrelated workflow already exists at {target}")


def install(services, url=DEFAULT_URL):
    files = workflow_files(url)
    target = services / (NAME + ".workflow")
    check_owned(target)
    services.mkdir(parents=True, exist_ok=True)
    # Stage the complete bundle before touching an existing installation.
    # Preserve that installation as a backup until the rename succeeds.
    with tempfile.TemporaryDirectory(prefix=".vira-define-", dir=services) as tmp:
        staged = Path(tmp) / target.name
        contents = staged / "Contents"
        contents.mkdir(parents=True)
        for name, data in files.items():
            (contents / name).write_bytes(plistlib.dumps(data))
        backup = Path(tmp) / "previous.workflow"
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise
    return target


def uninstall(services):
    target = services / (NAME + ".workflow")
    check_owned(target)
    if target.exists():
        shutil.rmtree(target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="local Vira URL (default: %(default)s)")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("This Service requires macOS")
    services = Path.home() / "Library" / "Services"
    try:
        target = (uninstall(services) if args.uninstall
                  else install(services, args.url))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"{'Removed' if args.uninstall else 'Installed'}: {target}")
    try:
        subprocess.run([PBS, "-update"], check=True, capture_output=True,
                       text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        print("macOS could not refresh Services. Reopen the app you are selecting text in.")
    if not args.uninstall:
        print("Select text, then right-click > Services > Define with Vira.")
        print("Vira must be running. The definition opens in your default browser.")
        print("Enable the action or add a shortcut in System Settings > Keyboard > Keyboard Shortcuts > Services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
