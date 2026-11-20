"""Configuration loading and validation.

Design rules (security):
- YAML is parsed with yaml.safe_load only (no arbitrary object construction).
- Secrets never live in the config file: providers name an *environment
  variable* that holds the key (``api_key_env``), and the loader refuses a
  literal ``api_key`` field.
- Upstream URLs come only from this file. Clients can never supply a URL,
  so the router cannot be turned into an open proxy (SSRF).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key_env: str | None = None
    local: bool = False  # True = data never leaves your machine/network
    # Optional: an env var holding an *admin* key, only for reading this
    # provider's account spend (see accounts.py). Never used for requests.
    admin_key_env: str | None = None
    # Streaming: ask for the usage block at the end (OpenAI's stream_options). Off for a provider
    # that rejects the option (Trioditis also retries once without it when a 400 names it).
    stream_usage: bool = True

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None

    def admin_key(self) -> str | None:
        return (os.environ.get(self.admin_key_env) or None) if self.admin_key_env else None

    def available(self) -> bool:
        """A cloud provider without its key is treated as not configured."""
        return self.api_key_env is None or self.api_key() is not None


@dataclass(frozen=True)
class Model:
    name: str  # name clients see
    provider: str
    upstream_id: str  # model id sent to the provider
    capabilities: frozenset[str] = frozenset()  # e.g. vision, tools
    context_tokens: int = 8192


@dataclass(frozen=True)
class Route:
    name: str
    description: str = ""
    models: tuple[str, ...] = ()  # preference order; later ones are fallbacks
    keywords: tuple[str, ...] = ()
    patterns: tuple[re.Pattern, ...] = ()
    examples: tuple[str, ...] = ()  # for embedding similarity
    local_only: bool = False
    tool_keywords: tuple[str, ...] = ()  # matched inside the request's tool names: a coding agent's tools say "code"
    prefer: str = "configured"  # configured (list order) | cheapest (local first, then estimated cost) | fastest


# Used when the embeddings block has no `threshold`. 0.45 won the held-out test
# in docs/route-accuracy.md (example config, nomic-embed-text); other embedding
# models score on other scales, so measure with `trioditis eval-routes`.
DEFAULT_EMBEDDINGS_THRESHOLD = 0.45


@dataclass(frozen=True)
class Embeddings:
    provider: str
    model: str
    threshold: float = DEFAULT_EMBEDDINGS_THRESHOLD
    threshold_given: bool = False  # the config names a threshold (else it is the built-in default)


@dataclass(frozen=True)
class RoutePicker:
    """A local model that picks the route for the first turn of an `auto` conversation (opt-in).
    A client that sends no session id and no tools has no conversation to pin: it is asked every turn."""
    provider: str
    model: str
    timeout_ms: int = 2500  # no answer in time: the router's route is used
    max_chars: int = 200    # longer prompts skip the picker: the accuracy test's prompts were at most 206
    #                         characters, and on a laptop CPU 2 of 20 calls passed timeout_ms at 200
    #                         characters, 7 of 20 at 250 (docs/route-accuracy.md, "Time by prompt length")
    serves: bool = True     # not a config key: False only when the reinforcement loop adopted "the router
    #                         serves" (trioditis/adopted.py); the request then gets the router's own route


PICKER_TEXT_CHARS = 4000  # how much of a prompt the picker reads: the same cut the embeddings signal makes
ROUTE_PICKER_KEYS = {"provider", "model", "timeout_ms", "max_chars"}


@dataclass(frozen=True)
class Reinforce:
    """The reinforcement loop (opt-in, docs/staying-current.md): with this block present, routing
    values the loop adopted override the config's. Absent = off, and the adopted file is ignored."""
    labels: str  # the labelled gate set: a CSV with prompt and expected_route, read on this machine only


REINFORCE_KEYS = {"labels"}


@dataclass(frozen=True)
class Privacy:
    enabled: bool = True
    builtin_detectors: bool = True
    keywords: tuple[str, ...] = ()
    patterns: tuple[re.Pattern, ...] = ()
    # What to do when a prompt is sensitive but no local model can take it.
    on_no_local_model: str = "reject"  # reject | allow_cloud


@dataclass(frozen=True)
class Limits:
    max_body_bytes: int = 20 * 1024 * 1024
    upstream_timeout_s: float = 120.0
    max_fallbacks: int = 3
    session_ttl_s: int = 1800


@dataclass(frozen=True)
class Logging:
    path: str | None = "logs/decisions.jsonl"
    log_prompts: bool = False  # off by default: logs carry metadata, not content


@dataclass(frozen=True)
class PlanSettings:
    """What `trioditis plan import` reads when run without arguments (the daily timer does that)."""
    claude_code: bool = False      # import token counts from Claude Code's transcripts
    meter_file: str | None = None  # a file of plan meter readings
    person: str | None = None      # a label put on imported events, e.g. a first name


