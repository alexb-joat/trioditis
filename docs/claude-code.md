# Claude Code through Trioditis

Trioditis can sit between Claude Code and Anthropic on your own machine. Requests go through unchanged, with your own claude.ai login, and Trioditis counts each call's tokens. It doesn't route these calls: Claude Code picks the model, as it does without Trioditis.

What you get: every call's input, output and cache tokens in `trioditis usage --plan`, including the calls Claude Code makes that its transcripts don't record. A later `trioditis plan import --claude-code` merges into the same rows instead of counting them twice.

## Attach

For one terminal session, from a clone of the repository:

```sh
scripts/claude-t            # same arguments as claude
```

`claude-t` checks that the router at `http://127.0.0.1:8787` has the passthrough (`/health` says `"anthropic_gate": true`). If it does, it sets `ANTHROPIC_BASE_URL` for that one process, and `ENABLE_TOOL_SEARCH=true` unless you set it yourself (see Limits). If it doesn't, it starts Claude Code direct, without metering, and prints a line saying so. Plain `claude` is never affected.

Without the script, set the variable yourself:

```sh
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

For the VS Code extension and every terminal, put the same variable in the `env` block of `~/.claude/settings.json`:

```json
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787" } }
```

To detach, remove that line (or `unset ANTHROPIC_BASE_URL`) and restart Claude Code; it goes direct to Anthropic again. Do this if the router is down: a Claude Code pointed at a stopped router can't answer.

## What passes through

- **Up:** the request body as bytes, every `anthropic-*` and `x-claude-code-*` header, and `Authorization` or `x-api-key`, unchanged. Trioditis sends only to `https://api.anthropic.com` (only the router's own environment can point it elsewhere, with `TRIODITIS_GATE_UPSTREAM`, https only) and drops only its own `X-Trioditis-*` headers and the connection headers that belong to one hop.
- **Down:** the status, the headers Claude Code reads (`retry-after`, `x-should-retry`, `anthropic-ratelimit-unified-*`) and the body, unchanged. A stream is passed on chunk by chunk, `ping` events included. Error bodies aren't wrapped: Claude Code recovers from some errors by their exact wording.
- **Paths:** `POST /v1/messages` and `POST /v1/messages/count_tokens`. Claude Code's `HEAD /api/hello` warm-up gets a 404, or a 401 when the router has a key; the gateway guide says a gateway can reject it without breaking anything.

These are the requirements in Anthropic's [gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol), read 18 November 2026.

## What is kept

Counts only, never text: model, message id (hashed with your machine's salt), session id (hashed), whether a subagent sent it, and the input, output, cache-read and cache-write tokens. Your credential is passed on and never stored or logged. Trioditis checks two things about it: that it isn't the router key (below), and whether an `x-api-key` header is present:

- **No `x-api-key`** (your claude.ai login): the call goes to plan usage, next to `plan import`. It never counts toward the spending caps, because the plan's meters bill it, not per token.
- **`x-api-key` present** (an Anthropic API key): the call is billed per token, so it goes with the API calls (`trioditis usage`, route `passthrough`), priced from `prices.json` when the model is there, and counts toward the caps. Trioditis never blocks it, even past a cap; Claude Code would only stall. The caps then stop the router's other cloud calls, not this one.

## Shadow counts (opt-in)

Claude Code's requests are never routed: Anthropic's model answers whatever Trioditis would have picked. To see which route the router and the route picker *would* name for your real prompts, add `shadow_claude_code: true` to the `route_picker:` block and restart the router. Off unless set.

- **What runs:** for the first main request of each Claude Code session, the same classification a routed `auto` request gets (router and picker side by side, with the picker's `max_chars` and `timeout_ms`), in the background. The request goes to Anthropic unchanged and is not held while this runs.
- **What it calls:** the picker's local model and, if configured, the local embeddings provider. Both must be `local: true`. Nothing leaves the machine.
- **What it writes:** one line per session to `logs/shadow.jsonl`, beside the decision log, owner-only: the router's route, what the picker did, the prompt's length, the length of the whole message, how many system-reminder blocks were left out, the number of tools, the model name and a hashed session id. Never the prompt text.
- **What reads it:** `trioditis eval-routes --log` counts these lines with the decision log's. They have no labels either, so they show disagreement, not which side was right. They carry no cost, because nothing was served.
- **Which request counts:** the first in a session with `x-claude-code-session-id`, no `x-claude-code-agent-id` (subagents are skipped) and a non-empty `tools` list (side calls such as titles send none). Text blocks that are one whole `<system-reminder>` are left out of the prompt. Tools are left out of the classification, because Claude Code's own tool names would match route `code` on every turn. These rules come from reading what Claude Code sends and are not yet checked against many real sessions.
- **What it costs others:** the picker answers one call at a time, so a routed `auto` request that arrives while a shadow call runs gets the router's route (outcome `busy`), as it would behind any other request.

## With a router key

If the router has `TRIODITIS_API_KEY`, `Authorization` still belongs to Anthropic, so the router key goes in its own header. `claude-t` sends it when `TRIODITIS_API_KEY` is set; by hand:

```sh
ANTHROPIC_CUSTOM_HEADERS="X-Trioditis-Key: <your router key>" ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

A request that carries the router key in `Authorization` or `x-api-key` is refused before anything is sent, whether it is the whole value or follows a word such as `Bearer`. The key inside a longer string, or encoded, is not recognised.

Without the header, the router answers 401, and Claude Code keeps retrying instead of showing the error (16 tries in 90 seconds when tested on 18 November 2026). If Claude Code seems to hang with a router key set, check `logs/security.jsonl` for `invalid or missing router key`.

## Limits

- Trioditis can't send Claude Code to a local or non-Claude model. Anthropic doesn't support routing Claude Code to non-Claude models through any gateway, and the base URL changes where requests go, not which model answers ([source](https://code.claude.com/docs/en/llm-gateway), read 18 November 2026).
- It's for your own machine and your own login. The router binds `127.0.0.1`; don't share one router between people's logins.
- Fast mode's availability check and WebFetch's domain check call `api.anthropic.com` directly, so they don't pass through Trioditis and aren't counted.
- The privacy detectors don't run on this path: what Claude Code sends goes to Anthropic as it is.
- `claude-t` trusts whatever answers `/health` at `TRIODITIS_URL`. On a machine other people log in to, another local process could take the port while the router is down and would receive your login token. It refuses a URL that is plain `http` to another machine, or that has an `@` in it.
- A stream you interrupt is recorded with the token counts that had arrived by then, so its output count is too low.
- Claude Code 2.1.296 turns tool search off for any base URL that isn't Anthropic's, and then sends every tool's full definition on every turn: with many MCP tools that was 289 tools and about 1.4 MB a request, and autocompact kept refilling (tested 21–22 November 2026). Versions 2.1.322, 2.1.325 and 2.1.330 keep tool search on. On an older version, set `ENABLE_TOOL_SEARCH=true` (`claude-t` does); the gate passes tool search's blocks through unchanged.
- Request bodies over `limits.max_body_bytes` (20 MiB by default) are refused with a 413. Raise it in `config.yaml` if long sessions with images hit it.
