# Staying current: outcomes, the daily refit and model facts

Models launch, change price and retire every few weeks, and a router tuned once drifts out of date. Trioditis keeps its own record of how each model did and checks the configured models against a sourced list of model facts. Every number it reports comes with how sure it is, and nothing it finds changes routing on its own: you decide, and the change is yours to apply.

## What each call records

Next to the model, tokens, cost and latency, every call stores:

- **why the answer ended** (`finish_reason`: `stop`, `length`, `tool_calls`, ...), for streams too;
- **why no model answered**, when none did (`rate_limited`, `budget_exceeded`, `upstream_error`);
- **a fingerprint of the conversation** (a hash, never the text), to notice when the same request comes back within 2 minutes;
- **cached prompt tokens**, where the provider reports them. OpenAI and OpenRouter do. Anthropic's OpenAI-compatible endpoint, which Trioditis uses, has no prompt caching, so Claude calls always pay the full input price there.

`trioditis usage --outcomes` turns these into rates per model, route, tool or day ([spending-and-limits.md](spending-and-limits.md#usage-reports)).

## The daily refit

```bash
python -m trioditis refit --dry-run     # see the result, write nothing
python -m trioditis refit               # write it
python -m trioditis refit --json        # the whole fitted document
```

`refit` reads the metrics database (read-only), your config, `prices.json` and, if configured, the model facts. It writes two files to `~/.hlidskjalv/`:

- **`trioditis-fitted.json`**: for each model over the last 28 days (`--days`), its error, cut-off, asked-again and cache-hit rates with 95% intervals, p50 and p95 latency, and a daily error chart. Every number carries its count, its window and its method (the `methods` block says which).
- **`trioditis-notebook.jsonl`**: one line for each finding that wasn't there at the last refit. Nothing new means nothing written, so the notebook reads as a history of changes, not a daily copy.

The Linux installer (`deploy/linux/install-service.sh`) adds a user timer that runs `refit` once a day; `journalctl --user -u trioditis-refit` shows its output.

### The daily error chart

A rate that moves from 4% to 6% in a day is usually noise. The chart is an EWMA control chart: an average that weights recent days more, against limits set by how much a daily rate of that size varies by chance (the binomial spread at the baseline's day size).

- The first 10 days with at least 10 calls set the baseline.
- After that, a day on which the average leaves its limits is a **drift alarm**, recorded once per excursion, not once per day it stays out.
- With fewer days of data the chart says "not enough data" and how much it needs, instead of guessing.

## Model facts

Add the path of a `provider-facts.v1` file to `config.yaml`:

```yaml
facts: ~/euboulia/src/catalog/provider-facts.json
```

Then `trioditis check`, the startup log and `refit` report:

- **Configured models listed as deprecated or retired**, or retiring within 30 days. Each line quotes the fact, the facts' own note and the page it was read from. Read the note: a status can be narrower than it looks. OpenAI's models page, for example, marks models retired from ChatGPT that are still on the API.
- **Models "allowed to retire" soon.** Anthropic gives most models a "not before" date, the earliest day they may retire. Within 30 days of it, a legacy model is reported as "may retire from".
- **Configured models the facts don't list.** Their status is unknown, and the report says so instead of calling them fine.
- **Candidates to test**: active models of the providers you already use that aren't in your config. They are never added for you.
- **Price changes**: when the facts and `prices.json` disagree, `refit` records a proposal and the command that applies it (`trioditis prices import <facts>`). Prices change only when you run it.
- **Stale facts**: a facts file older than 30 days is reported, since prices and dates may have moved since.
