"""Destination selection and confined vault mutations, shared by every writer.

Source IDs are durable; a destination never changes global settings. A write
policy grants folders, not an entire filesystem. Model exposure is a separate
permission from local reading and writing.
"""
import hashlib
import os
import re
import stat
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import settings
from .filelock import locked

POLICY_FIELDS = ("read_enabled", "write_enabled", "model_exposure", "purpose",
                 "contexts", "capture_dir", "write_dirs", "protected_dirs",
                 "allow_publish", "model_exclude_dirs")
LOCK_ROOT = Path(__file__).resolve().parent.parent / "data" / "vault-locks"


def policy(row=None, primary=False):
    row = row or {}
    return {
        "read_enabled": bool(row.get("read_enabled", True)),
        "write_enabled": bool(row.get("write_enabled", primary)),
        "model_exposure": bool(row.get("model_exposure", True)),
        "model_exclude_dirs": list(row.get("model_exclude_dirs") or []),
        "purpose": str(row.get("purpose") or ""),
        "contexts": list(row.get("contexts") or []),
        "capture_dir": str(row.get("capture_dir") or "inbox"),
        "write_dirs": list(row.get("write_dirs", ["inbox", "plans", "wiki", "raw"]
                                   if primary else [])),
        "protected_dirs": list(row.get("protected_dirs") or []),
        "allow_publish": bool(row.get("allow_publish", False)),
        "policy_explicit": any(key in row for key in POLICY_FIELDS),
    }


def assert_mutation_allowed():
    if os.environ.get("VIRA_PASSIVE"):
        raise ValueError("passive test instance: vault writes are disabled")


def _specs():
    from . import vault
    return vault.source_specs()


def resolve_destination(destination=None, context=None, operation="capture",
                        for_model=False):
    """Honor explicit ID/name, then an exact configured context, then default.

    Unknown explicit/context routes fail closed. Only a legacy single writer
    retains implicit selection; multiple writers without a default need a
    destination question. Availability never changes which route was selected.
    """
    if operation != "read":
        assert_mutation_allowed()
    specs = _specs()
    chosen = None
    requested = str(destination or "").strip()
    if requested:
        requested = requested.removeprefix("@")
        matches = [s for s in specs if s["id"] == requested]
        if not matches:
            matches = [s for s in specs
                       if s["name"].casefold() == requested.casefold()]
        if len(matches) != 1:
            raise ValueError("unknown or ambiguous vault destination; choose a connected vault ID")
        chosen = matches[0]
    elif str(context or "").strip():
        key = str(context).strip().casefold()
        matches = [s for s in specs if key in
                   {str(c).casefold() for c in s.get("contexts", [])}]
        if len(matches) != 1:
            raise ValueError("no unique vault for this context; choose a destination")
        chosen = matches[0]
    else:
        default = str(settings.get("vault_default_destination") or "").strip()
        if default:
            return resolve_destination(default, operation=operation,
                                       for_model=for_model)
        writers = [s for s in specs if s.get("write_enabled")]
        if len(writers) != 1:
            raise ValueError("choose a vault destination; no unambiguous default is configured")
        chosen = writers[0]
    if not chosen["root"].is_dir():
        raise ValueError("vault destination is disconnected")
    permission = "read_enabled" if operation == "read" else "write_enabled"
    if not chosen.get(permission):
        raise ValueError("vault reading is disabled" if operation == "read"
                         else "vault destination is read-only")
    if for_model and not chosen.get("model_exposure"):
        raise ValueError("model access is disabled for this vault")
    return chosen


def relative_path(value):
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if (not raw or "\\" in raw or ":" in raw or "\x00" in raw
            or path.is_absolute() or any(p in ("..", ".") for p in raw.split("/"))
            or raw.startswith("@") or any(
                part != part.rstrip(" .") or not part
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                for part in raw.split("/"))):
        raise ValueError("use a relative vault path without traversal")
    return path.as_posix()


