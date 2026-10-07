"""Open Vira's fixed Microsoft destinations in the machine's default browser."""
import os
import shutil
import subprocess
from urllib.parse import urlsplit

from . import settings


def open_url(url):
    parsed = urlsplit(url)
    local = (parsed.scheme in {"http", "https"} and parsed.hostname == "localhost"
             and parsed.path == "/api/mail/graph/browser/launch")
    portal = parsed.scheme == "https" and parsed.netloc == "entra.microsoft.com"
    if not (local or portal) or parsed.username or parsed.password or parsed.query:
        raise ValueError("Unsupported Microsoft setup destination.")
    try:
        if settings.IS_MAC:
            subprocess.run(["open", url], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif settings.IS_WIN:
            os.startfile(url)
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                raise OSError("No system browser launcher")
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not open the system browser. Check that a default browser is installed, then try again.") from exc
