"""Explicit, session-scoped model settings and tool observation hooks.

The context contains metadata, never retrieved content. Optional transformation
calls and tool output budgets can therefore use the consuming run's settings
without consulting a mutable global model choice halfway through a turn.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
from pathlib import Path
import time

_CURRENT = ContextVar("vira_answer_runtime", default=None)
_OBSERVER = ContextVar("vira_tool_observer", default=None)
COMPLETIONS_STORE = Path(__file__).resolve().parent.parent / "data" / "model-calls.jsonl"


def current():
    return copy.deepcopy(_CURRENT.get() or {})


@contextmanager
def scope(manifest=None, observer=None):
    token = _CURRENT.set(copy.deepcopy(manifest) if manifest is not None else _CURRENT.get())
    receipt = _OBSERVER.set(observer if observer is not None else _OBSERVER.get())
    try:
        yield
    finally:
        _OBSERVER.reset(receipt)
        _CURRENT.reset(token)


def manifest(provider, model=None, effort=None, seed=None, backend="cli"):
    out = copy.deepcopy(seed or {})
    out.setdefault("version", 1)
    out["requested"] = {"provider": provider, "model": model or None,
                        "effort": effort or None, "backend": backend}
    out.setdefault("effective", {"provider": provider, "backend": backend,
                                  "model": None, "effort": None,
                                  "source": "awaiting_provider"})
    out.setdefault("created_t", time.time())
    evidence = out.get("evidence_scope")
    if isinstance(evidence, (list, tuple)):
        out["evidence_scope"] = {"sources": list(evidence)}
    return out


def resolved(provider, backend, model=None, effort=None, source="configured_unconfirmed"):
    """Record what a completion transport selected without inventing attestation."""
    runtime = _CURRENT.get()
    if runtime is not None:
        runtime["effective"] = {"provider": provider, "backend": backend,
                                "model": model, "effort": effort, "source": source}


def completion_event(event, call_id, **fields):
    """Local metadata receipt; prompts, retrieved content and answers stay out."""
    from .filelock import locked
    runtime = current()
    runtime.pop("execution_lease", None)
    row = {"event": event, "call_id": call_id, "t": time.time(), "runtime": runtime, **fields}
    COMPLETIONS_STORE.parent.mkdir(parents=True, exist_ok=True)
    with locked(COMPLETIONS_STORE):
        with COMPLETIONS_STORE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@contextmanager
def tool_call(name, arguments=None):
    """Observe the exact execution, including denied/failed/partial results.

    An observer implements start_tool(name,args) and finish_tool(id,result,error).
    Adapters may provide it explicitly; tools invoked outside a session remain
    ordinary calls. Yield a tiny result holder so no raw body is retained here.
    """
    observer = _OBSERVER.get()
    ident = observer.start_tool(name, arguments or {}) if observer else None
    holder = {}
    error = None
    try:
        yield holder
    except BaseException as exc:
        error = str(exc) or type(exc).__name__
        raise
    finally:
        if observer:
            observer.finish_tool(ident, holder.get("result"), error)