def _under(rel, folder, protected=False):
    folder = str(folder).rstrip("/")
    if protected:
        # Protected names must hold on case-insensitive and Unicode-normalizing
        # filesystems as well as Linux. A spelling variant is never a bypass.
        rel = unicodedata.normalize("NFC", rel).casefold()
        folder = unicodedata.normalize("NFC", folder).casefold()
    return folder in ("", ".") or rel == folder or rel.startswith(folder + "/")


def safe_path(spec, rel, operation="write"):
    """Recheck live policy and all path components before touching a file."""
    from .vault import _relative_inside

    current = next((s for s in _specs() if s["id"] == spec["id"]), None)
    if current is None or current["root"].resolve() != spec["root"].resolve():
        raise ValueError("vault destination changed or was disconnected")
    spec = current
    root = spec["root"].resolve()
    if not root.is_dir():
        raise ValueError("vault destination is disconnected")
    rel = relative_path(rel)
    if operation == "read":
        if not spec.get("read_enabled"):
            raise ValueError("vault reading is disabled")
    else:
        assert_mutation_allowed()
        if not spec.get("write_enabled"):
            raise ValueError("vault destination is read-only")
        if not any(_under(rel, d) for d in spec.get("write_dirs", [])):
            raise ValueError("path is outside this vault's writable folders")
        if any(_under(rel, d, protected=True) for d in spec.get("protected_dirs", [])):
            raise ValueError("path is in a protected vault folder")
    path = root
    for component in PurePosixPath(rel).parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("symlinks are not allowed in vault file paths")
    try:
        _relative_inside(path, root)
    except (OSError, ValueError) as exc:
        raise ValueError("path leaves the vault") from exc
    return path


def authorize_existing_path(candidate, operation="write"):
    """Apply text-vault policy to another subsystem's absolute file target.

    Image-only locations retain their own explicit approval workflow. Both
    lexical and resolved ancestry are checked so an in-vault symlink escape
    or an alternate spelling cannot turn a governed path into an unknown one.
    This authorizes scope; the caller retains its binary-file move mechanism.
    """
    assert_mutation_allowed()
    candidate = Path(candidate).expanduser()
    if not candidate.is_absolute():
        raise ValueError("an absolute file path is required")
    lexical = Path(os.path.abspath(candidate))
    resolved = lexical.resolve()
    matched = None
    for spec in _specs():
        root = Path(os.path.abspath(spec["root"]))
        for path, parent in ((lexical, root), (lexical, root.resolve()),
                             (resolved, root.resolve())):
            base_parts = [unicodedata.normalize("NFC", part).casefold()
                          for part in parent.parts]
            path_parts = [unicodedata.normalize("NFC", part).casefold()
                          for part in path.parts[:len(parent.parts)]]
            if path_parts != base_parts:
                continue
            rel = Path(*path.parts[len(parent.parts):]).as_posix()
            authorized = safe_path(spec, rel, operation=operation)
            if matched is not None and authorized.resolve() != matched.resolve():
                raise ValueError("ambiguous governed vault path")
            matched = authorized
            break
    # Keep the caller's actual path. Casefold matching is conservative on
    # case-sensitive filesystems and must never redirect an image-only file
    # into a different, similarly named text vault.
    return candidate


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# POSIX supports descriptor-relative operations. Windows retains the same
# policy and symlink checks, with before/after parent-identity checks around
# its path-based filesystem calls.
_DIR_FD = os.name == "posix" and hasattr(os, "O_NOFOLLOW")


def _lock_path(path):
    # The same file may have multiple case/Unicode spellings on macOS and
    # Windows. Serializing those aliases is conservative on Linux as well.
    canonical = unicodedata.normalize("NFC", str(path)).casefold()
    return LOCK_ROOT / hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identity(info):
    return info.st_dev, info.st_ino


