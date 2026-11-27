# Changelog

## Unreleased

**Added**
- An unknown key in `config.yaml` (a typo such as `upstream_timeout` for `upstream_timeout_s`) is still ignored, but `trioditis check` and the server's start now print a warning that names it and the nearest known key.
- `trioditis check` warns when a provider marked `local: true` has a public address. Sensitive prompts may go to local providers, so the flag must be true; private ranges (`10.0.0.0/8`, `192.168.0.0/16`, Tailscale's `100.64.0.0/10` and the like) count as your network. Only the host name is resolved; nothing is sent to the provider for this.
- The README has upgrade and uninstall steps: what to run for pipx, a clone and Docker, and the four places Trioditis writes to, so an uninstall leaves nothing behind.
- `limits.total_timeout_s` (default 240) caps how long one request waits for an answer to start, fallbacks included. Before, a provider that never answered cost the full `upstream_timeout_s` (120 s) for each of up to four models, about 8 minutes before the 502. Now the model still silent at the deadline is given up and the rest are not tried; `failed_attempts` and the 502 name them as `total_timeout`. A stream that has started is not cut. The Claude Code passthrough (`/v1/messages`) is unchanged.

**Changed**
- `pyproject.toml` now states lower bounds: fastapi 0.133.0, starlette 1.3.1, uvicorn 0.30.0, httpx 0.27.0, PyYAML 6.0.1. These are the oldest versions the test suite passes on, and starlette 1.3.1 is the first with no known advisory (pip-audit). starlette is listed itself because Trioditis imports it. A new CI job installs exactly these versions on Python 3.11 and runs the tests and the server smoke test. An older environment that `pip` used to accept now gets these versions or newer.
- CI builds the Docker image and runs it as `docker-compose.yml` does (read-only, no capabilities, as the host user, with a pre-0.9.1 volume to move): `scripts/docker_smoke.py`, which you can also run yourself. Linux only; Docker Desktop on macOS and Windows stays untested.
- The PyPI page lists the Python versions and systems CI tests (3.11–3.13; Linux, macOS, Windows), "Development Status :: 4 - Beta" while on 0.9.x, and links to the docs and this changelog.
- `--help`, the README and the docs now mark `plan`, `prices`, `refit` and `reinforce` as experimental, as `docs/compatibility.md` already did: they work and are tested, but may still change in a minor release.
- The example config and `trioditis hardware` suggestions give local models `context_tokens: 16384` (was 32000), the `OLLAMA_CONTEXT_LENGTH` the docs tell Ollama to serve. With 32000, a prompt longer than the window Ollama actually serves was routed there and cut without an error; now Trioditis skips that model with a reason. If you raised `OLLAMA_CONTEXT_LENGTH`, raise `context_tokens` with it. The docs no longer say Ollama's default window is always 4,096: Ollama 0.40 picks 4k, 32k or 256k by GPU memory, and `ollama ps` shows it.
- A request whose models all stay silent now fails after 240 s instead of up to about 8 minutes. If your models are slow to start and you relied on the longer wait, raise `limits.total_timeout_s`.

**Fixed**
- Docker on Linux: the container ran as the image's user 10001, which can't write a `logs/` folder the host user owns (or one Docker created as root), so the first start ended in a traceback. Set `TRIODITIS_UID` and `TRIODITIS_GID` in `.env` and `docker-compose.yml` runs the container as you; you can then read `logs/` from the host too. Unset, nothing changes. Any start that can't write its log now says which file, the user id and the fix instead of a traceback.
- A decision-log line that can't be written (a full disk, a folder without write permission) no longer turns an answer the provider already gave, often a paid one, into a 500, or breaks the end of a stream. The answer goes out, metrics.db still records the call, and the server prints one warning until the log can be written again.

## 0.9.1 (2026-11-27)

Trioditis gets its own data folder, `~/.trioditis`, and `docs/compatibility.md` writes down what 1.0 will keep stable.

**Upgrading from 0.9.0 or earlier:** Trioditis's files move from `~/.hlidskjalv` to `~/.trioditis` the first time `serve` starts (or with `trioditis migrate`), and a link stays at each old path; rename `HLIDSKJALV_HOME` to `TRIODITIS_HOME`. Steps, Docker and Windows: [docs/deployment.md](docs/deployment.md#upgrading-from-090-or-earlier).

**Added**
- `docs/compatibility.md` lists every command and flag, environment variable, HTTP endpoint, header, error type, file, metrics-database column and decision-log field, each labelled stable, experimental or internal, with the deprecation rule and the tested Python versions and systems.
- `docs/configuration.md` documents every `config.yaml` key with its default and label.
- SECURITY.md has a table of supported versions (0.9.x).
- A contract test reads these lists from the code: an undocumented flag, key, variable, endpoint, header, column or log field fails the test suite, and so does a documented one that is gone.
- `TRIODITIS_HOME` names the data folder, default `~/.trioditis`.
- `trioditis migrate` moves Trioditis's own files (metrics.db, budget.json, prices.json and the rest) out of the folder it shared with the dashboard, `~/.hlidskjalv`, into `~/.trioditis`, and leaves a link at each old path so dashboards that read the old paths keep working. The dashboard's files stay where they are. `serve` does the same when it starts. `--dry-run` shows the move first. If Trioditis is still writing to metrics.db, or both folders already hold the same file, nothing moves and the message says why. On Windows nothing moves yet; Trioditis keeps reading the old folder. `check` says when files are still waiting in the old folder.

**Changed**
- After the move, edit `budget.json` and `prices.json` in `~/.trioditis`: an editor that saves by replacing the file would cut the link at the old path (`check` then reports both copies). Trioditis's own writers follow the link.
- A folder that holds only the dashboard's files is no longer taken for Trioditis's data folder: a fresh install next to it starts `~/.trioditis`.
- The Docker image keeps the data folder in `/app/logs/trioditis` (`TRIODITIS_HOME`). On a volume from an earlier image, the first start moves the files from `/app/logs/hlidskjalv` and leaves relative links there, so they also resolve from the host's `./logs`. A new volume gets only `trioditis/`.

**Deprecated**
- `HLIDSKJALV_HOME`: still read when `TRIODITIS_HOME` is unset, with a one-line warning. Rename it to `TRIODITIS_HOME` (same value).

## 0.9.0 (2026-11-27)

Fixes from a readiness check before Trioditis is announced: sensitive text in tool calls stays local, a refused cloud key falls back to the next model, broken streams and malformed requests get clear errors.

**Fixed**
- The privacy detectors now also read the arguments of earlier tool and function calls, and text parts of any type. Before, a card number that appeared only in an agent's earlier tool call could reach a cloud model. Images, files and audio are still not read; the README's Known limits says so.
- A cloud provider that refuses its key (401, 403) or wants payment (402) no longer ends the request: the next model answers, as for an outage, so a mistyped key doesn't stop the local fallback. When nothing answers, the error names the key's variable to check (never its value).
- A priced cloud answer that comes back without token usage is charged its estimate instead of $0, and its log line says `usage_missing`. A cloud answer with neither usage nor a price is counted as unpriced.
- A streamed answer that breaks halfway now ends with an error event and `[DONE]`, and is logged as a failed call (502, `stream_broken`) instead of with the upstream's 200.
- A malformed request (a message that isn't an object, content that isn't text or a list, a `max_tokens` that isn't a number, `tools` that isn't a list) gets a 400 that names the field, not a 500.

**Changed**
- `check`, `serve` and `explain` without a config now print where they looked and how to fix it (`trioditis init --dir`, `cd`, or `--config`).
- `check` shows the embeddings model's state and the `ollama pull` command when it's missing. Without it the router picks routes from keywords alone (116 of 315 held-out prompts instead of 205).

**Docs**
- The spending page says the Claude Code passthrough is metered but never refused by the caps. The README says `serve` reads `config.yaml` and `.env` once, at start.

## 0.8.1 (2026-11-27)

Trioditis itself is unchanged apart from the version number. PyPI shows the README as it was at the latest release, so this release brings the new first screen there.

**Docs**
- The README's first screen says who Trioditis is for, shows real `trioditis explain` output for a coding question and for a prompt with a card number, and points at `trioditis results`. A test checks that every line of that output is what `explain` prints.
- "Why Trioditis" is a list instead of a four-column table, so it reads on a phone, and says about 8,600 lines of Python (8,562 counted; it said 8,200).
- `docs/community-results.csv` has its first row, from a result shared through the form (issue #22).

## 0.8.0 (2026-11-27)

**Added**
- `trioditis results` measures the route picker and the router on your machine: the picker on the 315 held-out prompts (now shipped with the package), the router at the shipped settings on the same prompts, and the picker's waiting time at 100 to 2,000 characters. It uses the shipped config, not yours, so results from different machines measure the same thing; you choose the picker model (`--model`, default `qwen2.5-coder:7b`). With `--own-labels FILE` it also runs both on your labelled prompts and reports counts only. Everything goes to the local server at `--base-url`; an address not on this machine is refused.
- It writes `trioditis-results.json` (schema `trioditis-results.v1`) and prints it in full: numbers, model names and digests, versions, a hardware summary and the date. Never prompt text, file paths, user or host names, or times finer than the date. Then it asks once whether to share the numbers on GitHub, default No. Yes opens the form in your browser with the numbers filled in; Trioditis itself sends nothing, and you press Submit. It doesn't ask with `--no-share` or outside a terminal.
- A "Share results" issue form. When you answer yes, `trioditis results` opens it with your numbers filled in; you read them and press Submit, and nothing is sent before that. The numbers are public and CC0, and the form asks you to tick a box saying so.
- `scripts/collect_results.py` (maintainer) checks each shared result as untrusted data: the consent box, the exact keys and types, counts that add up, the shipped held-out set. It works out each 95% interval and the sign test again, holds the counts to the 315 shipped prompts route by route, and leaves out hidden or control characters, repeated keys and numbers too long to read. Each one that passes becomes a row of `docs/community-results.csv`. It shows the rows first and writes them only with `--write`; one bad submission never stops the others, and the sheet is replaced in one step, so a failed write leaves it whole.

- `trioditis results` stops with a plain message, writes no file and asks nothing when the run cannot be a measurement: no answer from the server, an HTTP error for the picker model (only a 404 suggests `ollama pull`), a reply that is not a chat completion, or more than half of the picker's calls failing even after a retry. A few failed calls are counted as wrong and named in a Note under the numbers. A bad `--out` or `--own-labels` is found before the first call, and if the file cannot be written at the end its contents are printed first.
- `trioditis results` grades the router from the vectors its embeddings check already holds and asks the server nothing more, so a server that drops out after the check can no longer turn the router row into keyword answers without a word. It refuses an address it cannot read (a port that is not a number) before any call, tells a wrong address from a missing model on HTTP 404, never writes `--out` or `--answers` over the `--own-labels` file, names failed calls on your own prompts in the Note, and writes the sign-test p-value to 6 significant digits, so a clear difference is no longer written as 0.
- `trioditis results --answers FILE` also writes the route the picker named for each of the 315 public held-out prompts, by row number (schema `trioditis-answers.v1`: the held-out fingerprint, the picker model, the version and the rows, nothing else), so two models can be compared prompt by prompt. Never your own labelled prompts, never prompt text, not part of the results file or the share form. Off unless asked for. The bench workflow keeps it with each run.
- A results file from a local server that is not Ollama (LM Studio, llama.cpp and the like have no `/api/version`, so `models.server` is null and there are no digests) is no longer left out of the community sheet. `scripts/collect_results.py` checks it like every other submission and writes `unknown server` in the `server` cell, so it is never taken for an Ollama row; a null server that still carries a version or a digest is left out. `trioditis results` says at the end when the file will be counted that way.
- `scripts/compare_results.py` (maintainer) compares pickers prompt by prompt from their `--answers` files: right of n with a Wilson 95% interval, the prompts only one of the two got right, the exact two-sided sign test, and the Holm-adjusted p over several candidates against one baseline; "ahead and significant" only when the candidate is ahead and the adjusted p is below 0.05. `changed` counts how many prompts got a different route between two runs. It refuses files written for another held-out set.
- `scripts/fake_ollama.py` and `scripts/rehearse_results.py` (maintainer) run `trioditis results` end to end against a stand-in server that answers like Ollama and measures nothing, from the source tree or from an installed wheel, and check the file with the collector. The stand-in has modes that misbehave (server error, wrong reply shape, missing embeddings, no Ollama API, dropping out mid-run) for the tests of what a stranger sees. The release workflow runs the wheel rehearsal after its clean-install checks.
- `tests/test_workflows.py` guards what the README and the security model say about the workflows: every `uses:` is a full commit SHA or a local path, every workflow is read-only at the top with write scopes only where listed, no `pull_request_target`, no event or input expression inside a `run:` line, and every service image is pinned by digest.
- A "Help measure" section in the README: the one command, what it needs, where the prompts go, what the file holds and that sharing defaults to No.
- `route_picker.keep_alive_minutes` (optional, absent by default): after each answered picker call, an empty Ollama `/api/generate` request keeps the picker's model loaded that many minutes (1 to 1440), so the next first turn finds it warm instead of waiting for a load. The model stays in memory that long. Absent, the picker's requests are exactly as before (docs/claude-code.md, "Keeping the picker loaded"). Not yet checked on a live Ollama server.
- Each shadow line (`logs/shadow.jsonl`) also holds `prompt_fp`, a 16-character hash of the whole prompt as classified, before any `long_prompts` cut (the decision log's formula; it can confirm a prompt someone already has, and the text is never written), and the picker's limits in force under `route_picker.limits`. `eval-routes --log` splits the picker's outcomes by those limits.
- `route_picker.long_prompts` (optional; `skip` when absent, as before): `cut` has the picker read the first `max_chars` characters of a longer prompt instead of skipping it. The router still reads the whole prompt, and `trioditis reinforce` asks the picker the same way the running router does. The wait is that of a `max_chars` prompt, measured on one laptop on 20 November 2026 (`docs/route-accuracy.md`, "The picker on longer prompts"). How often a cut prompt gets the right route is not measured. Shadow lines log the setting in `route_picker.limits` and the cut in `route_picker.cut`. The shipped defaults stay `timeout_ms` 2500 and `max_chars` 200.
- `scripts/fake_ollama.py` answers `/api/ps` and an empty `/api/generate` the way Ollama does and can load slowly (`--load-delay`, `--keep-alive`, `--per-char`), so the picker's `cold`, `timeout`, `skipped` and `busy` outcomes are reproduced in `tests/test_picker_speed.py`.
- `trioditis_metrics.stats` gains McNemar's test in full (`mcnemar`: exact, mid-p, and chi-square with and without the continuity correction, checked against the eight p-values of Fagerland, Lydersen and Laake 2013, Table 6), exact power for the paired test with the disagreements it needs (`sign_test_power`, `discordant_needed`, `paired_power`), stratified sampling by route with its estimate and standard error (`stratum_allocation`, `stratified_sample`, `stratified_proportion`), and the paired 2×2 table (`paired_counts`). Standard library only, as before.
- `scripts/compare_results.py table FIRST SECOND --out FILE` (maintainer) writes both pickers' routes side by side, one CSV row per held-out prompt with no prompt text, and prints the paired table and McNemar's test. The verdict still comes from `compare`, whose output is unchanged.
- `docs/eval-statistics.md` teaches each of these methods, with when to use it and when not, on one worked example: `scripts/stats_walkthrough.py` sets the keyword rule against a saved `llama3.2:3b` run on the 100 public development prompts, from committed files only, with no model call. A test keeps the page's copy of its output word for word.
- `docs/route-accuracy.md` gains the result of the small-picker comparison set out before any run: eight models of at most 4B parameters against `qwen2.5-coder:7b`, two runs each on GitHub's machines (`bench.yml`). None picked the route more often than the 7B; `gemma3:4b` and `ministral-3:3b` were not shown to differ from it, and the other six were behind. The example config keeps `qwen2.5-coder:7b`. All 36 results and answers files are in `docs/route-accuracy/picker-models/`, so the test reruns with `scripts/compare_results.py`.

**Fixed**
- The test suite no longer fails when `TRIODITIS_API_KEY` is already set in the developer's shell (21 tests answered 401 or 407).
- `scripts/claude-t.ps1` waits up to 5 s for the router's `/health` answer (was 2 s). On a Windows CI runner it once started Claude Code direct, most likely because a cold PowerShell's first call took longer than 2 s. A router that is down still answers at once.
- `scripts/stats_walkthrough.py` runs the keyword rule with no asyncio event loop, so it builds no socket pair and opens no connection on any OS. The loop's own wake-up pipe is a localhost TCP pair on Windows, which failed the walkthrough's no-network test on every Windows CI job. A new test puts that pair in place on any OS and refuses every connection.
- When the stand-in server does not answer within 15 s, `scripts/rehearse_results.py` now says why as far as it can tell: the tries made and what the last one met, whether the stand-in still runs, whether its port is bound, and what it printed. Both rehearsal tests timed out on every macOS CI job with no cause in the log.

**Changed**
- The held-out labels moved from `tests/fixtures/routes-heldout.csv` to `trioditis/data/routes-heldout.csv`, unchanged, so the installed package carries them. The dated run records in `docs/route-accuracy/` keep the old path they were run with.
- `scripts/route_study.py model-baseline` sends the route picker's own request (the router's code), and both study scripts use the package's local-only address check.
- The README, the banner and the package description no longer say that every prompt gets the right model, that sensitive prompts stay on your machine, or that every response names its route. They now say what happens: a route and a model are picked for each prompt (the router chose the labelled route for 205 of 315 held-out prompts); a prompt in which the detectors find an email, card number, key or password stays away from cloud models; routed answers carry the route and model headers, error replies and the Claude Code passthrough don't.

**Removed**
- `privacy.on_no_local_model` is gone. Its `allow_cloud` value never sent a sensitive prompt to a cloud model; it only turned the 422 refusal into a 503. A sensitive prompt that no local model can take is now always refused with 422. A leftover `on_no_local_model:` line is ignored.

**Security and engineering**
- `scripts/console_check.py`'s local HTTPS stand-in refuses TLS 1.0 and 1.1: its server context now sets a TLS 1.2 floor (CodeQL's `py/insecure-protocol`).
- FastAPI 0.163.1 and ruff 0.17.5. Both lockfiles were regenerated with uv from their own header commands, so the Windows-only `colorama` line that pytest needs stays in `requirements-dev.txt`.
- The CodeQL action is pinned at 4.39.3.
- Two guard tests: `tests/test_tls_floor.py` requires every `SSLContext(...)` in the repo to set a TLS 1.2 or 1.3 floor, and `tests/test_lockfiles.py` fails when a lockfile drifts from its own `uv pip compile` command (a runtime pin missing or different in the dev lockfile, the Windows `colorama` line gone, a pin without a hash).
- The stand-in servers in `scripts/` and the tests bind without the reverse-DNS lookup `HTTPServer` makes of its own address, which can stall for many seconds on GitHub's macOS runners: the results rehearsal's stand-in had not answered within 15 s on any macOS job.
- CI's dependency audit installs `pip-audit` from its own hash-pinned lockfile (`requirements-audit.txt`, 28 packages) into a separate environment; before, `pipx run pip-audit` fetched whatever release was newest that day. `tests/test_workflows.py` now fails on any workflow line that runs `pipx run`, `uvx` or `npx`, or a `pip install` without `--require-hashes` (a local file with `--no-deps` passes), and `tests/test_lockfiles.py` also checks the audit and release lockfiles' headers and hashes.

## 0.7.0 (2026-11-24)

**Licence**
- Trioditis is now under the GNU AGPL-3.0-or-later, from this version on. Versions up to 0.6.0 were released under MIT and stay MIT. If you run a modified Trioditis as a service for other people, the AGPL asks you to offer them your modified source. "Trioditis" is the project's name and not part of the licence: a fork needs its own name.
- Contributions now need a contributor agreement (`CLA.md`, adapted from the Apache Software Foundation's individual agreement): you keep your copyright and license your contribution to the maintainer. `CONTRIBUTING.md` says how.

**Claude Code**
- Claude Code can run through Trioditis: set `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`, or start it with `scripts/claude-t`. Requests go to Anthropic unchanged with your own claude.ai login, and the answer streams back unchanged. Each call's token counts go to plan usage, merged with `plan import --claude-code`, so `usage --plan` also shows calls the transcripts leave out. Calls made with an API key count as spend. Trioditis doesn't choose the model here. [docs/claude-code.md](docs/claude-code.md)
- Opt-in shadow counts: with `route_picker: {shadow_claude_code: true}`, the first turn of each Claude Code session is classified by the router and the picker in the background, and one line of numbers (routes, outcome, lengths, never text) goes to `logs/shadow.jsonl`. The request is not changed or held. `eval-routes --log` counts these lines. When the picker misses its timeout, the line also waits for the call to end and records the route it would have named (`route_picker.late`), which `eval-routes --log` reports apart; served requests never wait for it.
- Shadow counts read Claude Code's first turn as typed: the notice and command lines Claude Code adds after `/clear` (or another local command), IDE selections and task notices are left out of the prompt, and a chat that was mid-task when the router restarted is no longer counted as a new first turn. New lines carry `messages`; lines without it were read the old way.
- The Claude Code page now says whose login the passthrough is for: your own, on your own machine. It quotes Anthropic's terms on credential use and says not to put a router in front of other people's subscriptions.
- `/health` now says `"anthropic_gate": true`, so `claude-t` routes only to a router that has the passthrough.
- `scripts/console_check.py` starts Claude Code through the real gate from every console the machine has (bash, zsh, PowerShell 7, Windows PowerShell 5.1, cmd, Git Bash), through `claude-t` and by hand, against a local https stand-in for Anthropic, and checks tool search and the shadow count; nothing is billed. The `consoles` workflow runs it on Linux, macOS, windows-2022 and windows-2025.
- `scripts/claude-t.ps1` (Windows PowerShell 5.1 and PowerShell 7) and `scripts/claude-t.cmd` (cmd) do what `claude-t` does on Windows: the same URL checks, the `/health` check without a proxy, the router key in its own header, and `ENABLE_TOOL_SEARCH`.
- `claude-t` sets `ENABLE_TOOL_SEARCH=true` unless you set it yourself. Claude Code 2.1.296 turned tool search off for any base URL that isn't Anthropic's and sent every tool in full on every turn (289 tools, about 1.4 MB a request, with many MCP servers); 2.1.322 and later keep it on, and the flag changes nothing there.
- The router key is refused when it arrives as `Authorization` or `x-api-key`, bare or after a scheme word such as `Bearer`, so the usual mistake no longer sends it to Anthropic. `claude-t` refuses a `TRIODITIS_URL` that is plain `http` to another machine or has an `@` in it. [docs/security-model.md](docs/security-model.md) and `SECURITY.md` now describe the passthrough: it forwards Claude Code's own credential to Anthropic and runs no privacy detector.
- A missing, wrong or misplaced router key on the Claude Code paths is answered 407 (Proxy Authentication Required) instead of 401, and the message says which of the three it was. Claude Code took the 401 for its own claude.ai login failing: it retried, and the VS Code chat opened its sign-in picker, so changing the router key sent every open chat to that picker. With 407 the error shows once, as written.

**Routing**
- Opt-in `route_picker:` block: a local model picks the route for the first turn of each `auto` conversation (every turn, for a client that sends no session id and no tools). On the 315 held-out prompts `qwen2.5-coder:7b` chose the labelled route for 231 against the router's 205 (p = 0.023), but sent more quick questions to pricier routes. On a laptop CPU with the model loaded, a first turn's median wait was about 1 s at 100 characters and about 2 s at 200. Only a `local: true` provider is accepted. Off unless configured; `config.example.yaml` has the block commented out. [Study](docs/route-accuracy.md#a-local-model-picks-the-route-19-november-2026)
- The router still runs beside the picker and is used when the picker times out (`timeout_ms`, default 2500), fails or names no route. A call that times out is left to finish rather than cancelled, so a model that was loading finishes loading; one picker call runs at a time, so until that call ends, other requests skip the picker (outcome `busy`). Prompts over `max_chars`, tool lists that match a route's `tool_keywords`, forced routes, named models and pinned turns never reach it. `max_chars` defaults to 200: the test prompts were at most 206 characters, and on that laptop 2 of 20 calls passed the 2.5 s timeout at 200 characters, 7 of 20 at 250 and 12 of 20 at 300. Values over 4,000, the most the picker reads, are refused. `scripts/picker_timing.py` in the repository measures another machine.
- Each decision logs `router_route` and `route_picker` (model, instruction version, outcome, route, ms), never prompt text. A picker timeout while Ollama is still loading the model is logged as outcome `cold` (one local `GET /api/ps` after the timeout; providers that aren't Ollama keep `timeout`), and `eval-routes --log` also prints the picker's rate without cold starts. `trioditis check` reports whether the picker answers and how long a call takes, and warns when the picker's first call takes longer than `timeout_ms`.
- `trioditis eval-routes --log` reads the decision log and counts, for requests classified with `route_picker:` on, how often the picker named a route, how long it took, how often it and the router disagreed and where, with a cost estimate for the disagreements. It reads files only and has no labels, so it shows disagreement, not which of the two was right.
- First half of an opt-in reinforcement loop (`reinforce:` block, absent = off). `trioditis reinforce run --dry-run` scores "should the route picker's route be served?" on your own labelled prompts and prints the verdict: the candidate passes only when it is right on more rows and the 95% interval of the paired difference stays above -5 points. `trioditis reinforce revert` (and `--all`) takes adopted values back. Adopted values live in `~/.hlidskjalv/trioditis-adopted.json`, never in `config.yaml`; the running router picks up a change without a restart, `check` lists each value with the layer that set it (default, `config.yaml` or adopted, with the date), and `explain` names an adopted value among its reasons. Not in this version: adopting (`run` without `--dry-run` refuses until the watch that reverts an adoption exists) and threshold steps proposed from traffic. [docs/staying-current.md](docs/staying-current.md#the-reinforcement-loop-opt-in-the-gate-and-the-undo-are-built-adopting-is-not-yet)
- An `embeddings:` block without `threshold` now uses 0.45, the value the example config sets and the held-out test chose (was 0.55). Configs that set `threshold` are unchanged.

**Docs**
- The README's size and privacy claims now match the code: about 7,700 lines (it said 3,400), four direct dependencies and 18 pinned packages (it said four), and the privacy line names its two limits (detectors are patterns; the Claude Code passthrough is relayed unread). Tests fail if the numbers drift again.
- "Every network call" in the README, and the full list in the security model. The list now includes two background calls it left out: the account readouts (every 5 minutes, to a cloud provider's own account API, when its key is set) and Ollama's model list (every 10 minutes).
- The README no longer mentions a Sponsor button; the repository has none.
- Examples use `/path/to/provider-facts.json` in place of a path in the author's home folder, and no longer say Euboulia exports `plan-usage.v1` files: it doesn't yet.

**Fixed**
- Calls to local models no longer go through a proxy. With `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY` set and no `NO_PROXY` entry for the local server, the router sent every call to a `local: true` provider (chat, embeddings, the route picker, the installed-model list) to that proxy, including prompts the privacy check had kept local. Those calls now ignore proxy variables. Cloud calls still use the proxy. If you reach a `local: true` server through a proxy on purpose, that stops working: give the router a direct address.

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