@dataclass(frozen=True)
class Config:
    providers: dict[str, Provider]
    models: dict[str, Model]
    routes: dict[str, Route]
    default_route: str
    embeddings: Embeddings | None = None
    route_picker: RoutePicker | None = None
    reinforce: Reinforce | None = None
    privacy: Privacy = field(default_factory=Privacy)
    limits: Limits = field(default_factory=Limits)
    logging: Logging = field(default_factory=Logging)
    facts: str | None = None  # a provider-facts.v1 file: model status and retirement dates, prices
    plan: PlanSettings = field(default_factory=PlanSettings)

    def provider_of(self, model: str) -> Provider:
        return self.providers[self.models[model].provider]


def _prefer(value, route: str) -> str:
    if value not in ("configured", "cheapest", "fastest"):
        raise ConfigError(f"route {route}: prefer must be configured, cheapest or fastest")
    return value


def _check_name(kind: str, name: object) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(f"invalid {kind} name: {name!r}")
    return name


def _check_url(name: str, url: object) -> str:
    if not isinstance(url, str):
        raise ConfigError(f"provider {name}: base_url must be a string")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"provider {name}: base_url must be http(s)://host/...")
    if parsed.username or parsed.password:
        raise ConfigError(f"provider {name}: credentials in base_url are not allowed")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ConfigError(
            f"provider {name}: plain http is only allowed for local hosts; use https"
        )
    return url.rstrip("/")


def _compile(patterns: list, where: str) -> tuple[re.Pattern, ...]:
    out = []
    for p in patterns or []:
        if not isinstance(p, str) or len(p) > 500:
            raise ConfigError(f"{where}: pattern must be a string under 500 chars")
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            raise ConfigError(f"{where}: bad regex {p!r}: {e}") from e
    return tuple(out)


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    providers: dict[str, Provider] = {}
    for name, p in (raw.get("providers") or {}).items():
        _check_name("provider", name)
        if "admin_key" in p:
            raise ConfigError(f"provider {name}: put the admin key in an env var and use admin_key_env")
        if "api_key" in p:
            raise ConfigError(
                f"provider {name}: put the key in an env var and use api_key_env"
            )
        providers[name] = Provider(
            name=name,
            base_url=_check_url(name, p.get("base_url")),
            api_key_env=p.get("api_key_env"),
            admin_key_env=p.get("admin_key_env"),
            local=bool(p.get("local", False)),
            stream_usage=bool(p.get("stream_usage", True)),
        )
    if not providers:
        raise ConfigError("at least one provider is required")

    models: dict[str, Model] = {}
    for name, m in (raw.get("models") or {}).items():
        _check_name("model", name)
        if name == "auto" or name.startswith("route/"):
            raise ConfigError(f"model name {name!r} is reserved")
        if not isinstance(m.get("provider"), str) or m.get("provider") not in providers:
            raise ConfigError(f"model {name}: unknown provider {m.get('provider')!r}")
        models[name] = Model(
            name=name,
            provider=m["provider"],
            upstream_id=str(m.get("id", name)),
            capabilities=frozenset(m.get("capabilities") or []),
            context_tokens=int(m.get("context_tokens", 8192)),
        )
    if not models:
        raise ConfigError("at least one model is required")

    routes: dict[str, Route] = {}
    for name, r in (raw.get("routes") or {}).items():
        _check_name("route", name)
        rmodels = tuple(r.get("models") or [])
        for mn in rmodels:
            if mn not in models:
                raise ConfigError(f"route {name}: unknown model {mn!r}")
        if not rmodels:
            raise ConfigError(f"route {name}: needs at least one model")
        routes[name] = Route(
            name=name,
            description=str(r.get("description") or ""),  # `description:` left empty is None, not "None"
            models=rmodels,
            keywords=tuple(k.lower() for k in r.get("keywords") or []),
            patterns=_compile(r.get("patterns"), f"route {name}"),
            examples=tuple(r.get("examples") or []),
            local_only=bool(r.get("local_only", False)),
            tool_keywords=tuple(str(k).lower() for k in r.get("tool_keywords") or []),
            prefer=_prefer(r.get("prefer", "configured"), name),
        )
    default_route = raw.get("default_route")
    if default_route not in routes:
        raise ConfigError("default_route must name one of the routes")

    emb = None
    if raw.get("embeddings"):
        e = raw["embeddings"]
        if not isinstance(e.get("provider"), str) or e.get("provider") not in providers:
            raise ConfigError("embeddings.provider must name a provider")
        if not providers[e["provider"]].local:
            # Embedding the prompt sends it somewhere; keep that local.
            raise ConfigError("embeddings.provider must be a local provider")
        emb = Embeddings(e["provider"], e["model"], float(e.get("threshold", DEFAULT_EMBEDDINGS_THRESHOLD)),
                         threshold_given="threshold" in e)

    pv = raw.get("privacy") or {}
    on_no_local = pv.get("on_no_local_model", "reject")
    if on_no_local not in ("reject", "allow_cloud"):
        raise ConfigError("privacy.on_no_local_model must be reject or allow_cloud")
    privacy = Privacy(
        enabled=bool(pv.get("enabled", True)),
        builtin_detectors=bool(pv.get("builtin_detectors", True)),
        keywords=tuple(k.lower() for k in pv.get("keywords") or []),
        patterns=_compile(pv.get("patterns"), "privacy"),
        on_no_local_model=on_no_local,
    )

    lm = raw.get("limits") or {}
    limits = Limits(
        max_body_bytes=int(lm.get("max_body_bytes", Limits.max_body_bytes)),
        upstream_timeout_s=float(lm.get("upstream_timeout_s", Limits.upstream_timeout_s)),
        max_fallbacks=int(lm.get("max_fallbacks", Limits.max_fallbacks)),
        session_ttl_s=int(lm.get("session_ttl_s", Limits.session_ttl_s)),
    )
    lg = raw.get("logging") or {}
    logging = Logging(path=lg.get("path", Logging.path), log_prompts=bool(lg.get("log_prompts", False)))

    facts = raw.get("facts")
    if facts is not None and not isinstance(facts, str):
        raise ConfigError("facts must be the path of a provider-facts file")
    facts = str(Path(facts).expanduser()) if facts else None

    return Config(providers, models, routes, default_route, embeddings=emb,
                  route_picker=_parse_route_picker(raw.get("route_picker"), providers, routes),
                  reinforce=_parse_reinforce(raw.get("reinforce")),
                  privacy=privacy, limits=limits, logging=logging, facts=facts, plan=parse_plan(raw.get("plan")))