class _Parent:
    """An open parent directory; mutations never follow a replaced ancestor."""
    def __init__(self, spec, rel, path, fd):
        self.spec, self.rel, self.path, self.fd = spec, rel, path, fd
        self.identity = _identity(os.fstat(fd) if fd is not None
                                  else path.parent.stat())

    def validate(self):
        current = safe_path(self.spec, self.rel)
        if _identity(current.parent.stat()) != self.identity:
            raise ValueError("vault folder changed during the write; retry")

    def _name(self, name):
        return name if self.fd is not None else self.path.parent / name

    def info(self, name):
        return os.stat(self._name(name), dir_fd=self.fd, follow_symlinks=False)

    def read(self, name):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._name(name), flags, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("vault note is not a regular file")
            return stream.read()

    def temp(self, text, mode=0o600):
        name = ".vira-" + uuid.uuid4().hex + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._name(name), flags, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), mode)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            self.unlink(name)
            raise
        return name

    def link(self, source, target):
        if self.fd is not None:
            os.link(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
        else:
            os.link(self._name(source), self._name(target))

    def replace(self, source, target):
        if self.fd is not None:
            os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(self._name(source), self._name(target))

    def unlink(self, name):
        try:
            os.unlink(self._name(name), dir_fd=self.fd)
        except FileNotFoundError:
            pass

    def rollback(self, name, committed, backup=None):
        # A concurrently created replacement is never ours to remove. On the
        # portable path, a moved parent cannot safely be reached for cleanup.
        if self.fd is None and _identity(self.path.parent.stat()) != self.identity:
            return
        try:
            if _identity(self.info(name)) != committed:
                return
        except FileNotFoundError:
            return
        if backup:
            self.replace(backup, name)
        else:
            self.unlink(name)


@contextmanager
def _parent(spec, rel, create=False):
    path = safe_path(spec, rel)
    fd = None
    try:
        if _DIR_FD:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(Path(spec["root"]).resolve(), flags)
            for part in PurePosixPath(rel).parts[:-1]:
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
        elif create:
            path.parent.mkdir(parents=True, exist_ok=True)
        parent = _Parent(spec, rel, path, fd)
        parent.validate()
        yield parent
    finally:
        if fd is not None:
            os.close(fd)


def _text_digest(raw):
    # The Reader uses universal newlines. Its sha256 must also match notes
    # created by external editors using CRLF.
    return digest(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))


def _index_source(spec, receipt):
    from . import vault
    try:
        row = next((r for r in vault._vault_rows()
                    if r["spec"]["id"] == spec["id"]), None)
        if row:
            row["vault"].scan()
    except Exception:
        receipt["index_pending"] = True
    return receipt


def write_note(spec, rel, text, expected_hash=None, create_only=True):
    """Atomic create or compare-and-swap update, serialized across runners."""
    from . import vault
    if not isinstance(text, str) or not text.strip():
        raise ValueError("note text is required")
    if len(text.encode("utf-8")) > 4_000_000:
        raise ValueError("note is too large (maximum 4 MB)")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    rel = relative_path(rel)
    if not rel.lower().endswith(".md"):
        raise ValueError("vault notes must have a .md extension")
    path = safe_path(spec, rel)
    name = path.name
    with locked(_lock_path(path)), _parent(spec, rel, create=True) as parent:
        try:
            previous = parent.info(name)
        except FileNotFoundError:
            previous = None
        if create_only and previous is not None:
            raise FileExistsError("note already exists; read it and supply its sha256 to update")
        if not create_only:
            if not expected_hash:
                raise ValueError("updates require the sha256 from the current note")
            if previous is None:
                raise ValueError("note no longer exists")
            if _text_digest(parent.read(name)) != expected_hash:
                raise ValueError("note changed since it was read; reopen it before updating")
        mode = stat.S_IMODE(previous.st_mode) if previous is not None else 0o600
        tmp, backup = parent.temp(text, mode=mode), None
        try:
            parent.validate()
            committed = _identity(parent.info(tmp))
            if create_only:
                # link creates atomically without overwriting an external
                # writer that does not participate in Vira's advisory lock.
                parent.link(tmp, name)
            else:
                if _text_digest(parent.read(name)) != expected_hash:
                    raise ValueError("note changed during the update; reopen it")
                backup = ".vira-" + uuid.uuid4().hex + ".bak"
                parent.link(name, backup)
                if (_text_digest(parent.read(backup)) != expected_hash
                        or _identity(parent.info(name)) != _identity(parent.info(backup))):
                    raise ValueError("note changed during the update; reopen it")
                parent.validate()
                parent.replace(tmp, name)
            try:
                parent.validate()
                if _identity(parent.info(name)) != committed:
                    raise ValueError("note changed during the write; reopen it")
            except (OSError, ValueError):
                # A directory renamed out of the vault during commit is
                # detected before success. Remove our new file (or restore
                # the original update) through the still-open directory.
                parent.rollback(name, committed, backup)
                raise
        finally:
            parent.unlink(tmp)
            if backup:
                parent.unlink(backup)
    receipt = {"source_id": spec["id"], "source_name": spec["name"],
               "path": vault._public_path(spec, rel), "relative_path": rel,
               "absolute_path": str(path), "sha256": digest(text)}
    # Index exactly this readable source, never invoke model/publish hooks.
    return _index_source(spec, receipt)


