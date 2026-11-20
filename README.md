<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/banner-dark.svg">
  <img alt="Trioditis: one endpoint, the right model for every prompt" src="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/banner-light.svg" width="720">
</picture>

**A security-first router for LLMs.** One OpenAI-compatible endpoint that sends each prompt to the right model, whether local (Ollama), cloud (Claude, GPT, OpenRouter) or legacy, and keeps sensitive prompts on your machine.

[![ci](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/ci.yml)
[![codeql](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexb-joat/trioditis/actions/workflows/codeql.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-555)
[![license](https://img.shields.io/badge/license-MIT-green)](https://github.com/alexb-joat/trioditis/blob/main/LICENSE)

[Quick start](#quick-start) &nbsp;·&nbsp; [How it routes](#how-a-request-is-routed) &nbsp;·&nbsp; [Connect your tools](#connect-your-tools) &nbsp;·&nbsp; [Security](#security-at-a-glance) &nbsp;·&nbsp; [Docs](#documentation)

<sub><i>Trioditis: Hecate of the three ways, who stands where the road splits and decides who goes where.<br>Every prompt reaches the same crossroads here: a local model, a cloud model, or no further.</i></sub>

</div>

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/routing-dark.svg">
  <img alt="Clients send requests to Trioditis on 127.0.0.1:8787. It checks privacy, picks a route, filters models and calls with fallback to local, cloud or legacy models." src="https://raw.githubusercontent.com/alexb-joat/trioditis/main/docs/img/routing-light.svg" width="880">
</picture>

## Why Trioditis

| Private by default | Cost-aware | Small and auditable | Explains itself |
|---|---|---|---|
| Prompts with emails, cards, keys or passwords go to a local model or are rejected. They are never sent to a cloud API by accident. | Daily and monthly caps with automatic local fallback, plus per-provider limits and live spend. | About 3,400 lines and four hash-pinned dependencies. No LiteLLM, no LangChain, no telemetry. | Every response names its route and model. A log records the reasons, with no prompt text. |

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
```

**4.** Try it:

```bash
trioditis explain "Why does my python function raise KeyError?"     # shows the decision, calls no model
curl -s http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Write a regex for UK postcodes"}]}'
```

Cloud models are optional: add `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `OPENROUTER_API_KEY` to `.env` and they join the routes. A cloud provider without a key is simply skipped.

## How a request is routed

1. **Privacy check.** Built-in detectors (emails, card numbers, IBANs, API keys, private keys, passwords) plus your own keywords and regexes. A hit restricts the request to local models; if none can serve it, it is rejected rather than leaked.
2. **Pick a route** (`code`, `reasoning`, `writing`, `quick`, or your own): keyword and pattern signals, the names of the tools a coding agent offers (`tool_keywords`), then similarity to each route's example prompts using a *local* embedding model, else the default route. An agent's later turns stay on the route its task started on.
   Optionally, a *local* model picks the route instead (`route_picker:`, off unless configured): on the held-out prompts it chose the labelled route for 231 of 315 against the router's 205, but sends more quick questions to pricier routes, and it adds about 1 s to each conversation's first turn. The router still runs and is used when the picker is slow, fails or names no route, and requests whose tools match a route's `tool_keywords`, or whose prompt is over `max_chars` (default 500), never reach it. The decision log names both routes ([study](https://github.com/alexb-joat/trioditis/blob/main/docs/route-accuracy.md#a-local-model-picks-the-route-19-november-2026)).
3. **Filter and order models** by what the request needs: vision, tool calling, context length, and whether the provider's key is present. A route can keep its listed order or `prefer: cheapest` (local first, then by this request's estimated cost) or `fastest` (median latency of recent calls); a provider that just answered "too many requests" is tried last until its wait is over.
4. **Call the first model and fall back** on outage, rate limit, missing model or spending cap. If no model of the route fits, only another route's model or a local one is used, never a model you call by name only.
5. **Pin the session.** Requests with the same `X-Session-Id` (or `user`) stay on one route, so agents do not switch models mid-task.

Responses carry `X-Trioditis-Route` and `X-Trioditis-Model`. `POST /v1/route` or `trioditis explain "…"` shows the decision and reasons without calling a model (`/v1/route` does ask a configured route picker; `explain` contacts nothing). Clients can bypass routing with `model: "route/code"` (force a route), `model: "legacy-mistral"` (call a model directly) or the provider's own id, such as `model: "claude-haiku-5-5"`, which pins an exact version; `X-Trioditis-Upstream-Model` names the id the provider was sent.

## Connect your tools

| Client | Settings |
|---|---|
| **Cline** (VS Code) | Provider *OpenAI Compatible*, base URL `http://127.0.0.1:8787/v1`, model `route/code`. Full recipe, including local-model context: [docs/vscode-cline.md](https://github.com/alexb-joat/trioditis/blob/main/docs/vscode-cline.md) |
| **Open WebUI, Odysseus, chat UIs** | Add the endpoint `http://127.0.0.1:8787/v1` and pick model `auto` |
| **OpenAI SDKs** | `OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<your TRIODITIS_API_KEY>")` |
| **Claude Code** | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`, or `scripts/claude-t` from a clone, which goes direct if the router is down. Recipe: [docs/claude-code.md](https://github.com/alexb-joat/trioditis/blob/main/docs/claude-code.md) |

Trioditis speaks the OpenAI chat-completions format, which most tools support, and routes those requests. Claude Code speaks Anthropic's Messages format: Trioditis passes those requests to Anthropic unchanged with your own login and counts their tokens. It doesn't route them, and Claude Code picks the model.

## Spending and limits

Every cloud call is checked against your daily and monthly caps first (defaults: $1 a day, $15 a month). Over a cap, the route's local model answers instead, so you get an answer, not a bill. `trioditis usage --since 7d --by client` shows what each tool spent. Per-provider caps, provider rate-limit and balance readouts, and call history are in [docs/spending-and-limits.md](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md). `trioditis hardware` suggests which local models fit your computer ([docs/hardware.md](https://github.com/alexb-joat/trioditis/blob/main/docs/hardware.md)).

## Staying current

Each call records why its answer ended, retries and cache use, so `trioditis usage --outcomes` shows each model's error, cut-off and retry rates with 95% intervals. A daily `trioditis refit` adds latency and drift charts, and, given a model facts file, names configured models that are deprecated or about to retire and lists new ones worth testing. It changes nothing on its own: [docs/staying-current.md](https://github.com/alexb-joat/trioditis/blob/main/docs/staying-current.md). On a subscription, `trioditis plan import --claude-code` and `usage --plan` show tokens per model and effort next to how far the plan's meters rose, from counts only, never text ([Plan usage](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md#plan-usage)).

## Security at a glance

| Risk | Control |
|---|---|
| Hijacked dependency | Four direct dependencies, hash-pinned (`--require-hashes`), `pip-audit` in CI, Dependabot with a 7-day hold |
| Router exposed to the network | Binds `127.0.0.1`; refuses any other bind without a 32+ character API key |
| A web page in your browser reaching the router | Foreign `Host` (DNS rebinding) or `Origin` gets 403 and a log line; POST bodies must be JSON |
| Sensitive data sent to the cloud | Detectors force local-only routing or reject |
| Key and prompt leakage | Keys only in environment variables; the client's router key is never forwarded; logs hold metadata and a hash, not prompt text. The Claude Code passthrough forwards your own Anthropic credential to `https://api.anthropic.com` only, and never stores or logs it |
| Open proxy and SSRF | Upstream URLs come only from `config.yaml`; no redirects followed |
| Supply chain | Actions pinned to commit SHAs, read-only workflows, CodeQL and dependency review on every pull request |

Report a vulnerability privately via [SECURITY.md](https://github.com/alexb-joat/trioditis/blob/main/SECURITY.md). The full design and its limits are in [docs/security-model.md](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md).

## Known limits

- Fallback happens before an answer starts. If a streamed answer breaks halfway, Trioditis does not retry it on another model.
- Every routed chat request goes to the provider's OpenAI-style `/chat/completions` address. There are no native Anthropic or Gemini adapters for routing; the Claude Code passthrough only forwards to Anthropic.
- No prompt caching for Claude models. Trioditis calls Anthropic at its OpenAI-compatible endpoint, and Anthropic's page for it says "Prompt caching is not supported" ([source](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk), read 13 November 2026). For every provider, Trioditis adds no caching settings of its own and records cached tokens only when the provider reports them.
- On Windows, Trioditis does not warn about a `.env` that other users can read, and it does not set the mode of the metrics database. Keep the folder in your user profile ([security model](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md)).
- Only the Linux installer sets up the daily `refit` and `plan import`. On macOS and Windows, run them by hand.
- CI does not build the Docker image.
- The router picks the right route about two times in three. On 315 held-out prompts it chose the labelled route for 205 (65.1%, 95% interval 59.7–70.1%) on the example config with local embeddings on, and for 116 (36.8%) with keywords alone. Most misses (65 of 110) go to `quick`, the default route, and a miss still gets an answer. The prompts are synthetic and in English; measured 13 November 2026 ([method, data and limits](https://github.com/alexb-joat/trioditis/blob/main/docs/route-accuracy.md)). Your own routes give a different number: `trioditis eval-routes` measures it.
- The route picker was measured on one machine, one set of labels and one model at temperature 0, on prompts of at most 206 characters with no tool lists, so longer prompts skip it unless you raise `max_chars`. `eval-routes` does not measure it yet; the decision log's `route` and `router_route` show how often it overrules the router on your traffic.

## Platforms

| Linux (systemd) | macOS (launchd) | Windows 10/11 (Scheduled Task) |
|---|---|---|
| Python 3.11+, CI | Python 3.11+, CI | Python 3.11+, CI |

CI runs the unit tests and a real-server smoke test on all three, on Python 3.11, 3.12 and 3.13. To run in the background or in a hardened container, see [docs/deployment.md](https://github.com/alexb-joat/trioditis/blob/main/docs/deployment.md).

## Documentation

- [VS Code with Cline](https://github.com/alexb-joat/trioditis/blob/main/docs/vscode-cline.md) · [Spending caps and provider limits](https://github.com/alexb-joat/trioditis/blob/main/docs/spending-and-limits.md) · [Hardware suggestions](https://github.com/alexb-joat/trioditis/blob/main/docs/hardware.md)
- [Background service and Docker](https://github.com/alexb-joat/trioditis/blob/main/docs/deployment.md) · [Security model](https://github.com/alexb-joat/trioditis/blob/main/docs/security-model.md) · [`config.example.yaml`](https://github.com/alexb-joat/trioditis/blob/main/config.example.yaml) (providers, models, routes, privacy, limits, logging)

**See every decision live:** [Hlidskjalv](https://github.com/alexb-joat/hlidskjalv) reads the decision log (route, model, tokens, never prompts) and shows it next to your files, commands and Git history.

<details>
<summary>Roadmap</summary>

- [x] Daily and monthly spending caps with local fallback
- [x] Hardware detection and local-model suggestions
- [x] Routes can prefer the cheapest or fastest model
- [x] Outcome rates per model, a plan-usage ledger and a daily refit
- [ ] A small local LLM as an optional classifier for ambiguous prompts
- [ ] Learn from feedback: per-route model scores from thumbs up and down
- [ ] Cost and latency budgets per route
- [ ] Native Anthropic and Gemini adapters (today: their OpenAI-compatible endpoints)
- [ ] Per-user keys and rate limits for team use

</details>

## Contributing

Pull requests are welcome; see [CONTRIBUTING.md](https://github.com/alexb-joat/trioditis/blob/main/CONTRIBUTING.md). Run the checks with `pip install --require-hashes --no-deps -r requirements-dev.txt && python -m pytest -q`. If Trioditis saves you money or time, the **Sponsor** button at the top of the repository supports it.

## License

[MIT](https://github.com/alexb-joat/trioditis/blob/main/LICENSE). Not affiliated with Anthropic, OpenAI, Ollama, OpenRouter or any model provider; their names refer only to their public APIs.
