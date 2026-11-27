# Configuration reference

Every key `config.yaml` can hold, with its default and its label. The labels (**stable**, **experimental**, **internal**) mean what [compatibility.md](compatibility.md) says. `trioditis init` writes a starting config from [config.example.yaml](../config.example.yaml); `trioditis check` validates one.

The file is read with a safe YAML loader. Keys are case-sensitive. `<name>` stands for a name you choose: letters, digits and `._:/-`, starting with a letter or digit, up to 128 characters. A key the loader doesn't know is ignored today, except inside `route_picker`, `reinforce` and `plan`, where it is an error. `tests/test_contract.py` checks this page against the loader, so every key below is one Trioditis really reads.

## Top level

| Item | Label | Default | What it does |
|---|---|---|---|
| `providers` | stable | required | the servers Trioditis may call; at least one |
| `models` | stable | required | the models clients can reach; at least one |
| `routes` | stable | required | kinds of request and the models that serve each |
| `default_route` | stable | required | the route used when no route's signals match; must name a route |
| `embeddings` | stable | off | a local embeddings model that compares prompts with each route's `examples` |
| `route_picker` | stable | off | a local model that picks the route for a conversation's first turn |
| `privacy` | stable | on | the check that keeps sensitive prompts on local models |
| `limits` | stable | see below | request size, timeouts and fallbacks |
| `logging` | stable | see below | the decision log |
| `facts` | experimental | none | the path of a `provider-facts.v1` file; `check` and `refit` report model status, retirement dates and price changes from it |
| `reinforce` | experimental | off | turns on the reinforcement loop ([staying-current.md](staying-current.md)) |
| `plan` | experimental | see below | what `trioditis plan import` reads when run with no arguments |

## providers

| Item | Label | Default | What it does |
|---|---|---|---|
| `providers.<name>.base_url` | stable | required | the OpenAI-style API root, such as `http://localhost:11434/v1`. `https` is required except for `localhost`, `127.0.0.1`, `::1` and `host.docker.internal`; a user or password in the URL is refused |
| `providers.<name>.api_key_env` | stable | none | the name of the environment variable that holds the key. A provider that names one whose variable is empty is skipped. A literal `api_key` key is refused: keys never go in this file |
| `providers.<name>.local` | stable | `false` | `true` means prompts sent here stay on your machine or network: sensitive prompts may go here, and no proxy is used |
| `providers.<name>.stream_usage` | stable | `true` | asks for the token usage at the end of a stream (`stream_options`); set `false` for a provider that rejects it (Trioditis also retries once without it when a 400 names it) |
| `providers.<name>.admin_key_env` | experimental | none | an environment variable with an admin key, used only to read this provider's account spend for `/v1/accounts`; never sent with requests. A literal `admin_key` is refused |

## models

| Item | Label | Default | What it does |
|---|---|---|---|
| `models.<name>.provider` | stable | required | the provider that serves it. The names `auto` and `route/…` are reserved |
| `models.<name>.id` | stable | the model's name | the model id sent to the provider |
| `models.<name>.capabilities` | stable | none | a list; `vision` is needed for requests with images and `tools` for requests with tools |
| `models.<name>.context_tokens` | stable | `8192` | the model's context window; a longer request skips this model |

## routes

| Item | Label | Default | What it does |
|---|---|---|---|
| `routes.<name>.models` | stable | required | model names in preference order; later ones are fallbacks |
| `routes.<name>.description` | stable | empty | what the route is for; required for every route when `route_picker` is on |
| `routes.<name>.keywords` | stable | none | words that point a prompt to this route (case-insensitive) |
| `routes.<name>.patterns` | stable | none | regular expressions that point a prompt here (case-insensitive, each under 500 characters) |
| `routes.<name>.examples` | stable | none | example prompts, compared with the request when `embeddings` is on |
| `routes.<name>.tool_keywords` | stable | none | words matched inside the request's tool names (a coding agent's tools say "code") |
| `routes.<name>.local_only` | stable | `false` | `true` keeps every request on this route on local models |
| `routes.<name>.prefer` | stable | `configured` | `configured` (list order), `cheapest` (local first, then the estimated cost) or `fastest` (past latency) |

## embeddings

| Item | Label | Default | What it does |
|---|---|---|---|
| `embeddings.provider` | stable | required | must be a `local: true` provider |
| `embeddings.model` | stable | required | the embeddings model on it, such as `nomic-embed-text` |
| `embeddings.threshold` | stable | `0.45` | the similarity a prompt needs to count toward a route. 0.45 won the held-out test with `nomic-embed-text` ([route-accuracy.md](route-accuracy.md)); other models score on other scales, so measure with `trioditis eval-routes` |

## route_picker

| Item | Label | Default | What it does |
|---|---|---|---|
| `route_picker.provider` | stable | required | must be a `local: true` provider: the picker reads every first prompt |
| `route_picker.model` | stable | required | the model id on that provider, such as `qwen2.5-coder:7b` |
| `route_picker.timeout_ms` | stable | `2500` | 100 to 60000; with no answer in time, the router's own route is used |
| `route_picker.max_chars` | stable | `200` | 1 to 4000; longer prompts skip the picker (or are cut, with `long_prompts: cut`) |
| `route_picker.long_prompts` | stable | `skip` | `skip` or `cut`: with `cut` the picker reads the first `max_chars` characters (how often that picks the right route is not measured) |
| `route_picker.keep_alive_minutes` | stable | none | 1 to 1440: after each picker call, asks Ollama to keep the model loaded this long. Needs a `base_url` ending in `/v1`. None: the server's own keep-alive applies |
| `route_picker.shadow_claude_code` | experimental | `false` | also names a route for each Claude Code session's first turn, written to `shadow.jsonl` only; nothing is served from it |

## privacy

| Item | Label | Default | What it does |
|---|---|---|---|
| `privacy.enabled` | stable | `true` | `false` turns the whole check off |
| `privacy.builtin_detectors` | stable | `true` | email addresses, card numbers, IBANs, US social security numbers, API keys, private keys and `password=` assignments |
| `privacy.keywords` | stable | none | your own words that mark a prompt sensitive (case-insensitive) |
| `privacy.patterns` | stable | none | your own regular expressions (case-insensitive, each under 500 characters) |

## limits

| Item | Label | Default | What it does |
|---|---|---|---|
| `limits.max_body_bytes` | stable | `20971520` (20 MiB) | larger request bodies are refused with 413 |
| `limits.upstream_timeout_s` | stable | `120` | seconds to wait for a provider before trying the next model |
| `limits.max_fallbacks` | stable | `3` | how many more models to try after the first fails |
| `limits.session_ttl_s` | stable | `1800` | how long a conversation stays on the route its first turn got |

## logging

| Item | Label | Default | What it does |
|---|---|---|---|
| `logging.path` | stable | `logs/decisions.jsonl` | the decision log, relative to the folder `serve` runs in; `null` turns it off. `security.jsonl` and `shadow.jsonl` go next to it |
| `logging.log_prompts` | stable | `false` | `true` also writes the conversation text to the log |

## reinforce

| Item | Label | Default | What it does |
|---|---|---|---|
| `reinforce.labels` | experimental | required | the labelled gate set: a CSV with `prompt` and `expected_route`, read on this machine only |

## plan

| Item | Label | Default | What it does |
|---|---|---|---|
| `plan.claude_code` | experimental | `false` | imports token counts from Claude Code's transcripts |
| `plan.meter_file` | experimental | none | a file of plan meter readings |
| `plan.person` | experimental | none | a label put on imported events, such as a first name |
