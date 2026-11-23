"""Routing decisions: which model should answer this request, and why."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .config import PICKER_TEXT_CHARS, Config, Route
from .security import detect_sensitive


class RoutingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Decision:
    route: str
    candidates: list[str]  # first = primary, rest = fallbacks
    reasons: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)  # detector names only
    local_only: bool = False
    estimates: dict[str, dict] = field(default_factory=dict)  # model -> {"usd": ..., "p50_ms": ...} when ordered by them
    # Set only when `route_picker:` is configured and this request was classified: what the router
    # alone chose, and what the picker did ({model, instruction, outcome, route, ms}).
    router_route: str | None = None
    picker: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "route": self.route,
            "model": self.candidates[0] if self.candidates else None,
            "fallbacks": self.candidates[1:],
            "reasons": self.reasons,
            "sensitive": self.sensitive,
            "local_only": self.local_only,
        }
        if self.estimates:
            out["estimates"] = self.estimates
        if self.router_route is not None:
            out["router_route"] = self.router_route
            out["route_picker"] = self.picker
        return out


# ---------------------------------------------------------------- request facts

def _parts(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


def all_text(messages: list) -> str:
    out = []
    for m in messages:
        for p in _parts(m.get("content")):
            if p.get("type") in ("text", "input_text"):
                out.append(str(p.get("text", "")))
    return "\n".join(out)


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return "\n".join(
                str(p.get("text", "")) for p in _parts(m.get("content"))
                if p.get("type") in ("text", "input_text")
            )
    return ""


def has_images(messages: list) -> bool:
    return any(
        p.get("type") in ("image_url", "input_image")
        for m in messages for p in _parts(m.get("content"))
    )


def estimate_tokens(body: dict) -> int:
    # ~4 chars/token is a deliberately rough, dependency-free estimate; tool definitions count too.
    text = all_text(body.get("messages") or [])
    tools = json.dumps(body.get("tools") or [])
    return (len(text) + len(tools)) // 4 + int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)


def tool_names(body: dict) -> list[str]:
    """Names of the tools a request offers (OpenAI function tools), lower-cased."""
    names = []
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            if isinstance(fn.get("name"), str):
                names.append(fn["name"].lower()[:80])
    return names[:128]


def agent_key(messages: list) -> str | None:
    """A conversation key for an agent that sends no session id: the same system prompt and
    first request means the same task. Kept in memory only, never logged."""
    system = "\n".join(str(p.get("text", "")) for m in messages if m.get("role") == "system"
                       for p in _parts(m.get("content")) if p.get("type") in ("text", "input_text"))
    first = next((m for m in messages if m.get("role") == "user"), None)
    if first is None:
        return None
    first_text = "\n".join(str(p.get("text", "")) for p in _parts(first.get("content"))
                           if p.get("type") in ("text", "input_text"))
    return "agent:" + hashlib.sha256((system + "\x00" + first_text).encode("utf-8", "ignore")).hexdigest()[:24]


# ---------------------------------------------------------------- signals

def keyword_score(text: str, route: Route) -> int:
    low = text.lower()
    score = 0
    for kw in route.keywords:
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", low):
            score += 1
    for rx in route.patterns:
        if rx.search(text):
            score += 2
    return score


def tool_score(names: list[str], route: Route) -> tuple[int, list[str]]:
    """+2 for each tool whose name contains one of the route's tool keywords."""
    hits = [n for n in names if any(k in n for k in route.tool_keywords)]
    return 2 * len(hits), hits


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):  # vectors from two different embedding models: not comparable
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingsCountMismatch(ValueError):
    """The embeddings server sent back a different number of vectors than texts it was given."""