def _parse_route_picker(raw, providers: dict[str, Provider], routes: dict[str, Route]) -> RoutePicker | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("route_picker must be a mapping with provider and model, or absent (off)")
    unknown = set(raw) - ROUTE_PICKER_KEYS
    if unknown:
        raise ConfigError(f"route_picker: unknown key(s) {', '.join(sorted(unknown))}; use provider, model, timeout_ms, max_chars")
    prov = raw.get("provider")
    if not isinstance(prov, str) or prov not in providers:
        raise ConfigError(f"route_picker.provider must name a provider (got {prov!r})")
    if not providers[prov].local:
        # The picker reads every first `auto` prompt, sensitive ones included; keep that local.
        raise ConfigError(f"route_picker.provider {prov} is not local: the picker reads every first prompt, "
                          f"sensitive ones too, so it must be a provider with local: true")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("route_picker.model must be the model id on that provider, e.g. qwen2.5-coder:7b")
    timeout = raw.get("timeout_ms", RoutePicker.timeout_ms)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 60_000:
        raise ConfigError(f"route_picker.timeout_ms must be a whole number from 100 to 60000 (got {timeout!r})")
    max_chars = raw.get("max_chars", RoutePicker.max_chars)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= PICKER_TEXT_CHARS:
        raise ConfigError(f"route_picker.max_chars must be a whole number from 1 to {PICKER_TEXT_CHARS} "
                          f"(the picker reads that much of a prompt; got {max_chars!r})")
    without = [n for n, r in routes.items() if not r.description.strip()]
    if without:
        raise ConfigError(f"route {without[0]}: needs a description, because route_picker chooses from the "
                          f"route descriptions")
    return RoutePicker(prov, model.strip(), timeout, max_chars)


def _parse_reinforce(raw) -> Reinforce | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("reinforce must be a mapping with labels, or absent (off)")
    unknown = set(raw) - REINFORCE_KEYS
    if unknown:
        raise ConfigError(f"reinforce: unknown key(s) {', '.join(sorted(unknown))}; use labels")
    labels = raw.get("labels")
    if not isinstance(labels, str) or not labels.strip():
        raise ConfigError("reinforce.labels must be the path of the labelled gate set, a CSV with the columns "
                          "prompt and expected_route (e.g. ~/routing/labels.csv)")
    return Reinforce(str(Path(labels.strip()).expanduser()))


_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def parse_plan(raw) -> PlanSettings:
    if raw is None:
        return PlanSettings()
    if not isinstance(raw, dict):
        raise ConfigError("plan must be a mapping with claude_code, meter_file and person")
    unknown = set(raw) - {"claude_code", "meter_file", "person"}
    if unknown:
        raise ConfigError(f"plan: unknown key(s) {', '.join(sorted(unknown))}; use claude_code, meter_file, person")
    meter = raw.get("meter_file")
    if meter is not None and not isinstance(meter, str):
        raise ConfigError("plan.meter_file must be the path of a meter readings file")
    person = raw.get("person")
    if person is not None and not (isinstance(person, str) and _LABEL.match(person)):
        raise ConfigError("plan.person must be a short label (letters, digits, . _ -), not a sentence")
    return PlanSettings(claude_code=bool(raw.get("claude_code", False)),
                        meter_file=str(Path(meter).expanduser()) if meter else None, person=person)


def load_plan_settings(path: str | Path) -> PlanSettings:
    """Only the `plan:` section, so `plan import` works with a config that has other problems."""
    p = Path(path)
    if not p.is_file():
        return PlanSettings()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return parse_plan(raw.get("plan") if isinstance(raw, dict) else None)


def load_config(path: str | Path) -> Config:
    text = Path(path).read_text(encoding="utf-8")
    return parse_config(yaml.safe_load(text))
