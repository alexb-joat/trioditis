# Security model

How Trioditis is built to resist misuse, what it deliberately does not cover, and how to run it safely. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Threat model

Trioditis sits between your apps and every LLM you use, so it sees all prompts and holds all provider keys. The design assumes:

- **Dependencies can be hijacked** (as LiteLLM's PyPI package was on 2026-03-24). Mitigation: minimal dependency set, exact versions with hashes, install with `--require-hashes`, `pip-audit` on every push, Dependabot updates held 7 days before proposing.
- **The network is hostile.** The server binds `127.0.0.1` by default and refuses to bind anything else unless `TRIODITIS_API_KEY` (≥32 chars) is set. Keys are compared in constant time. No docs/OpenAPI endpoints are exposed; `/health` answers without a key and returns only `ok` and whether this build has the Anthropic passthrough (`anthropic_gate`).
- **The browser is on the network too.** Without a key, anyone who can reach `127.0.0.1` is trusted, and a web page open in your browser can reach it: by DNS rebinding (its requests then carry a `Host` that is not ours) or by a plain cross-site POST (a foreign `Origin`). Both are refused with 403 and logged; `*.localhost` and loopback addresses are the only names the router answers to. POST bodies must be `application/json` (415 otherwise), so a form or `text/plain` request that needs no CORS preflight is refused as well. With a key set, the key is the defence: a page cannot send it, so remote UIs that hold it keep working from any origin.
- **Prompts may contain secrets or personal data.** Built-in and custom detectors force local-only routing; if no local model fits, the request is refused with 422; there is no setting that lets it reach a cloud model. Embeddings must use a local provider, since embedding a prompt sends it somewhere. The Anthropic passthrough ([claude-code.md](claude-code.md)) is the exception: it relays Claude Code's requests to Anthropic as bytes and runs no detector on them.
- **Clients are not trusted to choose destinations.** Upstream URLs come only from the config file. The passthrough has one destination, `https://api.anthropic.com`; only the router's own environment can change it (`TRIODITIS_GATE_UPSTREAM`, https only, no user or password in the URL), never a request. Plain `http` is only allowed for local hosts, URLs with embedded credentials are rejected, and HTTP redirects are not followed.
- **Logs leak.** Decision logs and the metrics database contain metadata and a SHA-256 prefix of the prompt, never the prompt itself, unless `logging.log_prompts: true`. The session id (`X-Session-Id` or `user`) is stored only as a SHA-256 prefix too; the calling tool is stored by name only (`X-Trioditis-Client`, or the first word of `User-Agent`, kept only if it is a short lower-case name). Both files are created with mode `0600`.
- **Upstream providers are separate trust zones.** Each provider receives only its own key; the client's `Authorization` header and the session id are never forwarded. The passthrough is the one path that forwards the caller's credential: Claude Code's `Authorization` or `x-api-key` goes to Anthropic unchanged, because it is Anthropic's to check. The router key is refused if it arrives as either of those headers, bare or after a word such as `Bearer`.

## Which outbound calls exist

No telemetry, update checks or remote config. Every connection Trioditis opens is in this list.

While `trioditis serve` runs:

- **Requests you send** go to the providers your config names. Cloud calls obey `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` if you set them. Calls to `local: true` providers never do: a proxy would otherwise receive the prompts the privacy check kept on this machine.
- **Claude Code's requests through the passthrough** go to `https://api.anthropic.com`.
- **The route picker.** With `route_picker:` configured, the router sends the first turn of each `auto` conversation (every turn, for a client that sends no session id and no tools; sensitive prompts too, at most 4,000 characters) to that provider's `/chat/completions`, which must be `local: true`. When that call outlives `timeout_ms`, one `GET /api/ps` to the same provider (beside its `/v1` base URL, a quarter-second limit) asks Ollama whether the model was still loading. With `route_picker.shadow_claude_code: true`, each Claude Code session's first turn is also classified by the local picker and the local embeddings provider (both must be `local: true`).
- **Embeddings.** With `embeddings:` configured, prompts and each route's examples go to that provider's `/embeddings`, which must be `local: true`.
- **Installed models.** Every 10 minutes, and when `/v1/hardware` is asked, `GET /api/tags` to each `local: true` provider. The answer goes to `hardware.json` in the data folder (`~/.hlidskjalv`, or `HLIDSKJALV_HOME`).
- **Account readouts.** Every 5 minutes, and when `/v1/accounts` is asked, read-only `GET` requests to a cloud provider's own account API, on the host in that provider's `base_url`. This runs only when a cloud provider with a key is one of `openrouter.ai`, `api.anthropic.com` or `api.openai.com`. OpenRouter: `/api/v1/key` with your normal key, and `/api/v1/credits` if `admin_key_env` holds a management key. Anthropic: `/v1/organizations/cost_report`, only with an Admin API key in `admin_key_env`. OpenAI: `/v1/organization/costs`, only with an admin key in `admin_key_env`. Without the admin key nothing is sent to those two. The answers go to `accounts.json` in the data folder and never affect routing.

Commands:

- `trioditis check` makes one read-only `GET {base_url}/models` to each *local* provider (2-second timeout, no proxy, no redirects) and nothing to cloud providers. With a route picker configured it also sends that local provider two calls on a made-up message.
- `trioditis hardware` asks local providers for `GET /api/tags`.
- `trioditis eval-routes` sends your labelled prompts to the config's local embeddings provider, and nowhere else.
- `trioditis reinforce run` sends each prompt of your labelled gate set to the config's local embeddings provider and local route picker, and nowhere else.
- Every other command reads and writes local files only.

## Controls at a glance

| Risk | Control |
|---|---|
| Malicious/hijacked dependency | 4 direct deps, every package hash-pinned (`--require-hashes`), `pip-audit` in CI, Dependabot with a 7-day cooldown |
| Router exposed to the network | Binds `127.0.0.1` by default; refuses any other bind without a ≥32-char API key |
| A web page in your browser reaching the router | Without a key, requests with a foreign `Host` (DNS rebinding) or `Origin` (cross-site POST) get 403 and a line in `logs/security.jsonl`; POST bodies must be `application/json` |
| Key leakage | Keys only in env vars (config loader rejects inline keys); client's router key is never forwarded upstream; local models get no key |
| Sensitive data sent to the cloud by accident | Privacy detectors force local-only routing or reject (not on the Anthropic passthrough, which relays Claude Code's requests unread) |
| Open proxy / SSRF | Upstream URLs only from config, and one fixed host for the Anthropic passthrough; plain `http` only for local hosts; redirects not followed |
| Prompt content in logs | Logs hold route, model, latency, detector names and a hash — not prompt text (opt-in only) |
| Abuse / DoS | Body size cap, upstream timeouts, bounded fallbacks and session cache |
| Compromised CI | Workflow is read-only, actions pinned to commit SHAs, no persisted credentials |

## Scope and limits

What Trioditis does not try to do, so you can plan around it:

- Multi-user isolation and per-user rate limits (single shared key today).
- Prompt-injection defence for the *content* of responses: Trioditis routes, it does not inspect model output.
- TLS termination: put a reverse proxy (Caddy, Tailscale) in front before exposing beyond localhost.
- Detectors are pattern-based and will miss some sensitive data; treat them as a safety net, not a guarantee.

## Operating checklist

- Generate the API key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Keep `.env` and `config.yaml` out of Git (already in `.gitignore`).
- Protect `.env`: `chmod 600 .env` on Linux/macOS (Trioditis warns if others can read it); on Windows keep the repo inside your user profile, whose permissions already exclude other users.
- Pin the Docker base image to a digest before deploying.
- Enable GitHub secret scanning and push protection on the repository.
- Rotate any provider key that was ever pasted into a chat, screenshot or log.
