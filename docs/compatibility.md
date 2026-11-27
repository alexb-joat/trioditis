# Compatibility: what stays stable

This page lists everything you can build on: commands and flags, environment variables, HTTP endpoints and headers, error shapes and the files Trioditis writes. Each item carries one of three labels. The config file has its own page, [configuration.md](configuration.md), with the same labels.

`tests/test_contract.py` reads these lists from the code: a command, flag, variable, endpoint, header, database column or log field that is missing here, or listed here but gone from the code, fails the test suite.

## The labels

- **stable**: from 1.0.0, it keeps its name and meaning for every 1.x release. New optional flags, keys, fields and columns may be added; nothing stable is renamed, removed or made to mean something else before 2.0.
- **experimental**: it works and is tested, but its shape may still change in a minor release (1.1, 1.2 and so on). Every change is in the [CHANGELOG](../CHANGELOG.md). Today this covers `reinforce`, `refit`, `plan`, `prices`, the notebook, the route picker's shadow log, the rate-limit table and the hardware and account files.
- **internal**: there for the program or its tests. It may change in any release; don't depend on it.

**Deprecation.** A stable item is removed only in a major release. Before that it is deprecated: it keeps working, prints or logs a one-line warning that names its replacement, and the CHANGELOG says so, for at least one minor release.

**Before 1.0.** Releases until 1.0.0 are 0.9.x patches. They follow the same rule: an old name or path keeps working with a deprecation warning, so a 0.9.x upgrade never breaks a setup that worked. The data folder is the one planned change: it moves from `~/.hlidskjalv` to `~/.trioditis` (variable `TRIODITIS_HOME`), with the old path and the old `HLIDSKJALV_HOME` still read.

**Python and systems.** Trioditis needs Python 3.11 or newer. CI runs the full test suite on Python 3.11, 3.12 and 3.13 on Ubuntu, macOS, Windows Server 2022 and Windows Server 2025. The Docker image uses Python 3.12. Other versions and systems may work but are not tested.

**Messages are not part of the contract.** The wording of errors, `--help`, warnings, `check` output and the decision log's `reasons` may change in any release; exit codes, error `type` values and field names don't.

## Commands and flags

`serve` runs when no command is given. Every command has `-h` / `--help`.

