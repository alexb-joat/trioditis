# Changelog

## Unreleased

**Usage reports**
- `trioditis usage --outcomes`: per model, route, tool or day, the share of calls that failed, of answers cut off at the length limit, of answers asked again within 2 minutes, and of prompt tokens served from cache. Every call-based rate has a 95% interval, and `-` means no data, never zero. `--json` always includes the rates.
- Each call now records why its answer ended (`finish_reason`, for streams too), why no model answered (`error`), a fingerprint of the conversation and cached prompt tokens where the provider reports them. Older databases gain the columns on open; reports on them show the new rates as unknown.

**Plan usage**
- `trioditis plan import` keeps a ledger of what a subscription's chats and agents used: token counts from Claude Code's transcripts (`--claude-code`), `plan-usage.v1` event files (Euboulia's export), and plan meter readings (`--meter`). Counts, model, effort and salted hashes only: message content, folder names, paths and titles are never written. Without arguments it imports what the new `plan:` config section names; the Linux daily timer does that before `refit`.
- `trioditis usage --plan`: messages and tokens (uncached, cache read, cache write, out) per model and effort, or per day, source, person or subagent, and how far each meter rose in the window. Spend and caps never count plan usage.
- `scripts/effort_trial.py`: measure how heavy each reasoning effort is on your own plan. It runs your prompts through `claude -p` at each effort (tools off, your subscription, never `--bare`), then reports output tokens per prompt, the ratio between efforts with a 95% interval and how far the meter rose per batch. `blind` shuffles the answers so you can rate them without knowing the effort.

**Staying current**
- `trioditis refit` (daily through the Linux user timer): per model over 28 days, error, cut-off, asked-again and cache-hit rates with 95% intervals, p50/p95 latency and an EWMA control chart of the daily error rate. Writes `~/.hlidskjalv/trioditis-fitted.json` (each number with its count, window and method) and appends only new findings to `trioditis-notebook.jsonl`. It changes nothing the router uses.
- `facts:` in the config names a provider-facts file. `check`, the startup log and `refit` then name configured models that are deprecated, retired or retiring within 30 days, quoting the source page and its note, say when a model isn't in the facts, and list active models of your providers as candidates to test. Price differences become proposals that `prices import` applies.
- Found while checking: Anthropic's OpenAI-compatible endpoint has no prompt caching, so `cache hit` stays `-` for Claude models.

**Fixed**
- `trioditis init` writes `config.yaml` readable by you only, like `.env`. In 0.5.0 other accounts on the computer could read it, and it names your providers and local addresses. A `config.yaml` written by 0.5.0 keeps its mode: `chmod 600 config.yaml`.

## 0.5.0 (2026-10-31)

Local and cheap first, coding agents recognised, rate limits stepped around; installable as a package.

**Routing**
- Coding agents: a route's `tool_keywords` are matched against the names of the tools a request offers. The example `code` route lists `editor`, `read_file`, `run_command` and others, so Cline with `model: auto` lands on `code` instead of `quick`. An agent's later turns ("proceed") stay on the route its task started on, even without a session id.
- `prefer: cheapest` orders a route's models local first ($0), then by this request's estimated cost; `prefer: fastest` by the median latency of recent calls. `/v1/route` and `explain` show the estimates. The example config prefers cheapest on `quick` and `writing`.
- A provider that answered "too many requests" (or reported a rate limit at zero) is tried last until its wait is over, so the next requests go straight to a model that can answer.
- When no model of a route fits, the fallback takes only another route's model or a local one, never a model you call by name only.
- The token estimate counts tool definitions, as the cost estimate already did.

**Installing**
- `trioditis init` writes `config.yaml` and `.env` (owner-only) from templates shipped in the package, and never overwrites.
- The version is written once (`trioditis.__version__`). A release workflow checks the tag against it, builds the sdist and wheel from hash-pinned tools, tests the wheel in a clean venv and attaches both files to the GitHub release; PyPI publishing through trusted publishing is ready and switched on once the PyPI project exists.

**Removed**
- The pre-rename `SWITCHBOARD_*` environment names, `$ARCADE_HOME` and the `~/.arcade` folder with its one-time move.

## 0.4.0 (2026-10-30)

Ready for batch jobs: an evaluation or pipeline can pin a model version, read back what one run cost, and wait on a rate limit instead of failing.

**Spending**
- Caps hold each cloud call's estimated cost (prompt size and `max_tokens` at your prices) from the moment it starts until its real cost is recorded, so a burst of parallel requests cannot overshoot a cap. A call whose estimate would cross a cap is skipped like one over it.
- Prices can carry a `long_context` tier (for example Claude Haiku 5.5 above 100,000 prompt tokens), applied to the whole call.
- `trioditis prices import provider-facts.json`: prices from a sourced price list, matched by model id, each with its source URL and date, instead of typed by hand.

**Usage reports**
- `trioditis usage`: calls, errors, tokens and spend per model, route, provider, calling tool, session or day; `--json` for scripts. Reads the metrics database read-only and needs no config.
- Each call records the calling tool (`X-Trioditis-Client`, or the first word of `User-Agent`) and a hash of its session id.

**Models and rate limits**
- A request can name a model by its provider id (`claude-haiku-5-5`) to pin an exact version; `X-Trioditis-Upstream-Model` says which id the provider was sent.
- When every model tried answers "too many requests", the client gets `429 rate_limited` with `Retry-After` (the shortest wait asked for) instead of a 502. `budget_exceeded` keeps meaning stop. The decision log records which.
- Example config: Claude Haiku 5.5 and Sonnet 5.5 with their 1M context, and `claude-opus` (Opus 5.5) callable by name, in no route.

**Security and engineering**
- Dependencies: `opentelemetry-api` 1.45.2 (unbounded `tracestate` parsing, GHSA-xcq7-p3pf-p88h) and `certifi` 2026.10.23 (PYSEC-2026-4320).
- ruff in CI (pyflakes, bugbear, import order, pyupgrade, bandit security rules), hash-pinned like everything else. It found a JSON error that chained the decoder's traceback and a cosine similarity that silently truncated vectors of different lengths; both fixed.

## 0.3.0 (2026-10-14)

First public release. Earlier versions were private and named Switchboard.

**Security**
- Refuses web pages in your browser: a foreign `Host` (DNS rebinding) or `Origin` gets 403 and a log line, and POST bodies must be JSON.
- Request bodies are capped while they are read, not after; every 413 is logged.
- `logs/decisions.jsonl` (50 MB) and `logs/security.jsonl` (5 MB) rotate to one backup.
- Refused requests are logged to `logs/security.jsonl`.
- Hardware probes run from system directories, never from `PATH`.
- Docker base image pinned by digest.

**Works with VS Code (Cline)**
- Tested against what Cline really sends (native tool calling, a large system prompt, streamed usage): a fixture, unit tests and a live replay script (`scripts/vscode_replay.py`).
- Streamed answers are relayed decoded. A gzip-compressed upstream stream (Anthropic) used to end in "stream ended without a finish reason".
- Prompts a local model probably cut short (Ollama's context limit) are flagged in the decision log.
- Setup guide: [docs/vscode-cline.md](docs/vscode-cline.md).

**Spending and limits**
- SQLite call history; daily and monthly spending caps with automatic local fallback.
- Caps per provider; provider rate limits and account balances where the provider reports them.
- Usage is requested on streams; cloud calls without a price are recorded and can be charged at an assumed price.

**Other**
- `trioditis hardware`: detects CPU, RAM and GPU and suggests local models that fit.
- `trioditis --version`.
- Renamed from Switchboard; the old `SWITCHBOARD_API_KEY` is still read.
- CI on Linux, macOS and Windows (two Windows generations), Python 3.11 to 3.13; CodeQL and dependency review.
- Short README; reference moved to `docs/`.

## Before 0.3.0 (private)

- 0.1 (2026-10-03): the router. Privacy detectors with local-only routing, routes by keywords and local embeddings, capability filters, fallback, session pinning, hash-pinned dependencies, localhost-only binding.
- Contributor, review and security tooling; token usage and local/cloud split logged per request.
