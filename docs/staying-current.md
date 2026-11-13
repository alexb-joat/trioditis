# Staying current: outcomes, the daily refit and model facts

Models launch, change price and retire every few weeks, and a router tuned once drifts out of date. Trioditis keeps its own record of how each model did and checks the configured models against a sourced list of model facts. Every number it reports comes with how sure it is, and nothing it finds changes routing on its own: you decide, and the change is yours to apply.

## What each call records

Next to the model, tokens, cost and latency, every call stores:

- **why the answer ended** (`finish_reason`: `stop`, `length`, `tool_calls`, ...), for streams too;
- **why no model answered**, when none did (`rate_limited`, `budget_exceeded`, `upstream_error`);
- **a fingerprint of the conversation** (a hash, never the text), to notice when the same request comes back within 2 minutes;
- **cached prompt tokens**, where the provider reports them. OpenAI and OpenRouter do. Anthropic's OpenAI-compatible endpoint, which Trioditis uses, does not support prompt caching ([Anthropic's page](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk), read 13 November 2026), so Trioditis records no cached tokens for Claude calls.

`trioditis usage --outcomes` turns these into rates per model, route, tool or day ([spending-and-limits.md](spending-and-limits.md#usage-reports)).

## The daily refit

```bash
python -m trioditis refit --dry-run     # see the result, write nothing
python -m trioditis refit               # write it
python -m trioditis refit --json        # the whole fitted document
```

`refit` reads the metrics database (read-only), your config, `prices.json` and, if configured, the model facts. It writes two files to `~/.hlidskjalv/`:

- **`trioditis-fitted.json`**: for each model over the last 28 days (`--days`), its error, cut-off, asked-again and cache-hit rates with 95% intervals, p50 and p95 latency, and a daily error chart; with a plan ledger, the [plan weights](#plan-weights). Every number carries its count, its window and its method (the `methods` block says which).
- **`trioditis-notebook.jsonl`**: one line for each finding that wasn't there at the last refit. Nothing new means nothing written, so the notebook reads as a history of changes, not a daily copy.

The Linux installer (`deploy/linux/install-service.sh`) adds a user timer that runs `refit` once a day; `journalctl --user -u trioditis-refit` shows its output.

### The daily error chart

A rate that moves from 4% to 6% in a day is usually noise. The chart is an EWMA control chart: an average that weights recent days more, against limits set by how much a daily rate of that size varies by chance (the binomial spread at the baseline's day size).

- The first 10 days with at least 10 calls set the baseline.
- After that, a day on which the average leaves its limits is a **drift alarm**, recorded once per excursion, not once per day it stays out.
- With fewer days of data the chart says "not enough data" and how much it needs, instead of guessing.

### Plan weights

Given a plan-usage ledger ([`plan import`](spending-and-limits.md#plan-usage)), `refit` also fits how many meter points each model and effort costs: points per million tokens, with the number of stretches it rests on and a 95% interval. A plan meter is hard to read this way. It shows whole percents, and everyone on the account raises it (other people, the chat app, another computer), while the ledger holds only what was tracked here. So a weight comes only from stretches where the rise is plausibly yours:

- **Stretches**: runs of consecutive meter readings in which one model and effort sent at least 90% of the tracked tokens, by every token measure. One idle reading right after a run joins it, because the meter can show the last messages a reading late. Rounding cancels inside a run, so a stretch is off by under one point however long it is.
- **Quiet only**: a stretch counts only when the meter stood still in every idle reading within 30 minutes either side of it. Untracked use *during* a stretch can't be seen, and would make a weight look heavier; the output says how many points rose with nothing tracked, so you can judge.
- **The weight**: sum of rises over sum of tokens across the quiet stretches, with a ratio-estimator interval that never assumes less noise than the rounding gives. Readings more than 15 minutes apart, or across a reset, are skipped.
- **Token measure**: uncached (input + output + cache writes), output only, or all tokens including cache reads. Each meter uses the one under which each model's stretches agree best, and the fitted file lists how well each fitted.
- **Relative weights**: each model and effort against the one with most stretches, with an interval on the log scale.
- A weight needs 3 quiet stretches and 10 points of rise; until then it says "not enough data" and what it needs. Models that never ran alone (subagents running beside the main model, say) are listed but get no weight.

The same section carries result-signal rates per model and effort, from events that record them (Euboulia's: suggestion followed, answer regenerated, asked again, up votes), with Wilson intervals. Claude Code's transcripts carry none.

A measured weight is a proposal. Its `evidence` block (`kind: measured`, `status: proposed`) is shaped for the team rules file, and it goes there only after you review it. Nothing is applied, and the notebook gets one line when a weight first becomes measurable (and again if its token measure changes).

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

## Measuring the router

`trioditis eval-routes` answers one question: how often does the router pick the route you meant? It runs each prompt of a labelled file through the same classifier a `model: auto` request uses (one user message, no session pin, no tools), compares the route with the label, and calls no chat model. It makes no routing decision for you either: it only reports.

```
trioditis eval-routes --labels labels.csv
```

The config is the usual one (`--config`, `TRIODITIS_CONFIG`, else `config.yaml`; the option works before or after the command).

**The label file** is a UTF-8 CSV whose first row names the columns. Two are required, `prompt` and `expected_route`; any others (an id, a note) are ignored. `expected_route` must be a route in your config. Header names may differ in case and spacing (`Expected Route`). Row numbers in error messages count the header as row 1, and a prompt that appears twice is an error, because counting it twice makes the result look more certain than it is.

```
prompt,expected_route,note
"Why does this Python function throw a KeyError?",code,
"Draft a short follow-up email after a sales call",writing,
"hi there",quick,greeting
```

**What it prints**: the number of prompts, then, for keywords only and (when the config has `embeddings:` and the local provider answers) keywords plus embeddings, the accuracy as `k/n` with its 95% Wilson interval, the same per route, and a confusion table (rows are the expected route, columns the predicted one). Without embeddings, or when the provider doesn't answer, it says so in one line and still prints the keyword row. The same files and config print the same text, apart from the date and the last paragraph: the first run that records a proposal says so, and a later run says it is already in the notebook.

The result depends on the label set. Prompts you chose to test the router with are not a sample of what you will send it, and a small set gives a wide interval: read the interval, not the percentage. Label prompts from your own work, including the odd ones, and keep the set once you have started comparing numbers.

**The threshold sweep** (only with the embeddings row, a config with at least two routes and at least 20 labelled prompts; otherwise one line says why it was skipped) tries `embeddings.threshold` from 0.40 to 0.80 in steps of 0.05, plus your own value if it is not on that grid. The prompts are sorted by a hash of their text and cut in two exact halves, the first half (rounded up) to pick and the rest to report, so the order of your file does not matter. The best threshold on the pick half (ties go to the one closest to yours, then the lower one) is scored against yours on the report half, so the figure you read was not the one the choice was tuned on. Both settings are scored on the same rows, so it counts the report-half rows where they disagree and runs an exact two-sided sign test on them: if the two were equally good, each disagreement would be a coin toss, and `p` is the chance of a split at least this lopsided. A proposal goes into the notebook (`--notebook`, default `~/.hlidskjalv/trioditis-notebook.jsonl`) only when the picked value scores higher on the report half and `p` is below 0.05; otherwise the output says why nothing is recorded (the picked value did not score higher, or the difference could be chance). A proposal is recorded once for the same labels (in any order), embeddings model, route keywords, patterns and examples, and pair of thresholds; change any of them and a new one can follow. `--dry-run` shows the line without writing it. **Nothing is applied**: your config is never changed, and you edit `embeddings.threshold` yourself if you agree.

**The one network call**: embedding the prompts and each route's `examples` with the configured local embeddings provider (for example Ollama), the same call the router makes. Nothing else leaves your machine, and each prompt is embedded once however many thresholds are tried.