class Embedder:
    """Semantic signal via any OpenAI-compatible /embeddings endpoint (local only)."""

    def __init__(self, cfg: Config, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self._route_vecs: dict[str, list[list[float]]] | None = None

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        e = self.cfg.embeddings
        prov = self.cfg.providers[e.provider]
        headers = {}
        if prov.api_key():
            headers["Authorization"] = f"Bearer {prov.api_key()}"
        r = await self.client.post(
            f"{prov.base_url}/embeddings",
            json={"model": e.model, "input": texts},
            headers=headers,
            timeout=15.0,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        if len(data) != len(texts):  # fewer vectors would compare a route on fewer examples than it has
            raise EmbeddingsCountMismatch(
                f"{prov.base_url}/embeddings returned {len(data)} vectors for {len(texts)} texts (model {e.model})")
        return [d["embedding"] for d in data]

    async def similarities(self, text: str) -> dict[str, float]:
        if self._route_vecs is None:
            vecs: dict[str, list[list[float]]] = {}
            for name, route in self.cfg.routes.items():
                if route.examples:
                    vecs[name] = await self._embed(list(route.examples))
            self._route_vecs = vecs
        if not self._route_vecs or not text.strip():
            return {}
        [q] = await self._embed([text[:4000]])
        return {
            name: max(_cosine(q, v) for v in vs)
            for name, vs in self._route_vecs.items()
        }


ROUTE_PICKER_INSTRUCTION = 1  # bump when the text below changes: a measurement names the instruction it used
PICKER_LOAD_S = 120.0          # a call that outlives timeout_ms may run this long: it may be loading the model


class PickerNoRoute(ValueError):
    """The picker answered, but its reply named no configured route."""


class RoutePickerClient:
    """Asks a local model which route fits (config `route_picker:`).

    The request is the one the held-out test sent (docs/route-accuracy.md, "A local model picks the
    route"): the route names and descriptions as the instruction, the prompt fenced, temperature 0,
    seed 0, eight tokens. The first word of the reply that names a route wins."""

    def __init__(self, cfg: Config, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client
        self.routes = {n.lower(): n for n in cfg.routes}
        self._calls: set[asyncio.Task] = set()  # running calls: at most one (see start)

    def busy(self) -> bool:
        """A call is running: another request's, or one that outlived timeout_ms and was left to
        finish (often: the model is loading)."""
        return any(not t.done() for t in self._calls)

    def start(self, text: str) -> asyncio.Task:
        """Start the call. The caller checks busy() first and calls this with no await in between,
        so one call runs at a time: requests that arrive together don't each load a cold model.
        A call that outlives timeout_ms is not cancelled, because cancelling stops a cold model's
        load and every later call would time out too. Its answer is then dropped, its error swallowed."""
        task = asyncio.ensure_future(self.pick(text))
        self._calls.add(task)
        task.add_done_callback(self._ended)
        return task

    def _ended(self, task: asyncio.Task) -> None:
        self._calls.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved here, so a late failure nobody waits for is not logged as lost

    def system(self) -> str:
        return ("You sort messages for a router. Routes:\n"
                + "".join(f"- {n}: {r.description}\n" for n, r in self.cfg.routes.items())
                + "Reply with one word: the name of the route that fits the message best.")

    def body(self, text: str) -> dict:
        return {"model": self.cfg.route_picker.model, "temperature": 0, "seed": 0, "max_tokens": 8,
                "messages": [{"role": "system", "content": self.system()},
                             {"role": "user", "content": f"Message:\n<<<\n{text[:PICKER_TEXT_CHARS]}\n>>>\nRoute:"}]}

    def parse(self, reply: str) -> str | None:
        words = [w.strip(".,:;`'\"*") for w in reply.strip().lower().split()]
        return next((self.routes[w] for w in words if w in self.routes), None)

    async def loaded(self) -> bool | None:
        """Whether the provider has the picker's model in memory: Ollama's `GET /api/ps`, beside the
        `/v1` base URL, lists a model only once it has finished loading. None when the provider isn't
        Ollama-shaped or doesn't answer within a quarter second; a local call, like the picker's own."""
        p = self.cfg.route_picker
        base = self.cfg.providers[p.provider].base_url.rstrip("/")
        if not base.endswith("/v1"):
            return None
        try:
            r = await asyncio.wait_for(self.client.get(f"{base[:-3]}/api/ps", timeout=0.25), 0.25)
            r.raise_for_status()
            models = r.json()["models"]
            names = {str(m.get(k, "")) for m in models for k in ("name", "model")}
        except Exception:  # not Ollama's answer, or none: this only labels an outcome, never fails a request
            return None
        return p.model in names or f"{p.model}:latest" in names

    async def pick(self, text: str) -> str:
        """The route the model names. Raises on timeout, an HTTP error or a reply with no route."""
        p = self.cfg.route_picker
        prov = self.cfg.providers[p.provider]
        headers = {"Authorization": f"Bearer {prov.api_key()}"} if prov.api_key() else {}
        # timeout_ms is enforced by the caller; this cap only ends a call that was left to finish.
        r = await self.client.post(f"{prov.base_url}/chat/completions", json=self.body(text), headers=headers,
                                   timeout=PICKER_LOAD_S)
        r.raise_for_status()
        try:
            reply = r.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise PickerNoRoute("reply was not a chat completion") from e
        route = self.parse(reply if isinstance(reply, str) else "")
        if route is None:
            raise PickerNoRoute("reply named no route")
        return route


class Cooldowns:
    """Providers that asked us to wait (429/503 with Retry-After, or a rate limit at zero):
    tried last until then, never dropped. In memory: a restart forgets them."""

    MAX_S = 3600

    def __init__(self):
        self._until: dict[str, float] = {}

    def wait(self, provider: str, seconds: float | None = None, until: float | None = None) -> None:
        now = time.time()
        t = until if until is not None else now + float(seconds or 0)
        t = min(t, now + self.MAX_S)
        if t > now and t > self._until.get(provider, 0.0):
            self._until[provider] = t

    def until(self, provider: str) -> float | None:
        t = self._until.get(provider)
        if t is None:
            return None
        if t <= time.time():
            del self._until[provider]
            return None
        return t


@dataclass
class Signals:
    """What the router may use to order a route's models beyond the configured order."""
    prices: object | None = None  # trioditis_metrics.Prices: .estimate(model, tokens_in, tokens_out)
    latency: Callable[[str], int | None] | None = None  # model -> median latency in ms, recent successful calls
    cooldowns: Cooldowns | None = None


class SessionPins:
    """Keep one route per conversation so agents don't switch models mid-task."""

    def __init__(self, ttl_s: int, max_items: int = 10_000):
        self.ttl = ttl_s
        self.max = max_items
        self._d: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def get(self, sid: str) -> str | None:
        item = self._d.get(sid)
        if not item:
            return None
        route, exp = item
        if exp < time.monotonic():
            self._d.pop(sid, None)
            return None
        return route

    def set(self, sid: str, route: str) -> None:
        self._d[sid] = (route, time.monotonic() + self.ttl)
        self._d.move_to_end(sid)
        while len(self._d) > self.max:
            self._d.popitem(last=False)


# ---------------------------------------------------------------- decision

def _eligible(cfg: Config, model: str, *, vision: bool, tools: bool, tokens: int, local_only: bool) -> str | None:
    """Return None if eligible, else a short reason it was skipped."""
    m = cfg.models[model]
    prov = cfg.providers[m.provider]
    if not prov.available():
        return "no API key"
    if local_only and not prov.local:
        return "cloud model blocked (local-only)"
    if vision and "vision" not in m.capabilities:
        return "no vision"
    if tools and "tools" not in m.capabilities:
        return "no tool calling"
    if tokens > m.context_tokens:
        return "context too small"
    return None


async def decide(
    cfg: Config,
    body: dict,
    *,
    embedder: Embedder | None = None,
    pins: SessionPins | None = None,
    session_id: str | None = None,
    signals: Signals | None = None,
    picker: RoutePickerClient | None = None,
) -> Decision:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RoutingError(400, "messages must be a non-empty list")

    requested = str(body.get("model") or "auto")
    vision = has_images(messages)
    tools = bool(body.get("tools"))
    tokens = estimate_tokens(body)
    reasons: list[str] = []

    sensitive = detect_sensitive(all_text(messages), cfg.privacy)
    local_only = bool(sensitive)
    if sensitive:
        reasons.append(f"sensitive content ({', '.join(sensitive)}) -> local models only")

    # 1. Which route (or explicit model)?
    explicit_model = None
    router_route, picked = None, None
    if requested in cfg.models:
        explicit_model = requested
        route_name = "explicit"
        reasons.append(f"client asked for {requested}")
    elif requested.startswith("route/"):
        route_name = requested[6:]
        if route_name not in cfg.routes:
            raise RoutingError(404, f"unknown route {route_name!r}")
        reasons.append(f"client forced route {route_name}")
    elif requested == "auto":
        # An agent that sends no session id is still one conversation: pin it by its task.
        pin_key = session_id or (agent_key(messages) if tools else None)
        pinned = pins.get(pin_key) if (pins and pin_key) else None
        if pinned and pinned in cfg.routes:
            route_name = pinned
            reasons.append(f"session pinned to {pinned}" if session_id else f"agent conversation pinned to {pinned}")
        elif picker is not None:
            route_name, router_route, picked = await _classify_with_picker(
                cfg, last_user_text(messages), embedder, picker, reasons, tool_names(body))
            if pins and pin_key:
                pins.set(pin_key, route_name)
        else:
            route_name = await _classify(cfg, last_user_text(messages), embedder, reasons, tool_names(body))
            if pins and pin_key:
                pins.set(pin_key, route_name)
    elif named := [n for n, m in cfg.models.items() if m.upstream_id == requested]:
        # The provider's own id ("claude-haiku-5-5") pins an exact version, where a config
        # name ("claude-haiku") may move to a newer one; useful for evaluations.
        if len(named) > 1:
            raise RoutingError(400, f"model id {requested!r} is configured more than once ({', '.join(named)}); "
                                    "ask for one of those names")
        explicit_model = named[0]
        route_name = "explicit"
        reasons.append(f"client asked for {requested} (model {explicit_model})")
    else:
        raise RoutingError(404, f"unknown model {requested!r}")

    # 2. Candidate models, filtered by what this request needs.
    if explicit_model:
        preferred = [explicit_model]
    else:
        route = cfg.routes[route_name]
        preferred = list(route.models)
        if route.local_only:
            local_only = True
            reasons.append(f"route {route_name} is local-only")
    if vision:
        reasons.append("image attached -> needs vision")
    if tools:
        reasons.append("tools present -> needs tool calling")

    def filt(models):
        keep = []
        for mn in models:
            why = _eligible(cfg, mn, vision=vision, tools=tools, tokens=tokens, local_only=local_only)
            if why is None:
                keep.append(mn)
            else:
                reasons.append(f"skip {mn}: {why}")
        return keep

    candidates = filt(preferred)
    if not candidates and not explicit_model:
        # Only models some route lists, or local ones: a model configured for calling by name
        # (an expensive one, say) is never reached by surprise.
        in_routes = {mn for r in cfg.routes.values() for mn in r.models}
        others = [m for m in cfg.models if m not in preferred
                  and (m in in_routes or cfg.providers[cfg.models[m].provider].local)]
        candidates = [
            m for m in others
            if _eligible(cfg, m, vision=vision, tools=tools, tokens=tokens, local_only=local_only) is None
        ]
        if candidates:
            reasons.append("no route model fits; using another route's capable model")

    estimates: dict[str, dict] = {}
    if signals and not explicit_model and len(candidates) > 1:
        prefer = cfg.routes[route_name].prefer
        if prefer == "cheapest":
            candidates, estimates = _by_cost(cfg, body, candidates, signals, reasons)
        elif prefer == "fastest":
            candidates, estimates = _by_speed(candidates, signals, reasons)
    if signals and signals.cooldowns and len(candidates) > 1:
        candidates = _cooled_last(cfg, candidates, signals.cooldowns, reasons)

    if not candidates:
        if sensitive and cfg.privacy.on_no_local_model == "reject":
            raise RoutingError(
                422,
                "Sensitive content detected and no local model can serve this request. "
                "Remove the sensitive data or add a local model.",
            )
        raise RoutingError(503, "no configured model can serve this request")

    return Decision(
        route=route_name,
        candidates=candidates[: 1 + cfg.limits.max_fallbacks],
        reasons=reasons,
        sensitive=sensitive,
        local_only=local_only,
        estimates=estimates,
        router_route=router_route,
        picker=picked,
    )


def prompt_tokens(body: dict) -> int:
    """Rough prompt size: about four characters per token, messages plus tool definitions."""
    return (len(all_text(body.get("messages") or [])) + len(json.dumps(body.get("tools") or []))) // 4


def _by_cost(cfg: Config, body: dict, candidates: list[str], signals: Signals, reasons: list[str]):
    """Local models first ($0), then cloud models by this request's estimated cost; unpriced last."""
    tin = prompt_tokens(body)
    tout = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
    est: dict[str, dict] = {}

    def key(m: str):
        if cfg.providers[cfg.models[m].provider].local:
            est[m] = {"usd": 0.0}
            return (0, 0.0)
        usd = signals.prices.estimate(m, tin, tout) if signals.prices is not None else None
        est[m] = {"usd": usd}
        return (1, usd) if usd is not None else (2, 0.0)

    ordered = sorted(candidates, key=key)  # stable: ties keep the configured order
    unpriced = [m for m in ordered if est[m]["usd"] is None]
    reasons.append("prefer cheapest: local first, then by estimated cost"
                   + (f"; no price for {', '.join(unpriced)} (last)" if unpriced else ""))
    return ordered, est


def _by_speed(candidates: list[str], signals: Signals, reasons: list[str]):
    """By median latency of recent successful calls; models with no data yet keep their order, last."""
    est = {m: {"p50_ms": signals.latency(m) if signals.latency else None} for m in candidates}
    ordered = sorted(candidates, key=lambda m: (0, est[m]["p50_ms"]) if est[m]["p50_ms"] is not None else (1, 0))
    unknown = [m for m in ordered if est[m]["p50_ms"] is None]
    reasons.append("prefer fastest: by median latency of recent calls"
                   + (f"; no data yet for {', '.join(unknown)} (last)" if unknown else ""))
    return ordered, est


def _cooled_last(cfg: Config, candidates: list[str], cooldowns: Cooldowns, reasons: list[str]) -> list[str]:
    ready, waiting = [], []
    for m in candidates:
        until = cooldowns.until(cfg.models[m].provider)
        if until is None:
            ready.append(m)
        else:
            waiting.append(m)
            reasons.append(f"{m}: rate-limited until {time.strftime('%H:%M:%S', time.localtime(until))}, tried last")
    return ready + waiting


async def _classify(cfg: Config, text: str, embedder: Embedder | None, reasons: list[str],
                    tools: list[str] | None = None) -> str:
    kw, tool_hits = {}, {}
    for name, r in cfg.routes.items():
        ts, hits = tool_score(tools or [], r)
        kw[name], tool_hits[name] = keyword_score(text, r) + ts, hits
    best_kw = max(kw.values()) if kw else 0

    sims: dict[str, float] = {}
    if embedder is not None:
        try:
            sims = await embedder.similarities(text)
        except Exception as e:  # semantic signal is optional; never fail a request on it
            reasons.append(f"embeddings unavailable ({type(e).__name__}); keywords only")

    if best_kw > 0:
        top = [n for n, s in kw.items() if s == best_kw]
        name = max(top, key=lambda n: sims.get(n, 0.0))
        hits = tool_hits.get(name) or []
        via = f"; tools: {', '.join(hits[:6])}" if hits else ""
        reasons.append(f"keywords matched route {name} (score {best_kw}{via})")
        return name

    if sims:
        name, sim = max(sims.items(), key=lambda kv: kv[1])
        if sim >= cfg.embeddings.threshold:
            reasons.append(f"semantic match route {name} (similarity {sim:.2f})")
            return name
        reasons.append(f"best semantic match {name} {sim:.2f} below threshold")

    reasons.append(f"no strong signal -> default route {cfg.default_route}")
    return cfg.default_route


async def _late_answer(task: asyncio.Task, t0: float) -> dict:
    """What a picker call that missed timeout_ms answered once it ended: {outcome, route, ms}, ms counted
    from the call's start. Waits at most PICKER_LOAD_S, the call's own cap, and never cancels the call."""
    route, outcome = None, "timeout"
    done, _ = await asyncio.wait({task}, timeout=PICKER_LOAD_S)
    if done and task.cancelled():
        outcome = "error"
    elif done:
        try:
            route, outcome = task.result(), "chose"
        except (TimeoutError, httpx.TimeoutException):
            outcome = "timeout"
        except PickerNoRoute:
            outcome = "no_route"
        except Exception:  # only counted; the call is over either way
            outcome = "error"
    return {"outcome": outcome, "route": route, "ms": int((time.monotonic() - t0) * 1000)}


async def _classify_with_picker(cfg: Config, text: str, embedder: Embedder | None, picker: RoutePickerClient,
                                reasons: list[str], tools: list[str] | None = None, *, late: bool = False
                                ) -> tuple[str, str, dict]:
    """(route served, route the router alone chose, what the picker did). The router always runs and
    catches every picker failure; both run at once, so a fallback adds no wait beyond the picker's.
    With `late` (shadow counts only: nobody waits on them), a call that missed timeout_ms is also waited
    for, and its answer goes in `info["late"]`; the outcome and the returned routes stay as they were."""
    p = cfg.route_picker
    info: dict = {"model": p.model, "instruction": ROUTE_PICKER_INSTRUCTION, "outcome": "", "route": None, "ms": None}
    # A coding agent's tool list is a structural fact, and the held-out test had no tools: when the
    # tool names match a route's tool keywords, the router decides and the picker is not asked.
    tool_hit = next((n for n, r in cfg.routes.items() if tool_score(tools or [], r)[0] > 0), None)
    skip = ("an adopted setting has the router serve (trioditis reinforce)" if not p.serves
            else f"the request's tools match route {tool_hit}'s tool keywords" if tool_hit
            else "no text to classify" if not text.strip()
            else f"the prompt is {len(text):,} characters, over max_chars {p.max_chars:,}" if len(text) > p.max_chars
            else "another picker call is still running (one at a time; the model may be loading)" if picker.busy()
            else None)
    if skip:
        router = await _classify(cfg, text, embedder, reasons, tools)
        info["outcome"] = ("busy" if skip.startswith("another picker call")
                           else "not_serving" if not p.serves else "skipped")
        reasons.append(f"route picker not asked: {skip}")
        return router, router, info

    t0 = time.monotonic()
    task = picker.start(text)  # no await since busy(): one call at a time

    async def ask() -> tuple[str | None, str, str]:
        """(route or None, outcome, why)"""
        try:
            done, _ = await asyncio.wait({task}, timeout=p.timeout_ms / 1000)
            if not done:
                left = "left to finish; requests with no pinned route that start after it ends get the picker"
                if await picker.loaded() is False:
                    return None, "cold", f"no answer in {p.timeout_ms / 1000:g} s: the model was still loading ({left})"
                return None, "timeout", f"no answer in {p.timeout_ms / 1000:g} s ({left})"
            return task.result(), "chose", ""
        except asyncio.CancelledError:
            task.cancel()
            raise
        except (TimeoutError, httpx.TimeoutException):
            return None, "timeout", f"no answer in {PICKER_LOAD_S:g} s"
        except PickerNoRoute as e:
            return None, "no_route", str(e)
        except Exception as e:  # the picker is optional; never fail a request on it
            return None, "error", type(e).__name__
        finally:
            info["ms"] = int((time.monotonic() - t0) * 1000)

    asking = asyncio.ensure_future(ask())  # runs alongside the router
    try:
        router = await _classify(cfg, text, embedder, reasons, tools)
    except BaseException:  # the request ends here: nobody is left to use the answer
        asking.cancel()
        task.cancel()  # directly too: ask() may not have started yet, and then cancels nothing
        raise
    route, outcome, why = await asking
    info["outcome"] = outcome
    if late and outcome in ("cold", "timeout"):
        info["late"] = await _late_answer(task, t0)
    if route is None:
        reasons.append(f"route picker: {why} -> router chose {router}")
        return router, router, info
    info["route"] = route
    same = "the router alone agrees" if route == router else f"the router alone would pick {router}"
    reasons.append(f"route picker ({p.model}) chose {route} in {info['ms'] / 1000:.2f} s; {same}")
    return route, router, info
