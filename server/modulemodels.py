"""Local model choices for the feature that is doing the work.

A request or background operation enters a module scope. The same scope feeds
both prompt budgets and model dispatch, and never rewrites the global default.
Explicit models on individual runs remain authoritative.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from . import jsonstore, models


MODULES = {
    "find": ("Find", "Chat, answers, query planning and concepts. Changing the model starts a fresh model session on the next message and retains this chat history.", "session"),
    "find-define": ("Definition", "Definitions and source explanations", "session"),
    "people": ("People", "Reply drafts, thread reads, profiles and networking", "session"),
    "attention": ("Attention", "Brief narratives and incoming work classification", "completion"),
    "journal": ("Journal", "Note extraction and follow-up runs", "session"),
    "work": ("Work", "New runs, idea tagging, lessons and work summaries", "session"),
    "applications": ("Applications", "Role scoring, draft checks and application runs", "session"),
    "reader": ("Reader", "Document tagging and reading-room updates", "session"),
    "atlas": ("World", "Circle reads and relationship explanations", "completion"),
    "subs": ("Subscriptions", "Receipt extraction and subscription update runs", "session"),
    "design": ("Design Studio", "Design analysis and new design runs", "session"),
    "evidence": ("Evidence Ledger", "Compose evidence cases", "completion"),
    "map": ("System Map", "Refresh the system map", "session"),
    "setup": ("Config", "Build initial contact dossiers and setup runs", "session"),
}
ALIASES = {"feed": "people", "find-cloud": "find", "find-related": "find",
           "brain": "find", "search": "find", "radar": "people"}
SESSION_BACKENDS = {"anthropic": ["cli"], "openai": ["cli"],
                    "google": ["api"], "xai": ["api"]}
_active = ContextVar("vira_model_module", default=None)


def canonical(module_id):
    module_id = ALIASES.get(module_id, module_id)
    if module_id not in MODULES:
        raise ValueError(f"unknown model module: {module_id}")
    return module_id


def current():
    return _active.get()


@contextmanager
def scope(module_id):
    token = _active.set(canonical(module_id) if module_id else None)
    try:
        yield
    finally:
        _active.reset(token)


def scoped(module_id):
    """Scope background/direct calls; nested helpers keep the caller's module."""
    canonical(module_id)
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if current():
                return fn(*args, **kwargs)
            with scope(module_id):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def selection(module_id, cfg=None):
    from . import suggest
    module_id = canonical(module_id)
    cfg = suggest.base_config() if cfg is None else cfg
    choices = cfg.get("module_models") or {}
    row = choices.get(module_id) if isinstance(choices, dict) else None
    if not isinstance(row, dict):
        return None
    pid, backend, model = (row.get(k) for k in ("provider", "backend", "model"))
    if pid not in models.PROVIDERS or backend not in models.PROVIDERS[pid]["config_keys"]:
        return None
    if not isinstance(model, str) or not model:
        return None
    return {"provider": pid, "backend": backend, "model": model}


def apply(cfg, module_id=None):
    module_id = module_id or current()
    picked = selection(module_id, cfg) if module_id else None
    if not picked:
        return cfg
    cfg = dict(cfg)
    cfg["ai_provider"] = picked["provider"]
    cfg["ai_backend"] = picked["backend"]
    key = models.PROVIDERS[picked["provider"]]["config_keys"][picked["backend"]]
    cfg[key] = picked["model"]
    cfg["_module_model_explicit"] = picked
    return cfg


def supported_backends(module_id):
    module_id = canonical(module_id)
    if module_id == "design":
        # Reference analysis currently hands a local image path to a CLI.
        return {"anthropic": ["cli"], "openai": ["cli"]}
    if MODULES[module_id][2] == "session":
        return SESSION_BACKENDS
    return {pid: list(spec["config_keys"]) for pid, spec in models.PROVIDERS.items()}


