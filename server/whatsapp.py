"""WhatsApp in the feed: linked-device sidecar connector. Receive-only v1 —
inbound WhatsApp messages become feed items (channel "whatsapp") joined to
CRM people by phone number; nothing is ever sent.

The protocol lives in a Node sidecar (bridge/whatsapp/, Baileys pinned by
its package-lock) that this module spawns and polls over 127.0.0.1. The
sidecar appends inbound messages to an NDJSON inbox file; the poll cursor
is a byte offset into that file, so neither side restarting loses or
re-emits messages. Message content never leaves the machine — the same
privacy boundary as chat.db.

Dormant until linked. Setup (one time): settings sheet > WhatsApp >
Connect — Vira starts the sidecar, renders its pairing QR, and the owner
scans it from WhatsApp > Settings > Linked Devices. The session lives in
data/whatsapp/session/ (git-ignored, owner-only). Deleting that directory
unlinks the device — the same "never clean this up" class as the venv.

Parallel instances share one primary-owned linked-device connector. Each
instance polls it into its own feed and keeps its own durable cursor. Pairing
can start the shared connector from either instance; only its owner may stop
it. A cross-process lock prevents simultaneous starts against one session.
"""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import channels
from . import data as crm
from . import instance, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = instance.primary_root() / "bridge" / "whatsapp"
DATA_DIR = instance.primary_root() / "data" / "whatsapp"
STATE = ROOT / "data" / "whatsapp-state.json"

_ingest_lock = threading.Lock()   # watcher tick and the poll route serialize
_last_spawn = {"t": 0.0}


def connector_dir():
    # A fixture has an independent connector until explicitly paired. Resolve
    # at use time so changing setup from fixture to real data needs no restart.
    return ROOT / "data" / "whatsapp" if settings.fixture_mode() else DATA_DIR


def connector_owner():
    return instance.id() if settings.fixture_mode() else instance.primary_id()


def bridge_port():
    if settings.fixture_mode() and instance.is_branch():
        return int(urlsplit(instance.api_url()).port) + 10000
    # A copied branch config must not silently change the shared endpoint.
    if instance.is_branch():
        try:
            cfg = json.loads((instance.primary_root() / "data" / "config.json")
                             .read_text(encoding="utf-8"))
            return int(cfg.get("whatsapp_bridge_port") or 18377)
        except (OSError, ValueError, TypeError):
            pass
    return int(settings.get("whatsapp_bridge_port"))


def linked():
    """A prior pairing exists; the connector may run."""
    return (connector_dir() / "session" / "creds.json").exists()


def installed():
    return (BRIDGE_DIR / "node_modules").is_dir()


def _bridge_get(path, timeout=4):
    url = f"http://127.0.0.1:{bridge_port()}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def sidecar_status():
    return _bridge_get("/status")


def qr():
    """Pairing QR while unlinked: {qr, png} (png is a data URL)."""
    return _bridge_get("/qr") or {"qr": None, "png": None}


def stop_sidecar():
    st = sidecar_status()
    if st is None:
        return False
    owner = st.get("owner_id") or instance.primary_id()
    if owner != instance.id():
        raise RuntimeError(f"WhatsApp connector belongs to instance {owner}; "
                           "stop it from that instance")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{bridge_port()}/stop", method="POST",
            headers={"X-Vira-Instance": instance.id()})
        with urllib.request.urlopen(req, timeout=4):
            pass
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_sidecar(wait_seconds=8):
    """Use or start the shared primary-owned connector, including for pairing."""
    st = sidecar_status()
    if st is not None:
        return st
    with locked(connector_dir() / "sidecar-start"):
        # Another instance may have started it while this one waited.
        st = sidecar_status()
        if st is not None:
            return st
        return _spawn_sidecar(wait_seconds)


def _spawn_sidecar(wait_seconds):
    if not installed():
        raise RuntimeError(
            "sidecar not installed — run: cd bridge/whatsapp && npm install")
    # Throttle respawn so a crash-looping sidecar can't be relaunched
    # every poll tick.
    now = time.time()
    if now - _last_spawn["t"] < 30:
        raise RuntimeError("sidecar not responding (respawn throttled)")
    _last_spawn["t"] = now

    data_dir = connector_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    sidecar_log = data_dir / "sidecar.log"
    cmd = [settings.get("whatsapp_node_bin"), str(BRIDGE_DIR / "sidecar.js"),
           "--port", str(bridge_port()),
           "--owner-id", connector_owner(),
           "--session-dir", str(data_dir / "session"),
           "--inbox", str(data_dir / "inbox.ndjson"),
           "--pidfile", str(data_dir / "sidecar.pid"),
           "--log", str(sidecar_log)]
    # Detached like the job runner: the linked-device connection should
    # ride through Vira restarts instead of re-handshaking every merge.
    if settings.IS_WIN:
        detach = {"creationflags": (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)}
    else:
        detach = {"start_new_session": True}
    log = open(sidecar_log, "ab")
    try:
        subprocess.Popen(cmd, cwd=str(BRIDGE_DIR), stdout=log,
                         stderr=subprocess.STDOUT, **detach)
    finally:
        log.close()
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        st = sidecar_status()
        if st is not None:
            return st
        time.sleep(0.5)
    raise RuntimeError(f"sidecar did not come up — see {sidecar_log}")


