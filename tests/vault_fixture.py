"""Keep writer tests independent of the owner's configured sources and indexes."""
from pathlib import Path
from unittest import mock

from server import settings, vault, vaultwrite


def isolate(test, root):
    root = Path(root).resolve()
    values = dict(settings.DEFAULTS)
    values.update(vault_root=str(root), vault_dirs=["wiki", "inbox", "raw", "plans"],
                  vault_sources=[], vault_default_destination="",
                  vault_primary={"write_dirs": ["wiki", "inbox", "raw", "plans",
                                                 "pending-user-deletion"]})
    for patch in (
        mock.patch.object(settings, "get", side_effect=values.get),
        mock.patch.object(vault, "DB_PATH", root.parent / "fixture-index.sqlite"),
        mock.patch.object(vaultwrite, "LOCK_ROOT", root.parent / "fixture-locks"),
    ):
        patch.start()
        test.addCleanup(patch.stop)
    return values
