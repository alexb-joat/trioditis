"""Is a local model server there, and does it have the model? Used by `check` and by the 502 text.

Only providers the config marks ``local: true`` are ever contacted, once each, with a short
timeout, by listing their models (``GET {base_url}/models``, the OpenAI-compatible listing that
Ollama, llama.cpp, LM Studio and vLLM all serve). Cloud providers are never contacted here.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Config, Provider

TIMEOUT_S = 2.0
CONNECT_ERRORS = ("ConnectError", "ConnectTimeout")  # httpx exception names: "nothing is listening"


def looks_like_ollama(p: Provider) -> bool:
    """The config does not name a server type: go by the provider's name or Ollama's default port."""
    return "ollama" in p.name.lower() or ":11434" in p.base_url


def start_hint(p: Provider) -> str:
    return "start it (ollama serve)" if looks_like_ollama(p) else "start the server"


@dataclass
class Listing:
    answered: bool = False
    ids: set[str] | None = None  # None: it answered, but not with a usable list of models
    error: str = ""              # exception name when it did not answer
    status: int | None = None    # HTTP status when it answered with something other than 200


def _client(**kw) -> httpx.Client:
    """The one place a probe opens a connection (tests replace it, so no test touches the network)."""
    return httpx.Client(**kw)


def probe(p: Provider, timeout: float = TIMEOUT_S, transport: httpx.BaseTransport | None = None) -> Listing:
    """One read-only request to one local provider. No proxy, no redirects."""
    headers = {"Authorization": f"Bearer {p.api_key()}"} if p.api_key() else {}
    try:
        with _client(follow_redirects=False, trust_env=False, timeout=timeout, transport=transport) as c:
            r = c.get(f"{p.base_url}/models", headers=headers)
    except httpx.HTTPError as e:
        return Listing(error=type(e).__name__)
    if r.status_code != 200:
        return Listing(answered=True, status=r.status_code)
    try:
        items = r.json().get("data")
    except (ValueError, AttributeError):
        return Listing(answered=True)
    if not isinstance(items, list):
        return Listing(answered=True)
    return Listing(answered=True, ids={m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)})


def has_model(p: Provider, listing: Listing, model_id: str) -> bool:
    ids = listing.ids or set()
    if model_id in ids:
        return True
    # Ollama treats "name" as "name:latest"
    return looks_like_ollama(p) and ":" not in model_id and f"{model_id}:latest" in ids


def local_states(cfg: Config, transport: httpx.BaseTransport | None = None) -> tuple[dict[str, str], list[str]]:
    """({model name: state text}, [footer lines]) for every configured model on a local provider."""
    listings: dict[str, Listing] = {}
    states: dict[str, str] = {}
    notes: list[str] = []
    for name, m in cfg.models.items():
        p = cfg.providers[m.provider]
        if not p.local or not p.available():  # a local server whose key is missing is not asked
            continue
        if p.name not in listings:  # once per provider
            listings[p.name] = probe(p, transport=transport)
            if not listings[p.name].answered:
                notes.append(f"{p.name} did not answer at {p.base_url} ({listings[p.name].error}): "
                             f"{start_hint(p)}, then run `trioditis check` again.")
        li = listings[p.name]
        if not li.answered:
            states[name] = f"server not answering: {start_hint(p)}"
        elif li.ids is None:
            states[name] = (f"server answers (HTTP {li.status}); model not checked" if li.status
                            else "server answers; model not checked")
        elif has_model(p, li, m.upstream_id):
            states[name] = "ready"
        elif looks_like_ollama(p):
            states[name] = f"not on this server: ollama pull {m.upstream_id}"
        else:
            states[name] = "server answers; model not checked"
    return states, notes


def connection_hint(cfg: Config, attempts: list[dict]) -> str:
    """One sentence for the 502 when a local provider could not be reached; '' otherwise."""
    seen: dict[str, Provider] = {}
    for a in attempts:
        m = cfg.models.get(a.get("model"))
        if m and a.get("error") in CONNECT_ERRORS and cfg.providers[m.provider].local:
            seen.setdefault(m.provider, cfg.providers[m.provider])
    if not seen:
        return ""
    where = ", ".join(f"{p.name} at {p.base_url}" for p in seen.values())
    return f"No answer from the local server ({where}): {start_hint(next(iter(seen.values())))}, then run `trioditis check`."


def attempts_text(attempts: list[dict]) -> str:
    """The attempts as 'model: reason, model: reason' instead of a Python list."""
    out = []
    for a in attempts:
        if a.get("status") is not None:
            why = f"HTTP {a['status']}"
        elif a.get("error") == "budget_blocked":
            why = f"budget_blocked ({a['reason']})" if a.get("reason") else "budget_blocked"
        else:
            why = str(a.get("error") or "failed")
        out.append(f"{a.get('model')}: {why}")
    return ", ".join(out)