| Item | Label | Notes |
|---|---|---|
| `trioditis --config` | stable | the config file (default `TRIODITIS_CONFIG`, else `config.yaml` in the current folder) |
| `trioditis --version` | stable | prints `trioditis X.Y.Z` |
| `trioditis init` | stable | writes `config.yaml` and `.env` from the templates; never overwrites |
| `trioditis init --dir` | stable | folder to set up (default: the current one) |
| `trioditis serve` | stable | runs the router |
| `trioditis serve --host` | stable | default `TRIODITIS_HOST`, else `127.0.0.1` |
| `trioditis serve --port` | stable | default `TRIODITIS_PORT`, else `8787` |
| `trioditis check` | stable | validates the config and asks local servers for their models; exits 0 whatever state the models are in |
| `trioditis explain` | stable | the routing decision as JSON; calls no model and no service |
| `trioditis explain <prompt>` | stable | the prompt to route |
| `trioditis eval-routes` | stable | route accuracy against a labelled CSV |
| `trioditis eval-routes --labels` | stable | CSV with `prompt` and `expected_route` |
| `trioditis eval-routes --log` | stable | instead of labels, counts picker/router disagreements in the decision log |
| `trioditis eval-routes --config` | stable | same as the option before the command |
| `trioditis eval-routes --dry-run` | stable | writes nothing |
| `trioditis eval-routes --compare-threshold` | stable | scores one `embeddings.threshold` against the config's |
| `trioditis eval-routes --notebook` | experimental | where a threshold proposal goes |
| `trioditis results` | stable | measures picker and router on the public held-out set; writes a JSON file |
| `trioditis results --model` | stable | the picker model on the local server |
| `trioditis results --base-url` | stable | the local server; must be on this machine |
| `trioditis results --own-labels` | stable | also score your own labelled prompts (counts only) |
| `trioditis results --out` | stable | default `trioditis-results.json` |
| `trioditis results --answers` | stable | per-prompt picker answers, by row number |
| `trioditis results --no-share` | stable | don't ask about sharing |
| `trioditis hardware` | stable | hardware and local-model suggestions; changes nothing |
| `trioditis hardware --json` | stable | machine-readable output |
| `trioditis usage` | stable | calls, tokens and spend from the metrics database |
| `trioditis usage --since` | stable | `30m`, `24h`, `7d`, `today`, `month` or `YYYY-MM-DD` (default `24h`) |
| `trioditis usage --until` | stable | same forms; default now |
| `trioditis usage --by` | stable | `model`, `route`, `provider`, `client`, `session` or `day` |
| `trioditis usage --session` | stable | one `X-Session-Id`, matched by its hash |
| `trioditis usage --db` | stable | the metrics database |
| `trioditis usage --outcomes` | stable | rates with 95% intervals instead of spend |
| `trioditis usage --json` | stable | machine-readable output |
| `trioditis usage --plan` | experimental | plan usage instead of API calls |
| `trioditis plan` | experimental | subscription (plan) usage |
| `trioditis plan import` | experimental | imports token counts and meter readings |
| `trioditis plan import <file>` | experimental | a `plan-usage.v1` events file |
| `trioditis plan import --claude-code` | experimental | token counts from Claude Code's transcripts |
| `trioditis plan import --meter` | experimental | plan meter readings |
| `trioditis plan import --db` | experimental | the metrics database |
| `trioditis plan import --dry-run` | experimental | writes nothing |
| `trioditis plan import --claude-home` | internal | hidden; for tests |
| `trioditis prices` | experimental | manages `prices.json` |
| `trioditis prices import` | experimental | prices from a provider-facts file, with sources |
| `trioditis prices import <facts>` | experimental | a `provider-facts.v1` JSON file |
| `trioditis prices import --prices` | experimental | the prices file to update |
| `trioditis prices import --dry-run` | experimental | writes nothing |
| `trioditis refit` | experimental | refits rates, latency and drift from the call record |
| `trioditis refit --days` | experimental | window in days (default 28) |
| `trioditis refit --db` | experimental | the metrics database |
| `trioditis refit --out` | experimental | the fitted file |
| `trioditis refit --notebook` | experimental | the findings log |
| `trioditis refit --prices` | experimental | the prices file to compare |
| `trioditis refit --json` | experimental | prints the fitted document |
| `trioditis refit --dry-run` | experimental | writes nothing |
| `trioditis reinforce` | experimental | the reinforcement loop (off unless the config has `reinforce:`) |
| `trioditis reinforce run` | experimental | gates each candidate change on your labelled prompts |
| `trioditis reinforce run --dry-run` | experimental | required in this version: it adopts nothing yet |
| `trioditis reinforce revert` | experimental | takes back the last adopted value |
| `trioditis reinforce revert --all` | experimental | takes back every adopted value |
| `trioditis reinforce revert --dry-run` | experimental | writes nothing |
| `trioditis migrate` | stable | moves Trioditis's files from the old shared folder (`~/.hlidskjalv`, or `$HLIDSKJALV_HOME` when `TRIODITIS_HOME` is also set) to the data folder, leaving a link at each old path; `serve` does the same when it starts; nothing to do once moved; exits 1 when it can't move yet or both folders hold the files |
| `trioditis migrate --dry-run` | stable | shows what would move; writes nothing |