def save(module_id, picked):
    """Change one module atomically, preserving other modules and settings."""
    from . import suggest
    module_id = canonical(module_id)
    if picked is not None:
        pid, backend, model = (picked.get(k) for k in ("provider", "backend", "model"))
        if pid not in models.PROVIDERS:
            raise ValueError("unknown provider")
        if backend not in supported_backends(module_id).get(pid, []):
            raise ValueError(f"{MODULES[module_id][0]} does not support the {backend} transport for {pid}")
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError("choose a model id")
        if models.is_disabled(pid):
            raise ValueError(f"{models.PROVIDERS[pid]['label']} is disabled in Config")
        picked = {"provider": pid, "backend": backend, "model": model}

    def update(cfg):
        choices = cfg.get("module_models")
        choices = dict(choices) if isinstance(choices, dict) else {}
        if picked is None:
            choices.pop(module_id, None)
        else:
            choices[module_id] = picked
        cfg["module_models"] = choices
        return cfg
    jsonstore.mutate(suggest.CONFIG_PATH, update, {}, indent=2)
    return snapshot()


def _effective(cfg, module_id):
    from . import suggest
    error = ""
    try:
        pid, backend = suggest.effective_backend(cfg)
    except Exception as exc:
        pid, backend = cfg["ai_provider"], cfg["ai_backend"]
        error = str(exc)
    if MODULES[module_id][2] == "session" and not cfg.get("_module_model_explicit"):
        backend = SESSION_BACKENDS.get(pid, [backend])[0]
    key = models.PROVIDERS.get(pid, {}).get("config_keys", {}).get(backend)
    return {"provider": pid, "backend": backend,
            "model": cfg.get(key, "") if key else ""}, error


def snapshot():
    from . import suggest
    cfg = suggest.base_config()
    rows = []
    for module_id, (title, description, kind) in MODULES.items():
        picked = selection(module_id, cfg)
        default, default_error = _effective(cfg, module_id)
        if module_id == "find":
            from . import virachat
            try:
                default = virachat.default_model_selection()
            except Exception as exc:
                default_error = str(exc)
        effective, error = (_effective(apply(cfg, module_id), module_id)
                            if picked else (default, default_error))
        rows.append({"id": module_id, "title": title, "description": description,
                     "kind": kind, "windows": [module_id] + [alias for alias, target in ALIASES.items()
                                                               if target == module_id],
                     "selection": picked, "effective": effective, "default": default,
                     "error": error or default_error,
                     "supported_backends": supported_backends(module_id)})
    return {"modules": rows}


# Longest prefix first: Journal is mounted below Brief, Definition below
# the shared Find surface. Match segment boundaries, never string lookalikes.
_PATHS = {
    "/api/brief/journal": "journal", "/api/brief/note": "journal",
    "/api/define": "find-define", "/api/vira": "find",
    "/api/find": "find", "/api/search": "find", "/api/vault/ask": "find",
    "/api/omni": "find", "/api/person": "people", "/api/group": "people",
    "/api/suggest": "people", "/api/radar": "people", "/api/reconnect": "people",
    "/api/triage": "people", "/api/mail": "people", "/api/atlas": "atlas",
    "/api/brief": "attention", "/api/assistant": "attention",
    "/api/correspondence": "attention",
    "/api/applications": "applications", "/api/jobboards": "applications",
    "/api/resume": "applications", "/api/resumeview": "applications",
    "/api/ideas": "work", "/api/actions": "work", "/api/flows": "work",
    "/api/circuits": "work", "/api/routines": "work", "/api/lessons": "work",
    "/api/orphanwork": "work", "/api/showroom": "work", "/api/judge": "work",
    "/api/reading": "reader", "/api/evidence": "evidence",
    "/api/subs": "subs", "/api/subs-visuals": "subs",
    "/api/map": "map", "/api/genre": "design",
    "/api/design": "design", "/api/onboard/dossiers": "setup",
}


def module_for_path(path):
    if path.startswith("/api/frontdoor/") and path.endswith("/setup"):
        target = path.split("/")[3]
        return ALIASES.get(target, target) if ALIASES.get(target, target) in MODULES else "setup"
    return next((module_id for prefix, module_id in sorted(_PATHS.items(), key=lambda row: -len(row[0]))
                 if path == prefix or path.startswith(prefix + "/")), None)