# ---------- cursor ----------

def _load_cursor():
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("cursor")
    except (OSError, json.JSONDecodeError):
        return None


def _save_cursor(cursor):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")


# ---------- message -> feed item ----------

def _digits(jid_or_pn):
    """JID or E.164 -> bare digits for CRM matching. JIDs look like
    447700900123@s.whatsapp.net or 447700900123:17@s.whatsapp.net (device
    suffix); norm_digits downstream keeps the last 10."""
    s = str(jid_or_pn or "").split("@", 1)[0].split(":", 1)[0]
    return "".join(ch for ch in s if ch.isdigit())


def _to_item(row):
    """Sidecar inbox row -> feed item, or None to skip (own sends, noise)."""
    from . import photos
    if row.get("from_me"):
        return None                     # inbound only, like the iMessage feed
    digits = _digits(row.get("sender_pn") or row.get("sender_jid"))
    if not digits:
        return None
    kind = row.get("kind") or "text"
    text = (row.get("text") or "").strip()
    if not text:
        text = {"image": "[photo]", "video": "[video]", "voice": "[voice note]",
                "audio": "[audio]", "document": "[document]",
                "sticker": "[sticker]", "location": "[location]",
                "contact": "[contact card]", "poll": "[poll]"}.get(kind, "")
    if not text:
        return None
    pid = crm.resolve_handle(digits)
    person = crm._load()["by_id"].get(pid) if pid else None
    ts = row.get("timestamp")
    when = (datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat()
            if isinstance(ts, (int, float)) and ts > 0 else None)
    return {
        "rowid": f"wa-{row.get('id') or ''}",
        "when": when,
        "channel": "whatsapp",
        "text": text[:500],
        "handle": "+" + digits,
        "group": bool(row.get("group")),
        "group_name": row.get("group_subject") or None,
        "person_id": pid,
        "person_name": person["name"] if person else (
            row.get("push_name") or "+" + digits),
        "known": pid is not None,
        "has_photo": bool(pid and photos.photo_path(pid)),
    }


def ingest(shared):
    """One poll: fetch inbox lines past the cursor, push new feed items
    (channels.push_feed_item — no notify: the phone already announces
    WhatsApp natively, the same decision as iMessage). First run
    baselines at the current end of the inbox and emits nothing old
    (channels.first_run_baseline)."""
    with _ingest_lock:
        st = sidecar_status()
        if st is None:
            raise RuntimeError("sidecar not reachable")
        cursor, baselined = channels.first_run_baseline(
            _load_cursor(), lambda: int(st.get("inbox_bytes") or 0))
        if baselined:
            _save_cursor(cursor)
            return {"ingested": 0, "cursor": cursor, "baselined": True}
        res = _bridge_get(f"/messages?after={int(cursor)}", timeout=10)
        if res is None:
            raise RuntimeError("sidecar poll failed")
        n = 0
        for row in res.get("messages", []):
            item = _to_item(row)
            if item and channels.push_feed_item(shared, item):
                n += 1
        new_cursor = int(res.get("cursor") or cursor)
        if new_cursor != cursor:
            _save_cursor(new_cursor)
        return {"ingested": n, "cursor": new_cursor}


class WhatsAppWatcher:
    """Polls the sidecar for new inbound messages and merges them into the
    shared live feed. Dormant until a pairing exists; supervises the
    shared sidecar (respawn with throttle) while linked."""

    def __init__(self, imessage_watcher, poll_seconds=None):
        self.watcher = imessage_watcher   # shared feed + listeners
        self.poll = poll_seconds or int(settings.get("whatsapp_poll_seconds"))
        self.status = {"state": "dormant", "detail": ""}
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="vira-whatsapp").start()

    def _run(self):
        while not self._stop.is_set():
            wait = self.poll
            try:
                if not linked():
                    self.status = {"state": "dormant",
                                   "detail": "not linked — connect in settings"}
                    wait = 30
                else:
                    st = ensure_sidecar()
                    if st.get("logged_out"):
                        self.status = {"state": "logged_out",
                                       "detail": "unlinked by phone — re-pair in settings"}
                        wait = 60
                    else:
                        ingest(self.watcher)
                        self.status = {
                            "state": "connected" if st.get("connected")
                            else "connecting",
                            "detail": st.get("jid") or ""}
            except Exception as e:  # noqa: BLE001 — keep polling
                self.status = {"state": "error", "detail": str(e)[:200]}
                wait = 30
            self._stop.wait(wait)