**Exit codes** (stable): 0 when the command did its job. 2 when the command line is wrong (argparse's own errors, and a value such as `usage --since 3q` or `usage --by colour`) or `prices import` can't read its input file. 1 for anything else that stopped the command: a missing or invalid config, a file it couldn't write, a local model it needed and couldn't reach. One exception: `prices import` exits 1 when there is nothing to import.

## Environment variables

`TRIODITIS_*` values can also come from the `.env` file (`TRIODITIS_ENV_FILE`, default `.env` in the current folder). A variable already set in the environment wins over `.env`.

| Item | Label | Notes |
|---|---|---|
| `TRIODITIS_API_KEY` | stable | the router's own key; clients send it as `Authorization: Bearer …`. Unset: only this machine may call, with the `Host`/`Origin` checks |
| `TRIODITIS_CONFIG` | stable | default for `--config` |
| `TRIODITIS_ENV_FILE` | stable | the `.env` file to load (default `.env`) |
| `TRIODITIS_HOST` | stable | default for `serve --host` |
| `TRIODITIS_PORT` | stable | default for `serve --port` |
| `TRIODITIS_GATE_UPSTREAM` | internal | where the Claude Code passthrough sends calls (default `https://api.anthropic.com`; https only); for tests |
| `TRIODITIS_HOME` | stable | the data folder (default `~/.trioditis`; see "Files") |
| `HLIDSKJALV_HOME` | experimental | the old name of `TRIODITIS_HOME`, read only when that is unset, with a one-line warning; may be removed in a 1.x minor release |
| `CLAUDE_CONFIG_DIR` | experimental | Claude Code's own setting; `plan import --claude-code` reads transcripts there (default `~/.claude`) |

Provider keys live in variables you name yourself (`api_key_env`, `admin_key_env` in the config). Cloud calls follow `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`; calls to `local: true` providers never use a proxy.

## HTTP endpoints

With `TRIODITIS_API_KEY` set, every endpoint but `/health` needs `Authorization: Bearer <key>` (the passthrough endpoints take it in `X-Trioditis-Key`). Every `POST` needs `Content-Type: application/json` and a body under `limits.max_body_bytes`.

| Item | Label | Notes |
|---|---|---|
| `GET /health` | stable | `{"status": "ok", "anthropic_gate": true}`; needs no key |
| `GET /v1/models` | stable | OpenAI-style list: `auto`, `route/<name>` for each route, then each model whose provider is available |
| `POST /v1/chat/completions` | stable | OpenAI-style chat; routed, then passed to the chosen provider (see "Request fields") |
| `POST /v1/route` | stable | the routing decision for a chat body, without calling an answering model: `route`, `model`, `fallbacks`, `reasons`, `sensitive`, `local_only` |
| `POST /v1/messages` | stable | Claude Code passthrough to Anthropic ([claude-code.md](claude-code.md)) |
| `POST /v1/messages/count_tokens` | stable | Claude Code passthrough to Anthropic |
| `GET /v1/accounts` | experimental | each cloud provider's own view of your account, where an admin key is configured |
| `GET /v1/hardware` | experimental | hardware and local-model suggestions |

## Request fields

What Trioditis reads in a `POST /v1/chat/completions` body. Every other field goes to the provider unchanged.

| Item | Label | Notes |
|---|---|---|
| `model` | stable | `auto` (route it), `route/<name>` (force a route), a model name from the config, or a provider's own model id |
| `messages` | stable | read for routing and the privacy check; sent on unchanged |
| `stream` | stable | streams the provider's answer through |
| `stream_options` | stable | Trioditis adds `include_usage: true` for a provider with `stream_usage` on |
| `tools` | stable | tool names count toward a route's `tool_keywords`; a request with tools goes only to models with the `tools` capability |
| `max_tokens` | stable | used for the cost estimate the caps hold |
| `max_completion_tokens` | stable | same, when `max_tokens` is absent |
| `user` | stable | a session id when `X-Session-Id` is absent; never sent upstream |

## Headers

| Item | Label | Notes |
|---|---|---|
| `Authorization` | stable | request: `Bearer <TRIODITIS_API_KEY>` on the OpenAI-style endpoints |
| `X-Trioditis-Key` | stable | request: the router key on the passthrough endpoints, where `Authorization` is Anthropic's; never sent upstream (no `X-Trioditis-*` header is) |
| `X-Trioditis-Client` | stable | request: the calling tool's name for usage per tool (else the `User-Agent`) |
| `X-Session-Id` | stable | request: keeps a conversation on one route; stored only as a hash |
| `X-Trioditis-Route` | stable | response: the route used (`explicit` for a named model) |
| `X-Trioditis-Model` | stable | response: the model name from the config that answered |
| `X-Trioditis-Upstream-Model` | stable | response: the exact model id the provider was sent |
| `Retry-After` | stable | response: on a `rate_limited` 429, the seconds until a candidate is free again |

## Errors

The OpenAI-style endpoints answer errors as `{"error": {"message": "…", "type": "…"}}`. The `type` and the status are stable; the message is for people.

| Item | Label | Notes |
|---|---|---|
| `router_error` | stable | 400 bad body, 404 unknown route or model, 413 body too large, 415 wrong content type, 422 sensitive prompt and no local model, 503 no configured model can serve it |
| `auth_error` | stable | 401 missing or wrong key; 403 foreign `Host` or `Origin` when no key is set |
| `rate_limited` | stable | 429, every candidate was rate-limited; with `Retry-After` |
| `budget_exceeded` | stable | 429, a cost cap would be crossed and no local model can serve it |
| `upstream_error` | stable | 502, every candidate failed; inside a broken stream, an error event with `"code": "stream_broken"` |
| `authentication_error` | stable | passthrough only, in Anthropic's shape `{"type": "error", "error": {…}}`: 407 for a missing or wrong `X-Trioditis-Key` |

An error the provider itself returned (a 400, say) is passed through with its own body and status.

## Files

The data folder is `$TRIODITIS_HOME`, default `~/.trioditis`. An install from before 0.9.1 whose Trioditis files are still in `~/.hlidskjalv`, with no `~/.trioditis` yet, keeps using `~/.hlidskjalv` until `trioditis migrate` (or the next `serve` start) moves them. Log files sit where `logging.path` points (default `logs/` under the folder `serve` runs in). On Linux and macOS, `metrics.db` and the log files are readable by your user only (mode 600).

| Item | Label | Notes |
|---|---|---|
| `metrics.db` | stable | written by Trioditis, read by dashboards; schema under "Metrics database" |
| `budget.json` | stable | your cost caps; Trioditis only reads it. Keys: `daily_usd` (default 1), `monthly_usd` (default 15), `providers.<name>.daily_usd` and `.monthly_usd`, `assume_unpriced_usd` (0), `assume_output_tokens` (1024). See [spending-and-limits.md](spending-and-limits.md) |
| `prices.json` | stable | USD per million tokens by model name: `input`, `output`, optional `long_context` with `threshold`, `input`, `output`; keys starting with `_` are ignored |
| `decisions.jsonl` | stable | one line per chat request; fields under "Decision log". Rotated at 50 MB to `decisions.jsonl.1` |
| `security.jsonl` | stable | refused requests, next to the decision log: `ts`, `method`, `path`, `client`, `reason`; never keys or bodies. Rotated at 5 MB |
| `trioditis-results.json` | stable | what `results` writes; its `schema` field names the format version |
| `shadow.jsonl` | experimental | the route picker's shadow verdicts on Claude Code sessions, next to the decision log |
| `trioditis-fitted.json` | experimental | written by `refit` |
| `trioditis-notebook.jsonl` | experimental | findings and proposals from `refit` and `eval-routes` |
| `trioditis-adopted.json` | experimental | routing values the reinforcement loop adopted |
| `hardware.json` | experimental | what `/v1/hardware` last found; dashboards such as Lunete read it |
| `accounts.json` | experimental | what `/v1/accounts` last found; dashboards such as Lunete read it |
| `plan-salt` | internal | the salt for plan-usage hashes |
| `migrate.log` | internal | one line per file `trioditis migrate` moved |

## Metrics database

`metrics.db` is SQLite. In 1.x, tables and columns are only added: none is renamed, dropped or changes meaning. Open it read-only from other programs.

| Item | Label | Notes |
|---|---|---|
| `calls.id` | stable | row id |
| `calls.ts` | stable | when the call was recorded, in Unix seconds |
| `calls.route` | stable | the route used; `explicit` for a named model, `passthrough` for Claude Code calls billed per token |
| `calls.model` | stable | the model name that answered, or `none` |
| `calls.provider` | stable | its provider |
| `calls.status` | stable | the HTTP status returned to the client |
| `calls.error` | stable | why no model answered: `rate_limited`, `budget_exceeded`, `upstream_error`, `stream_broken` |
| `calls.latency_ms` | stable | the whole request, in milliseconds |
| `calls.fallbacks` | stable | how many candidates failed before the answer |
| `calls.sensitive` | stable | the privacy detectors that fired, as a JSON list (`[]` for none) |
| `calls.local` | stable | 1 when a local model answered |
| `calls.tokens_in` | stable | prompt tokens as the provider reported them |
| `calls.tokens_out` | stable | answer tokens as the provider reported them |
| `calls.cached_tokens` | stable | prompt tokens read from the provider's cache, if reported |
| `calls.cost` | stable | USD, from `prices.json` (0 for local) |
| `calls.unpriced` | stable | 1 for a cloud call with no price |
| `calls.finish_reason` | stable | as the provider said: `stop`, `length`, `tool_calls` and so on |
| `calls.session_fp` | stable | a hash of the session id, never the id |
| `calls.prompt_fp` | stable | a hash of the conversation text, never the text |
| `calls.client` | stable | the calling tool's name |
| `provider_limits.provider` | experimental | rate-limit headers last seen; dashboards such as Lunete read them |
| `provider_limits.name` | experimental | |
| `provider_limits.lim` | experimental | |
| `provider_limits.remaining` | experimental | |
| `provider_limits.reset_at` | experimental | |
| `provider_limits.seen` | experimental | |
| `plan_usage.id` | experimental | plan usage, filled by `trioditis plan import` |
| `plan_usage.at` | experimental | |
| `plan_usage.service` | experimental | |
| `plan_usage.source` | experimental | |
| `plan_usage.person` | experimental | |
| `plan_usage.agent` | experimental | |
| `plan_usage.task` | experimental | |
| `plan_usage.model` | experimental | |
| `plan_usage.effort` | experimental | |
| `plan_usage.speed` | experimental | |
| `plan_usage.tokens_in` | experimental | |
| `plan_usage.tokens_out` | experimental | |
| `plan_usage.cache_read` | experimental | |
| `plan_usage.cache_write` | experimental | |
| `plan_usage.tokens_measured` | experimental | |
| `plan_usage.thread_fp` | experimental | |
| `plan_usage.thread_tokens` | experimental | |
| `plan_usage.thread_turns` | experimental | |
| `plan_usage.suggested` | experimental | |
| `plan_usage.followed` | experimental | |
| `plan_usage.asked_again` | experimental | |
| `plan_usage.regenerated` | experimental | |
| `plan_usage.vote` | experimental | |
| `plan_meter.at` | experimental | plan meter readings |
| `plan_meter.meter` | experimental | |
| `plan_meter.model` | experimental | |
| `plan_meter.percent` | experimental | |
| `plan_meter.resets_at` | experimental | |
| `plan_files.path_fp` | experimental | files already imported (a hash of the path) |
| `plan_files.size` | experimental | |
| `plan_files.mtime` | experimental | |

## Decision log

`decisions.jsonl` holds one JSON object per chat request. Fields may be added; the ones below keep their names and meaning. Fields marked "when …" appear only then.

| Item | Label | Notes |
|---|---|---|
| `ts` | stable | local time, `YYYY-MM-DDTHH:MM:SS±HHMM` |
| `route` | stable | the route used (`explicit` for a named model) |
| `model` | stable | the model that answered, or null |
| `status` | stable | the HTTP status returned to the client |
| `failed_attempts` | stable | one object per candidate that failed: `model`, plus `status` or `error` |
| `latency_ms` | stable | the whole request |
| `reasons` | stable | why this route and model, as text (the wording may change) |
| `sensitive` | stable | the detectors that fired, or an empty list |
| `prompt_fp` | stable | a hash of the conversation text |
| `prompt_chars` | stable | its length in characters |
| `session_fp` | stable | a hash of the session id, or null |
| `client` | stable | the calling tool's name |
| `local` | stable | whether a local model answered |
| `usage` | stable | the provider's usage block, as sent |
| `error` | stable | when no model answered: `rate_limited`, `budget_exceeded`, `upstream_error`, `stream_broken` |
| `finish_reason` | stable | when the provider gave one |
| `usage_missing` | stable | when a priced cloud answer had no usage and was charged its estimate |
| `prompt_truncated` | stable | when the provider reported under half the prompt tokens sent (of at least 1,000): the prompt was probably cut at the model's context window |
| `prompt` | stable | the conversation text, only with `logging.log_prompts: true` |
| `router_route` | stable | with `route_picker:` on, the route the router alone chose |
| `route_picker` | experimental | with `route_picker:` on, what the picker did |
