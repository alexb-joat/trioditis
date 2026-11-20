# Changelog

## Unreleased

**Claude Code**
- Claude Code can run through Trioditis: set `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`, or start it with `scripts/claude-t`. Requests go to Anthropic unchanged with your own claude.ai login, and the answer streams back unchanged. Each call's token counts go to plan usage, merged with `plan import --claude-code`, so `usage --plan` also shows calls the transcripts leave out. Calls made with an API key count as spend. Trioditis doesn't choose the model here. [docs/claude-code.md](docs/claude-code.md)
- `/health` now says `"anthropic_gate": true`, so `claude-t` routes only to a router that has the passthrough.
- The router key is refused when it arrives as `Authorization` or `x-api-key`, bare or after a scheme word such as `Bearer`, so the usual mistake no longer sends it to Anthropic. `claude-t` refuses a `TRIODITIS_URL` that is plain `http` to another machine or has an `@` in it. [docs/security-model.md](docs/security-model.md) and `SECURITY.md` now describe the passthrough: it forwards Claude Code's own credential to Anthropic and runs no privacy detector.

**Routing**
- Opt-in `route_picker:` block: a local model picks the route for the first turn of each `auto` conversation (every turn, for a client that sends no session id and no tools). On the 315 held-out prompts `qwen2.5-coder:7b` chose the labelled route for 231 against the router's 205 (p = 0.023), but sent more quick questions to pricier routes. On a laptop CPU with the model loaded, a first turn's median wait was about 1 s at 100 characters and about 2 s at 200. Only a `local: true` provider is accepted. Off unless configured; `config.example.yaml` has the block commented out. [Study](docs/route-accuracy.md#a-local-model-picks-the-route-19-november-2026)
- The router still runs beside the picker and is used when the picker times out (`timeout_ms`, default 2500), fails or names no route. A call that times out is left to finish rather than cancelled, so a model that was loading finishes loading; one picker call runs at a time, so until that call ends, other requests skip the picker (outcome `busy`). Prompts over `max_chars`, tool lists that match a route's `tool_keywords`, forced routes, named models and pinned turns never reach it. `max_chars` defaults to 200: the test prompts were at most 206 characters, and on that laptop 2 of 20 calls passed the 2.5 s timeout at 200 characters, 7 of 20 at 250 and 12 of 20 at 300. Values over 4,000, the most the picker reads, are refused. `scripts/picker_timing.py` in the repository measures another machine.
- Each decision logs `router_route` and `route_picker` (model, instruction version, outcome, route, ms), never prompt text. `trioditis check` reports whether the picker answers and how long a call takes, and warns when the picker's first call takes longer than `timeout_ms`.
- `trioditis eval-routes --log` reads the decision log and counts, for requests classified with `route_picker:` on, how often the picker named a route, how long it took, how often it and the router disagreed and where, with a cost estimate for the disagreements. It reads files only and has no labels, so it shows disagreement, not which of the two was right.
- First half of an opt-in reinforcement loop (`reinforce:` block, absent = off). `trioditis reinforce run --dry-run` scores "should the route picker's route be served?" on your own labelled prompts and prints the verdict: the candidate passes only when it is right on more rows and the 95% interval of the paired difference stays above -5 points. `trioditis reinforce revert` (and `--all`) takes adopted values back. Adopted values live in `~/.hlidskjalv/trioditis-adopted.json`, never in `config.yaml`; the running router picks up a change without a restart, `check` lists each value with the layer that set it (default, `config.yaml` or adopted, with the date), and `explain` names an adopted value among its reasons. Not in this version: adopting (`run` without `--dry-run` refuses until the watch that reverts an adoption exists) and threshold steps proposed from traffic. [docs/staying-current.md](docs/staying-current.md#the-reinforcement-loop-opt-in-the-gate-and-the-undo-are-built-adopting-is-not-yet)
- An `embeddings:` block without `threshold` now uses 0.45, the value the example config sets and the held-out test chose (was 0.55). Configs that set `threshold` are unchanged.

## 0.6.0 (2026-11-16)

Measured rather than assumed: outcome rates per model, a ledger of plan usage, and the router's accuracy on held-out prompts. The first release on PyPI.

**Install**
- `pipx install trioditis` is now the Quick start. Installing from a clone, with every package checked against the lockfile's hashes, stays as the second option; from a clone the command is `python -m trioditis`, and the background-service scripts still need the clone.
- The docs write `trioditis ...` instead of `python -m trioditis ...`.

**Usage reports**
- `trioditis usage --outcomes`: per model, route, tool or day, the share of calls that failed, of answers cut off at the length limit, of answers asked again within 2 minutes, and of prompt tokens served from cache. Every call-based rate has a 95% interval, and `-` means no data, never zero. `--json` always includes the rates.
- Each call now records why its answer ended (`finish_reason`, for streams too), why no model answered (`error`), a fingerprint of the conversation and cached prompt tokens where the provider reports them. Older databases gain the columns on open; reports on them show the new rates as unknown.

**Plan usage**
- `trioditis plan import` keeps a ledger of what a subscription's chats and agents used: token counts from Claude Code's transcripts (`--claude-code`), `plan-usage.v1` event files (Euboulia's export), and plan meter readings (`--meter`). Counts, model, effort and salted hashes only: message content, folder names, paths and titles are never written. Without arguments it imports what the new `plan:` config section names; the Linux daily timer does that before `refit`.
- `trioditis usage --plan`: messages and tokens (uncached, cache read, cache write, out) per model and effort, or per day, source, person or subagent, and how far each meter rose in the window. Spend and caps never count plan usage.
- `trioditis refit` fits plan weights: meter points per million tokens for each model and effort, with the number of stretches behind it, a 95% interval and its weight relative to the best-measured one. Only stretches where one model and effort did nearly all the tracked work, with a still meter around them, count, because the meter is whole-percent and shared with everything untracked; the output says how much rose with nothing tracked. Plus result-signal rates (followed, regenerated, asked again, up votes) from events that record them. Weights are proposals for review, never applied.
- `scripts/effort_trial.py`: measure how heavy each reasoning effort is on your own plan. It runs your prompts through `claude -p` at each effort (tools off, your subscription, never `--bare`), then reports output tokens per prompt, the ratio between efforts with a 95% interval and how far the meter rose per batch (flagging a reading older than the batch before), and checks each run against the plan-usage ledger. `blind` shuffles the answers so you can rate them without knowing the effort.

**Staying current**
- `trioditis refit` (daily through the Linux user timer): per model over 28 days, error, cut-off, asked-again and cache-hit rates with 95% intervals, p50/p95 latency and an EWMA control chart of the daily error rate. Writes `~/.hlidskjalv/trioditis-fitted.json` (each number with its count, window and method) and appends only new findings to `trioditis-notebook.jsonl`. It changes nothing the router uses.
- `facts:` in the config names a provider-facts file. `check`, the startup log and `refit` then name configured models that are deprecated, retired or retiring within 30 days, quoting the source page and its note, say when a model isn't in the facts, and list active models of your providers as candidates to test. Price differences become proposals that `prices import` applies.
- Found while checking: Anthropic's OpenAI-compatible endpoint has no prompt caching, so `cache hit` stays `-` for Claude models.
- `trioditis eval-routes --labels FILE`: measures how often the router picks the route you meant, against a CSV of prompts and expected routes. Prints accuracy with a 95% interval, per route and as a confusion table, for keywords alone and with embeddings. With 20 or more prompts it also tries other `embeddings.threshold` values on half the labels and checks the best on the other half with an exact sign test; only a gain that is unlikely to be chance (p < 0.05) becomes a notebook proposal. It calls no chat model, applies nothing, and its one network call is to your local embeddings provider.
- The example config, and the `config.yaml` that `trioditis init` writes, now set `embeddings.threshold: 0.45` (was 0.55). On 315 held-out prompts the router picked the labelled route for 205 (65.1%) at 0.45 and for 134 (42.5%) at 0.55; the test and its decision rule were written before the prompts existed ([the study](docs/route-accuracy.md)). Your existing `config.yaml` is not changed: edit the value to adopt it. An `embeddings:` block with no `threshold` still uses 0.55.
- `eval-routes` also prints a baseline (always the default route), accuracy per `stratum` when the label file has that column, and with `--compare-threshold VALUE` tests one threshold against the config's on every row instead of running the sweep.

**Fixed**
- When an embeddings server returns fewer vectors than the texts it was sent, the router now treats that as "embeddings unavailable" and routes by keywords, and `eval-routes` prints "not graded" with the counts. Before, a route was quietly compared on fewer examples than it has.
- `trioditis check` now asks each local model server (one `GET /models`, 2 seconds) and says per model whether it is `ready`, not on that server (for Ollama: `ollama pull <id>`) or the server is not answering (with its address); it used to print `ready` for every local model even with nothing running. A cloud model with its key set reads `key set (not tested)`: `check` never contacts cloud providers. When every model fails, the 502 message lists the attempts as text (`local-coder: ConnectError`) and, if a local server did not answer, names its address and the fix.
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
