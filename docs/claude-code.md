# Claude Code through Trioditis

Trioditis can sit between Claude Code and Anthropic on your own machine. Requests go through unchanged, with your own claude.ai login, and Trioditis counts each call's tokens. It doesn't route these calls: Claude Code picks the model, as it does without Trioditis.

What you get: every call's input, output and cache tokens in `trioditis usage --plan`, including the calls Claude Code makes that its transcripts don't record. A later `trioditis plan import --claude-code` merges into the same rows instead of counting them twice.

## Attach

For one terminal session, from a clone of the repository:

```sh
scripts/claude-t            # same arguments as claude
```

`claude-t` checks that the router at `http://127.0.0.1:8787` has the passthrough (`/health` says `"anthropic_gate": true`). If it does, it sets `ANTHROPIC_BASE_URL` for that one process. If it doesn't, it starts Claude Code direct, without metering, and prints a line saying so. Plain `claude` is never affected.

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

- **Up:** the request body as bytes, every `anthropic-*` and `x-claude-code-*` header, and `Authorization` or `x-api-key`, unchanged. Trioditis sends only to `https://api.anthropic.com` and drops only its own `X-Trioditis-*` headers and the connection headers that belong to one hop.
- **Down:** the status, the headers Claude Code reads (`retry-after`, `x-should-retry`, `anthropic-ratelimit-unified-*`) and the body, unchanged. A stream is passed on chunk by chunk, `ping` events included. Error bodies aren't wrapped: Claude Code recovers from some errors by their exact wording.
- **Paths:** `POST /v1/messages` and `POST /v1/messages/count_tokens`. Claude Code's `HEAD /api/hello` warm-up gets a 404; the gateway guide says a gateway can reject it without breaking anything.

These are the requirements in Anthropic's [gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol), read 18 November 2026.

## What is kept

Counts only, never text: model, message id (hashed with your machine's salt), session id (hashed), whether a subagent sent it, and the input, output, cache-read and cache-write tokens. Your credential is passed on and never read, stored or logged. Trioditis only checks whether an `x-api-key` header is present:

- **No `x-api-key`** (your claude.ai login): the call goes to plan usage, next to `plan import`. It never counts toward the spending caps, because the plan's meters bill it, not per token.
- **`x-api-key` present** (an Anthropic API key): the call is billed per token, so it goes with the API calls (`trioditis usage`, route `passthrough`), priced from `prices.json` when the model is there, and counts toward the caps. Trioditis never blocks it; Claude Code would only stall.

## With a router key

If the router has `TRIODITIS_API_KEY`, `Authorization` still belongs to Anthropic, so the router key goes in its own header. `claude-t` sends it when `TRIODITIS_API_KEY` is set; by hand:

```sh
ANTHROPIC_CUSTOM_HEADERS="X-Trioditis-Key: <your router key>" ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

A request that carries the router key in `Authorization` is refused before anything is sent, so the router key never travels to Anthropic.

Without the header, the router answers 401, and Claude Code keeps retrying instead of showing the error (16 tries in 90 seconds when tested on 18 November 2026). If Claude Code seems to hang with a router key set, check `logs/security.jsonl` for `invalid or missing router key`.

## Limits

- Trioditis can't send Claude Code to a local or non-Claude model. Anthropic doesn't support routing Claude Code to non-Claude models through any gateway, and the base URL changes where requests go, not which model answers ([source](https://code.claude.com/docs/en/llm-gateway), read 18 November 2026).
- It's for your own machine and your own login. The router binds `127.0.0.1`; don't share one router between people's logins.
- Fast mode's availability check and WebFetch's domain check call `api.anthropic.com` directly, so they don't pass through Trioditis and aren't counted.
- Request bodies over `limits.max_body_bytes` (20 MiB by default) are refused with a 413. Raise it in `config.yaml` if long sessions with images hit it.
