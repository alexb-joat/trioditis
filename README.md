<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/banner-dark.svg">
  <img alt="Trioditis: one endpoint, a route for every prompt" src="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/banner-light.svg" width="720">
</picture>

**A security-first router for LLMs.** One OpenAI-compatible endpoint that picks a route and a model for each prompt, whether local (Ollama), cloud (Claude, GPT, OpenRouter) or legacy, and keeps a prompt in which its detectors find an email, card number, key or password away from cloud models.

For developers who run models in Ollama, with or without cloud keys, and want their editor, chat app and scripts to share one local address.

[![ci](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml)
[![codeql](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-555)
[![license](https://img.shields.io/badge/license-AGPL--3.0-blue)](https://github.com/alexb-joat/trioditis/blob/main/LICENSE)

[Quick start](#quick-start) &nbsp;·&nbsp; [Help measure](#help-measure) &nbsp;·&nbsp; [How it routes](#how-a-request-is-routed) &nbsp;·&nbsp; [Connect your tools](#connect-your-tools) &nbsp;·&nbsp; [Security](#security-at-a-glance) &nbsp;·&nbsp; [Docs](#documentation)

<sub><i>Trioditis: Hecate of the three ways, who stands where the road splits and decides who goes where.<br>Every prompt reaches the same crossroads here: a local model, a cloud model, or no further.</i></sub>

</div>

## See it decide

With an Anthropic key in `.env`, `trioditis explain` shows where a prompt would go and why, without calling any model. Here the coding question goes to Claude Sonnet, with a local model as fallback, and the prompt with a card number stays on a local model. Real output from the example config, with some fields left out (`…`):

```text
$ trioditis explain "Why does my python function raise KeyError?"
{
  "route": "code",
  "model": "claude-sonnet",
  "fallbacks": [
    "local-coder"
  ],
  "reasons": [
    "keywords matched route code (score 2)",
  …
}
$ trioditis explain "My card is 4111 1111 1111 1111, why was it declined?"
{
  "route": "quick",
  "model": "local-general",
  …
  "reasons": [
    "sensitive content (card_number) -> local models only",
    "no strong signal -> default route quick",
    "skip claude-haiku: cloud model blocked (local-only)"
  ],
  …
}
```

**You can measure it too.** After the [quick start](#quick-start), one command runs the route picker and the router on the labelled test prompts that ship with Trioditis, against your local Ollama, and writes the numbers to a file. It asks before sharing anything, and the default is No ([what it runs and what the file holds](#help-measure)):

```bash
trioditis results
```

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/routing-dark.svg">
  <img alt="Clients send requests to Trioditis on 127.0.0.1:8787. It checks privacy, picks a route, filters models and calls with fallback to local, cloud or legacy models." src="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/routing-light.svg" width="880">
</picture>

## Why Trioditis

- **Private by default.** A prompt in which the detectors find an email, card number, key or password goes to a local model or is rejected. Detectors are patterns and miss some; Claude Code's passthrough is relayed unread.
- **Cost-aware.** Daily and monthly caps with automatic local fallback, plus per-provider limits and live spend.
- **Small and auditable.** About 9,100 lines of Python. Four direct dependencies, 18 packages in the lockfile, each pinned by hash. No LiteLLM, no LangChain, no telemetry.
- **Explains itself.** Routed answers name their route and model in response headers; Trioditis's own error replies and the Claude Code passthrough don't. A log records the reasons, with no prompt text.

## Quick start

**1.** Install [Ollama](https://ollama.com/download) and pull a couple of local models (run it natively, not in Docker):

```bash
ollama pull llama3.2:3b && ollama pull qwen2.5-coder:7b && ollama pull nomic-embed-text
```

**2.** Install Trioditis with [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html) and give it a folder:

```bash
pipx install trioditis
trioditis init --dir ~/trioditis     # writes config.yaml and .env there (owner-only); never overwrites
cd ~/trioditis                       # config.yaml, .env and logs/ are read from the current folder
```

Version 0.7.0 and later are under the [GNU AGPL-3.0-or-later](#license). Versions up to 0.6.0 were released under MIT and stay MIT.

<details>
<summary>Or from a clone, with every package checked against the lockfile's hashes</summary>

`pipx` installs the newest release of every dependency on the day you run it. The clone installs the exact versions in `requirements.txt` and refuses any file whose hash differs. From a clone, write `python -m trioditis` wherever these pages say `trioditis`.

```bash
git clone https://github.com/alexb-joat/trioditis.git && cd trioditis
python3 -m venv .venv && source .venv/bin/activate    # Debian/Ubuntu: sudo apt install python3-venv
pip install --require-hashes --no-deps -r requirements.txt
python -m trioditis init
```

Windows (PowerShell):

```powershell
git clone https://github.com/alexb-joat/trioditis.git; cd trioditis
py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install --require-hashes --no-deps -r requirements.txt
python -m trioditis init
```

</details>

**3.** Check and run, the same on every system:

```bash
trioditis check     # which local models answer, which cloud keys are set
trioditis serve     # http://127.0.0.1:8787/v1, localhost only
```

With the models from step 1 pulled and no cloud keys yet, `check` prints:

```text
config OK: 9 models, 4 routes
  local-general            local  ready
  local-coder              local  ready
  local-reasoner           local  not on this server: ollama pull deepseek-r1:8b
  local-vision             local  not on this server: ollama pull llava:7b
  legacy-mistral           local  not on this server: ollama pull mistral:7b
  claude-sonnet            cloud  missing $ANTHROPIC_API_KEY
  claude-haiku             cloud  missing $ANTHROPIC_API_KEY
  claude-opus              cloud  missing $ANTHROPIC_API_KEY
  gpt                      cloud  missing $OPENAI_API_KEY
2 of 9 models confirmed ready; cloud providers are never contacted
embeddings: nomic-embed-text on ollama: ready
```

**4.** Try it:

```bash
trioditis explain "Why does my python function raise KeyError?"     # shows the decision, calls no model
curl -s http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Write a regex for UK postcodes"}]}'
```

Cloud models are optional: add `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `OPENROUTER_API_KEY` to `.env` and they join the routes. A cloud provider without a key is simply skipped.

## Help measure

Trioditis ships 315 labelled prompts, the public held-out set. One command runs its route picker and its router on them on your machine and writes the numbers to a file you may share:

```bash
trioditis results
```

It needs a local [Ollama](https://ollama.com/download) with two models pulled, as in step 1 of the quick start: the picker, `qwen2.5-coder:7b` unless you choose another with `--model`, and the embeddings model `nomic-embed-text`. The fixed prompts and fixed settings are explained in [the held-out test](https://github.com/alexb-joat/trioditis/blob/main/docs/route-accuracy.md).

- **Where the prompts go:** only to the local server at `--base-url` (default `http://localhost:11434/v1`). A server that is not on this machine is refused, proxy variables are ignored, and your `config.yaml` is not read: the settings come from the example config shipped in the package, so results from different machines measure the same thing.
- **What the file holds:** numbers, model names and digests, versions, a hardware summary and the date. It never holds prompt text, file paths, user or host names, or times finer than the date. The whole file is printed, and written to `trioditis-results.json` (choose another name with `--out`; an earlier file there is replaced). With `--own-labels FILE` it also scores your own labelled prompts, as counts only. With `--answers FILE` it writes a second file, kept apart: the route the picker named for each of the 315 public prompts, by row number, so two models can be compared prompt by prompt. It holds no prompt text and never your own labelled prompts, and the share form does not take it.
- **Sharing:** the last question is whether to share the numbers publicly on GitHub, and the default is No. Yes opens GitHub's form in your browser with the numbers filled in; you read them and press Submit, and nothing is sent before that. The program itself sends nothing anywhere except to your local server. Shared numbers are public and released under CC0. `--no-share` skips the question, and it is never asked when the command is not run in a terminal.

## How a request is routed

1. **Privacy check.** Built-in detectors (emails, card numbers, IBANs, API keys, private keys, passwords) plus your own keywords and regexes. A hit restricts the request to local models; if none can serve it, it is rejected rather than leaked.
2. **Pick a route** (`code`, `reasoning`, `writing`, `quick`, or your own): keyword and pattern signals, the names of the tools a coding agent offers (`tool_keywords`), then similarity to each route's example prompts using a *local* embedding model, else the default route. An agent's later turns stay on the route its task started on.
   Optionally, a *local* model picks the route instead (`route_picker:`, off unless configured): on the held-out prompts it chose the labelled route for 231 of 315 against the router's 205, but sends more quick questions to pricier routes. Each conversation's first turn waits for it (every turn, for a client that sends no session id and no tools): on a laptop CPU the median was about 1 s at 100 characters and about 2 s at 200, once the model is loaded. The router still runs and is used when the picker passes `timeout_ms` (2.5 s), fails or names no route. The first turn after the model was unloaded waits those 2.5 s; the model then loads in the background (15 to 21 s on that laptop; Ollama unloads an idle model after 5 minutes by default) and other requests skip the picker until that background call ends: one picker call runs at a time. Requests whose tools match a route's `tool_keywords`, or whose prompt is over `max_chars` (default 200), never reach it, unless `long_prompts: cut` has the picker read the first `max_chars` characters of a longer prompt (how often that names the right route is not measured). The decision log names both routes, and `trioditis eval-routes --log` counts from it how often the two disagreed on your own requests and what that cost ([study](https://github.com/alexb-joat/trioditis/blob/main/docs/route-accuracy.md#a-local-model-picks-the-route-19-november-2026)).
3. **Filter and order models** by what the request needs: vision, tool calling, context length, and whether the provider's key is present. A route can keep its listed order or `prefer: cheapest` (local first, then by this request's estimated cost) or `fastest` (median latency of recent calls); a provider that just answered "too many requests" is tried last until its wait is over.
4. **Call the first model and fall back** on outage, rate limit, missing model, spending cap, or a cloud key the provider refuses (401, 403) or an account out of credit (402). If no model of the route fits, only another route's model or a local one is used, never a model you call by name only.
5. **Pin the session.** Requests with the same `X-Session-Id` (or `user`) stay on one route, so agents do not switch models mid-task.

Responses carry `X-Trioditis-Route` and `X-Trioditis-Model`. `POST /v1/route` or `trioditis explain "…"` shows the decision and reasons without calling a model (`/v1/route` does ask a configured route picker; `explain` contacts nothing). Clients can bypass routing with `model: "route/code"` (force a route), `model: "legacy-mistral"` (call a model directly) or the provider's own id, such as `model: "claude-haiku-5-5"`, which pins an exact version; `X-Trioditis-Upstream-Model` names the id the provider was sent.

## Connect your tools

| Client | Settings |
|---|---|
| **Cline** (VS Code) | Provider *OpenAI Compatible*, base URL `http://127.0.0.1:8787/v1`, model `route/code`. Full recipe, including local-model context: [docs/vscode-cline.md](https://github.com/alexb-joat/trioditis/blob/main/docs/vscode-cline.md) |
| **Open WebUI, Odysseus, chat UIs** | Add the endpoint `http://127.0.0.1:8787/v1` and pick model `auto` |
| **OpenAI SDKs** | `OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<your TRIODITIS_API_KEY>")` |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`, or `scripts/claude-t` from a clone, which goes direct if the router is down. Recipe: [docs/claude-code.md](https://github.com/alexb-joat/trioditis/blob/main/docs/claude-code.md) |

Trioditis speaks the OpenAI chat-completions format, which most tools support, and routes those requests. Claude Code speaks Anthropic's Messages format: Trioditis passes those requests to Anthropic unchanged with your own login and counts their tokens. It doesn't route them, and Claude Code picks the model. This is for your own login on your own machine: Trioditis is not a hosted service and must not sit in front of other people's subscriptions ([whose login](https://github.com/alexb-joat/trioditis/blob/main/docs/claude-code.md#your-own-login-on-your-own-machine)).

## Spending and limits

Every cloud call is checked against your daily and monthly caps first (defaults: $1 a day, $15 a month). Over a cap, the route's local model answers instead, so you get an answer, not a bill. `trioditis usage --since 7d --by client` shows what each tool spent. Per-provider caps, provider rate-limit and balance readouts, and call history are in [docs/spending-and-limits.md](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md). `trioditis hardware` suggests which local models fit your computer ([docs/hardware.md](https://github.com/alexb-joat/trioditis/blob/main/docs/hardware.md)).

## Staying current

Each call records why its answer ended, retries and cache use, so `trioditis usage --outcomes` shows each model's error, cut-off and retry rates with 95% intervals. A daily `trioditis refit` (experimental) adds latency and drift charts, and, given a model facts file, names configured models that are deprecated or about to retire and lists new ones worth testing. It changes nothing on its own: [docs/staying-current.md](https://github.com/alexb-joat/trioditis/blob/main/docs/staying-current.md). On a subscription, `trioditis plan import --claude-code` and `usage --plan` (experimental) show tokens per model and effort next to how far the plan's meters rose, from counts only, never text ([Plan usage](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md#plan-usage)).

## Security at a glance

| Risk | Control |
|---|---|
| Hijacked dependency | Four direct dependencies, 18 packages in the lockfile, each hash-pinned (`--require-hashes`, from a clone; `pipx` takes the newest releases), `pip-audit` in CI, Dependabot with a 7-day hold |
| Router exposed to the network | Binds `127.0.0.1`; refuses any other bind without a 32+ character API key |
| A web page in your browser reaching the router | Foreign `Host` (DNS rebinding) or `Origin` gets 403 and a log line; POST bodies must be JSON |
| Sensitive data sent to the cloud | Detectors force local-only routing or reject (not for Claude Code through the passthrough, which is relayed unread) |
| Key and prompt leakage | Keys only in environment variables; the client's router key is never forwarded; logs hold metadata and a hash, not prompt text. The Claude Code passthrough forwards your own Anthropic credential to `https://api.anthropic.com` only, and never stores or logs it |
| Open proxy and SSRF | Upstream URLs come only from `config.yaml`; no redirects followed |
| Supply chain | Actions pinned to commit SHAs, read-only workflows, CodeQL and dependency review on every pull request |

### Every network call

Trioditis has no telemetry, no update check and no remote config. While `trioditis serve` runs, it connects to:

- **The providers in your `config.yaml`**, for the requests you send. Cloud calls use your proxy variables if you set any.
- **`https://api.anthropic.com`**, for Claude Code's requests through the passthrough.
- **Your local model servers** (`local: true`), never through a proxy: the answers themselves, the embeddings and route-picker calls if you configure them, Ollama's model list (`GET /api/tags`, every 10 minutes) and, after a slow picker call, `GET /api/ps`.
- **Each cloud provider's own account API**, every 5 minutes, only for a provider whose key is set: OpenRouter's key usage with your normal key; Anthropic's and OpenAI's cost reports only if you set an admin key (`admin_key_env`). Read-only `GET` requests that fill the spend readout.

The commands that connect anywhere (`check`, `hardware`, `eval-routes`, `reinforce run`, `results`) ask local servers only; `results` can also open GitHub's form in your browser, filled in, if you say yes. The full list is in the [security model](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md#which-outbound-calls-exist).

Report a vulnerability privately via [SECURITY.md](https://github.com/alexb-joat/trioditis/blob/main/SECURITY.md). The full design and its limits are in [docs/security-model.md](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md).

## Known limits

- The privacy detectors read text: every message's text parts and the arguments of earlier tool calls. They cannot read images, files or audio, so a screenshot or PDF with a card number in it is not caught.
- Fallback happens before an answer starts. If a streamed answer breaks halfway, Trioditis does not retry it on another model: it ends the stream with an error event and logs the call as failed.
- Every routed chat request goes to the provider's OpenAI-style `/chat/completions` address. There are no native Anthropic or Gemini adapters for routing; the Claude Code passthrough only forwards to Anthropic.
- No prompt caching for Claude models. Trioditis calls Anthropic at its OpenAI-compatible endpoint, and Anthropic's page for it says "Prompt caching is not supported" ([source](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk), read 13 November 2026). For every provider, Trioditis adds no caching settings of its own and records cached tokens only when the provider reports them.
- On Windows, Trioditis does not warn about a `.env` that other users can read, and it does not set the mode of the metrics database. Keep the folder in your user profile ([security model](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md)).
- `config.yaml` and `.env` are read when `serve` starts. Restart it after changing them.
- Only the Linux installer sets up the daily `refit` and `plan import`. On macOS and Windows, run them by hand.
- CI does not build the Docker image.
- The router picks the right route about two times in three. On 315 held-out prompts it chose the labelled route for 205 (65.1%, 95% interval 59.7–70.1%) on the example config with local embeddings on, and for 116 (36.8%) with keywords alone. Most misses (65 of 110) go to `quick`, the default route, and a miss still gets an answer. The prompts are synthetic and in English; measured 13 November 2026 ([method, data and limits](https://github.com/alexb-joat/trioditis/blob/main/docs/route-accuracy.md)). Your own routes give a different number: `trioditis eval-routes` measures it.
- The route picker was measured on one machine, one set of labels and one model at temperature 0, on prompts of at most 206 characters with no tool lists, so longer prompts skip it unless you raise `max_chars` or set `long_prompts: cut`. With `cut` the picker reads only their first `max_chars` characters, and its accuracy on prompts cut that way is not measured. Its calls also slow down with length: on that laptop, in ordinary use, 2 of 20 calls passed `timeout_ms` at 200 characters, 7 of 20 at 250 and 12 of 20 at 300. To measure your machine, run [`scripts/picker_timing.py`](https://github.com/alexb-joat/trioditis/blob/main/scripts/picker_timing.py) from a clone of the repository. `eval-routes` does not measure it yet; the decision log's `route` and `router_route` show how often it overrules the router on your traffic.

## Platforms

| Linux (systemd) | macOS (launchd) | Windows 10/11 (Scheduled Task) |
|---|---|---|
| Python 3.11+, CI | Python 3.11+, CI | Python 3.11+, CI |

CI runs the unit tests and a real-server smoke test on all three, on Python 3.11, 3.12 and 3.13. To run in the background or in a hardened container, see [docs/deployment.md](https://github.com/alexb-joat/trioditis/blob/main/docs/deployment.md).

## Documentation

- [VS Code with Cline](https://github.com/alexb-joat/trioditis/blob/main/docs/vscode-cline.md) · [Spending caps and provider limits](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md) · [Hardware suggestions](https://github.com/alexb-joat/trioditis/blob/main/docs/hardware.md)
- [Background service and Docker](https://github.com/alexb-joat/trioditis/blob/main/docs/deployment.md) ([upgrading from 0.9.0 or earlier](https://github.com/alexb-joat/trioditis/blob/main/docs/deployment.md#upgrading-from-090-or-earlier)) · [Security model](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md) · [`config.example.yaml`](https://github.com/alexb-joat/trioditis/blob/main/config.example.yaml) (providers, models, routes, privacy, limits, logging)
- [Configuration reference](https://github.com/alexb-joat/trioditis/blob/main/docs/configuration.md) (every key, its default and label) · [Compatibility](https://github.com/alexb-joat/trioditis/blob/main/docs/compatibility.md) (commands, environment variables, HTTP API and files: what stays stable)

**See every decision live:** [Lunete](https://github.com/alexb-joat/lunete) reads the decision log (route, model, tokens, never prompts) and shows it next to your files, commands and Git history.

<details>
<summary>Roadmap</summary>

- [x] Daily and monthly spending caps with local fallback
- [x] Hardware detection and local-model suggestions
- [x] Routes can prefer the cheapest or fastest model
- [x] Outcome rates per model, a plan-usage ledger and a daily refit (the last two experimental)
- [ ] A small local LLM as an optional classifier for ambiguous prompts
- [ ] Learn from feedback: per-route model scores from thumbs up and down
- [ ] Cost and latency budgets per route
- [ ] Native Anthropic and Gemini adapters (today: their OpenAI-compatible endpoints)
- [ ] Per-user keys and rate limits for team use

</details>

## Contributing

Pull requests are welcome; see [CONTRIBUTING.md](https://github.com/alexb-joat/trioditis/blob/main/CONTRIBUTING.md). Run the checks with `pip install --require-hashes --no-deps -r requirements-dev.txt && python -m pytest -q`. Code and documentation contributions need the [contributor agreement](https://github.com/alexb-joat/trioditis/blob/main/CLA.md).

## License

[GNU AGPL-3.0-or-later](https://github.com/alexb-joat/trioditis/blob/main/LICENSE). Copyright (C) 2026 Alex Bubnov.

Trioditis is used over a network, so the AGPL's network clause applies: if you run a modified version as a service for others, you must offer them your modified source under the same licence. Released versions up to v0.6.0 remain under their original MIT terms.

**Name:** "Trioditis" is the project name, not part of the code licence. A fork must use a different name and must not imply endorsement by or affiliation with this project.

Not affiliated with Anthropic, OpenAI, Ollama, OpenRouter or any model provider; their names refer only to their public APIs.
