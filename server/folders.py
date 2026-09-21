"""The owner's folder picker, operating on the machine running Vira.

Only directory names are read. A scoped picker stays within its selected
vault, including when a child is a symlink. Browsing is safe in previews;
creating a folder is a direct owner action and is disabled in sandbox mode.
"""
import os
import stat
from pathlib import Path

from . import settings, vaultwrite


class FolderError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _directory(value: str | Path) -> Path:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise FolderError("Choose a folder from the folder browser.")
        result = candidate.resolve(strict=True)
        if not stat.S_ISDIR(result.stat().st_mode):
            raise FolderError("That location is a file. Choose a folder instead.")
        return result
    except FolderError:
        raise
    except FileNotFoundError:
        raise FolderError("That folder no longer exists. Choose another folder.", 404)
    except PermissionError:
        raise FolderError("Vira cannot open that folder. Check its access permissions or choose another folder.", 403)
    except (OSError, RuntimeError, ValueError):
        raise FolderError("That folder cannot be opened. Choose another folder.")


def _inside(path: Path, root: Path | None) -> bool:
    return root is None or path.is_relative_to(root)


def _item(path: Path, root: Path | None, name: str = "") -> dict:
    relative = path.relative_to(root).as_posix() if root else None
    reason = ""
    if relative not in (None, "."):
        try:
            if vaultwrite.relative_path(relative) != relative:
                raise ValueError("folder name would change")
        except ValueError:
            reason = ("This folder name is not supported for a vault subfolder. "
                      "Choose another folder.")
    return {
        "name": name or path.name or str(path),
        "path": str(path),
        "relative": relative,
        "selectable": not reason,
        "selection_disabled_reason": reason,
    }


def _places(root: Path | None) -> list[dict]:
    if root is not None:
        return [_item(root, root)]
    home = Path.home()
    candidates = [("Home", home)]
    candidates.extend((name, home / name) for name in ("Desktop", "Documents", "Downloads"))
    if settings.IS_MAC:
        candidates.append(("Drives", Path("/Volumes")))
    elif not settings.IS_WIN:
        candidates.extend((name, Path(path)) for name, path in
                          (("Drives", "/media"), ("Mounted folders", "/mnt")))
    places = []
    seen = set()
    for name, path in candidates:
        try:
            path = _directory(path)
        except FolderError:
            continue
        if path not in seen:
            places.append(_item(path, None, name))
            seen.add(path)
    if settings.IS_WIN:
        # Enumerate drive letters without opening every mounted drive.
        # An unavailable drive reports its error only when selected.
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drives = [Path(f"{chr(65 + i)}:/") for i in range(26) if mask & (1 << i)]
        for drive in drives:
            if drive not in seen:
                places.append(_item(drive, None, f"Drive {drive.drive}"))
    else:
        places.append(_item(Path("/"), None, "Computer"))
    return places


def _creation_disabled() -> str:
    if settings.sandboxed():
        return "Creating folders is disabled in this sandbox. Existing folders can still be selected."
    return ""


def browse(path: str = "", root: str = "", show_hidden: bool = False) -> dict:
    """List one directory, without recursion or reading any file contents."""
    boundary = _directory(root) if root else None
    current = _directory(path or boundary or Path.home())
    if not _inside(current, boundary):
        raise FolderError("Choose a folder inside this vault.", 403)
    children = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    if not show_hidden and settings.IS_WIN:
                        attrs = getattr(entry.stat(), "st_file_attributes", 0)
                        if attrs & stat.FILE_ATTRIBUTE_HIDDEN:
                            continue
                    child = Path(entry.path).resolve(strict=True)
                    if _inside(child, boundary):
                        children.append(_item(child, boundary, entry.name))
                except (OSError, RuntimeError, ValueError):
                    # A removed, unreadable, or broken child should not make
                    # all of its siblings impossible to choose.
                    continue
    except PermissionError:
        raise FolderError("Vira cannot read this folder. Check its access permissions or choose another folder.", 403)
    except OSError:
        raise FolderError("This folder is no longer available. Choose another folder.", 404)
    children.sort(key=lambda row: (row["name"].casefold(), row["name"]))
    lineage = [current]
    while lineage[-1] != boundary and lineage[-1].parent != lineage[-1]:
        lineage.append(lineage[-1].parent)
    disabled = _creation_disabled()
    return {
        **_item(current, boundary),
        "root": str(boundary) if boundary else None,
        "parent": str(current.parent) if current != boundary and current.parent != current else None,
        "ancestors": [_item(part, boundary) for part in reversed(lineage)],
        "folders": children,
        "places": _places(boundary),
        "can_create": not disabled,
        "create_disabled_reason": disabled,
    }


def create(parent: str, name: str, root: str = "") -> dict:
    """Create exactly one new child, never overwrite or create parent paths."""
    disabled = _creation_disabled()
    if disabled:
        raise FolderError(disabled, 403)
    if (not isinstance(name, str) or not name.strip() or name != name.strip()
            or name in {".", ".."} or "/" in name or "\\" in name
            or any(ord(char) < 32 for char in name)):
        raise FolderError("Enter a folder name without slashes or leading or trailing spaces.")
    if settings.IS_WIN:
        reserved = {"CON", "PRN", "AUX", "NUL"}
        reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT")
                        for number in range(1, 10))
        if (any(char in name for char in '<>:"|?*') or name.endswith(".")
                or name.split(".", 1)[0].upper() in reserved):
            raise FolderError("That folder name is not supported by Windows. Choose a different name.")
    boundary = _directory(root) if root else None
    directory = _directory(parent)
    if not _inside(directory, boundary):
        raise FolderError("Choose a folder inside this vault.", 403)
    destination = directory / name
    if destination.parent != directory:
        raise FolderError("Enter a name for one folder inside the selected location.")
    selection = _item(destination, boundary)
    if not selection["selectable"]:
        raise FolderError(selection["selection_disabled_reason"])
    try:
        destination.mkdir()
    except FileExistsError:
        raise FolderError("A folder or file with that name already exists. Choose a different name.", 409)
    except PermissionError:
        raise FolderError("Vira cannot create a folder here. Check its access permissions or choose another folder.", 403)
    except OSError:
        raise FolderError("This folder could not be created. Choose a different name or location.")
    return browse(str(destination), str(boundary) if boundary else "")
