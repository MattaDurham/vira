"""Read-only handshake for an explicitly prepared synthetic evaluation instance.

This verifies the configured fixture corpus; it is not an OS sandbox or a
claim that arbitrary shell commands cannot read outside that corpus.
"""
import hashlib
import json
import os
from pathlib import Path


def fixture_documents():
    """Canonical allowed corpus, derived from tracked synthetic inputs only."""
    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "answer_eval" / "cases.json"
    sources = json.loads(fixture.read_text(encoding="utf-8"))["sources"]
    documents, index = {}, []
    for source in sources:
        if source.get("excluded"):
            continue
        name = source["path"]
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("invalid synthetic fixture path")
        documents[name] = source["text"]
        index.append({k: source[k] for k in ("id", "path", "kind", "family")})
    documents["wiki/source-index.md"] = "# Synthetic source index\n\n" + json.dumps(index, indent=2) + "\n"
    return documents


def expected_inventory():
    return {name: hashlib.sha256(body.encode("utf-8")).hexdigest()
            for name, body in fixture_documents().items()}


def tree_hash(root):
    """Hash every file, including its relative name; refuse symlink escapes."""
    root = Path(root).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("fixture corpus contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), files


def status():
    """Return enabled only for a matching, separately configured sandbox."""
    filename = os.environ.get("VIRA_ANSWER_EVAL_MANIFEST")
    if not filename:
        return {"enabled": False, "reason": "evaluation manifest is not configured"}
    try:
        from . import modulemodels, settings
        manifest = json.loads(Path(filename).read_text(encoding="utf-8"))
        root = Path(manifest["corpus_root"]).resolve()
        fake_home = Path(manifest["sandbox_home"]).resolve()
        if (not settings.sandboxed() or settings.raw().get("fixture_mode") is not True
                or not os.environ.get("VIRA_KEYCHAIN_PREFIX")):
            raise ValueError("a sandbox with explicit fixture mode and isolated keychain is required")
        if Path.home().resolve() != fake_home or not root.is_relative_to(fake_home):
            raise ValueError("sandbox home does not match the fixture manifest")
        if Path(str(settings.get("vault_root"))).expanduser().resolve() != root:
            raise ValueError("configured vault is not the frozen fixture corpus")
        if settings.get("vault_sources") or settings.get("reader_sources"):
            raise ValueError("secondary vaults and Reader sources must be disconnected in the sandbox")
        if not Path(str(settings.get("crm_root"))).expanduser().resolve().is_relative_to(fake_home):
            raise ValueError("configured CRM must be inside the synthetic sandbox home")
        if (settings.raw().get("chat_model") != manifest["model"]
                or settings.raw().get("chat_effort") != manifest["effort"]):
            raise ValueError("chat model and effort are not explicitly pinned to the manifest")
        picked = modulemodels.selection("find")
        if picked and picked["model"] != manifest["model"]:
            raise ValueError("Find's selected model differs from the manifest")
        digest, files = tree_hash(root)
        if digest != manifest["corpus_hash"] or files != manifest["files"] or files != expected_inventory():
            raise ValueError("fixture corpus differs from its frozen manifest")
        return {"enabled": True, "fixture_only": True, "corpus_hash": digest,
                "model": manifest["model"], "effort": manifest["effort"],
                "output_policy_hash": manifest["output_policy_hash"],
                "sources": ["vault:primary"], "scope_basis": "configured corpus, not OS confinement"}
    except (KeyError, OSError, ValueError, TypeError) as exc:
        return {"enabled": False, "reason": str(exc)}