def capture(title, text, destination=None, context=None, for_model=False):
    spec = resolve_destination(destination, context, for_model=for_model)
    title = str(title or "").strip().replace("\n", " ")[:160]
    if not title or not str(text or "").strip():
        raise ValueError("capture title and text are required")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"
    now = datetime.now()
    rel = (f"{spec['capture_dir'].rstrip('/')}/"
           f"{now:%Y-%m-%d}-{slug}-{uuid.uuid4().hex[:8]}.md")
    body = f"# {title}\n\nCaptured: {now.isoformat(timespec='seconds')}\n\n{str(text).strip()}\n"
    return write_note(spec, rel, body)


def delete_text(spec, rel, expected_hash=None):
    rel = relative_path(rel)
    path = safe_path(spec, rel)
    with locked(_lock_path(path)):
        if not path.exists():
            return {"source_id": spec["id"], "relative_path": rel, "removed": True}
        with _parent(spec, rel) as parent:
            name, backup = path.name, ".vira-" + uuid.uuid4().hex + ".bak"
            if expected_hash and _text_digest(parent.read(name)) != expected_hash:
                raise ValueError("note changed since it was read; reopen it before deleting")
            parent.link(name, backup)
            try:
                if expected_hash and (_text_digest(parent.read(backup)) != expected_hash
                        or _identity(parent.info(name)) != _identity(parent.info(backup))):
                    raise ValueError("note changed during deletion; reopen it")
                parent.validate()
                parent.unlink(name)
                try:
                    parent.validate()
                except (OSError, ValueError):
                    # Restoring through the anchored fd cannot follow a new
                    # symlink or clobber a replacement somebody else created.
                    if parent.fd is not None:
                        try:
                            parent.link(backup, name)
                        except FileExistsError:
                            pass
                    raise
            finally:
                parent.unlink(backup)
    return _index_source(spec, {"source_id": spec["id"],
                                "relative_path": rel, "removed": True})


def update(path, text, expected_hash, destination=None, for_model=False):
    raw = str(path or "").strip()
    if raw.startswith("@"):
        sid, sep, rel = raw[1:].partition("/")
        if not sep:
            raise ValueError("invalid source-aware note path")
        if destination and resolve_destination(destination)["id"] != sid:
            raise ValueError("note path and destination disagree")
        destination = sid
    else:
        rel = raw
        # Unprefixed paths are existing primary citations, never the default.
        destination = destination or "primary"
    spec = resolve_destination(destination, operation="update", for_model=for_model)
    return write_note(spec, rel, text, expected_hash=expected_hash, create_only=False)


def destinations(for_model=False):
    """Configuration visible to the owner or model; no source bodies copied."""
    return [{k: (str(v) if isinstance(v, Path) else v) for k, v in s.items()
             if k not in ("db", "root")}
            | {"connected": s["root"].is_dir(),
               "default": s["id"] == settings.get("vault_default_destination")}
            for s in _specs() if not for_model or s.get("model_exposure")]
