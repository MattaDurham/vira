"""Keep model-call tests off the checkout's model ledgers.

suggest._run takes a slot in the admission queue and appends a receipt to
the completions ledger on every call, refused or not, and a CLI answer
teaches modelbudget the transport's limits. All three stores are module
globals under the checkout's data/, so a test that reaches suggest without
pinning them appends rows to the owner's live receipts.
"""
import tempfile
from pathlib import Path
from unittest import mock

from server import answer_runtime, modeladmission, modelbudget


def pin_model_ledgers(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for patch in (
        mock.patch.object(answer_runtime, "COMPLETIONS_STORE", root / "model-calls.jsonl"),
        mock.patch.object(modeladmission, "STORE", root / "model-admission.sqlite3"),
        mock.patch.object(modelbudget, "STORE", root / "model-limits.json"),
    ):
        patch.start()
        test.addCleanup(patch.stop)
    return root
