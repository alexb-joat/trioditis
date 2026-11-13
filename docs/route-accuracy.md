# Route accuracy: the study

How often does the router pick the route a person would pick? This page holds the method, the data and
the results behind every accuracy number Trioditis publishes. The command is
[`trioditis eval-routes`](staying-current.md#measuring-the-router).

Status, 13 November 2026: the plan below was committed first (commit `bb6cf32`). The second set has since
been written and labelled by two models; see [What has been done](#what-has-been-done). The maintainer
still has to settle the rows where the labellers disagree, so the threshold test has not run and the
router has not seen the second set.

## What is already known (first set, 100 prompts)

`tests/fixtures/routes.csv`: 100 prompts, 25 per route of `config.example.yaml`. A small Claude model
(Haiku) wrote them from the four route descriptions alone, and the maintainer confirmed every route.

| Setting | Right | 95% interval (Wilson) |
|---|---|---|
| Keywords only | 40/100 | 30.9–49.8% |
| Keywords + embeddings, threshold 0.55 (the shipped value) | 46/100 | 36.6–55.7% |

A sweep over the threshold picked 0.45 on one half of this set. On the other half 0.45 got 34/50 right
and 0.55 got 22/50 (the two disagreed on 18 rows, 15 of them in favour of 0.45; exact sign test, two-sided,
p = 0.008).

These numbers are a first reading, and they have three weaknesses. One person and one model stand behind
the labels. Fifty rows is a small test. And 0.45 was found on this set, so this set can no longer test it
fairly.

## The plan, written before the new data exists

### New data

- 320 new prompts, 80 per route. A model that is given only the four route descriptions writes them and
  is told to open no files. They are written after this page is committed.
- Each prompt gets a stratum when it is written: **plain** (it uses the words people usually use for
  that kind of task) or **indirect** (it describes the need without those words). 40 of each per route.
- A prompt that is a near copy of another prompt, in either set, is removed before labelling
  (`difflib` ratio of 0.8 or more on lower-cased text). The count removed is reported.

### Labels

- The writer's intended route is the first label.
- A second, different model labels all 420 prompts blind: it sees the prompts in shuffled order and the
  four route descriptions, and it sees neither the first label nor the stratum.
- Agreement between the two is reported as Cohen's kappa with a 95% interval, for each set.
- Where the two agree, that route is the label. Where they disagree, the maintainer decides, and may
  drop a prompt that has no single right route. The counts of settled and dropped rows are reported.

### The test of the threshold

- **Hypothesis.** With `embeddings.threshold: 0.45` the router picks the labelled route more often than
  with 0.55. Config: `config.example.yaml`; embeddings: `nomic-embed-text` on a local Ollama.
- **Data.** The new prompts only, with their final labels. The first 100 are left out, because 0.45 was
  chosen on them.
- **Method.** Both settings classify every prompt. Only the prompts where exactly one setting is right
  count. Exact sign test, two-sided (`trioditis_metrics.stats.sign_test`).
- **Decision rule.** If p < 0.05 and 0.45 is ahead, 0.45 becomes the value in `config.example.yaml` in
  the next release. In every other case 0.55 stays. The result is published either way.
- **Once.** The test runs one time. No other threshold is tried on these prompts, and the router is not
  run on them at all before the labels are final.
- **What this n can show.** On the first set the two settings disagreed on 36% of the test rows, and 83%
  of those disagreements favoured 0.45. If the new prompts disagree as often (about 115 rows), the test
  finds a 65:35 split with 89% probability and a 60:40 split with 54%. If they disagree on only a
  quarter of the rows (80), a 65:35 split is found with 72%. Computed exactly from the binomial
  distribution.

### What gets reported

- **The headline number** is accuracy on the new prompts with the threshold the decision rule leaves in
  place, with its Wilson interval. Nothing was tuned on those prompts.
- Accuracy per stratum and per route, with intervals. These are descriptions; no test is run on them.
- **Baselines** on the same prompts: always answering with the default route, and a small local model
  that picks a route from the four descriptions. The first shows what costs nothing, the second what a
  model call per request would buy.
- The first 100 are reported separately as the development set.

### Limits, known in advance

- The prompts are synthetic, in English, and written by a language model.
- Both labellers are language models from the same vendor; a person decides only the disagreements in
  the second set.
- Everything is measured on the example config. Your own routes, keywords and examples give a different
  number; run `eval-routes` on your own labels.
- One user message per prompt: no conversation history, no tools, no session pin.

## What has been done

Everything here is from 13 November 2026. The files are in [`route-accuracy/`](route-accuracy/).

**Writing.** Four runs of Claude Haiku 5.5 (`claude-haiku-5-5`), one per route, each wrote 80 prompts from
[`writer-instructions.txt`](route-accuracy/writer-instructions.txt): 320 prompts, 40 plain and 40
indirect per route, 16 per route marked borderline by the writer. Each run made one tool call, the one
that saved its file. Five prompts were near copies of an earlier prompt and were removed
([`removed-near-copies.csv`](route-accuracy/removed-near-copies.csv): one reasoning, four quick), which
leaves 315: 80 code, 79 reasoning, 80 writing, 76 quick.

**Second labels.** Two runs of Claude Sonnet 5.5 (`claude-sonnet-5-5`) labelled all 415 prompts, half
each, from [`labeller-instructions.txt`](route-accuracy/labeller-instructions.txt). The prompts were
shuffled with a fixed seed, and the sheet held an id and the prompt, nothing else.

**Agreement** between the writer's route and the second label
(`python scripts/route_study.py agreement docs/route-accuracy/labels.csv`):

| Set | Same label | Cohen's kappa (95% interval) |
|---|---|---|
| First, 100 prompts | 96/100 | 0.95 (0.89–0.99) |
| Second, 315 prompts | 308/315 | 0.97 (0.95–0.99) |
| Both, 415 prompts | 404/415 | 0.96 (0.94–0.98) |

The interval comes from 2,000 bootstrap resamples of the prompts. All 11 disagreements are on prompts the
writer had marked borderline. Read the kappa with one caution: prompts written to fit a route are clearer
than what people type unprompted, so two labellers agree on them more easily than they would on real
traffic.

Both labels for every prompt are in [`labels.csv`](route-accuracy/labels.csv).

**The local-model baseline.** `llama3.2:3b` (Q4_K_M, on a local Ollama, temperature 0, seed 0) was given
the four route descriptions and asked for one word per prompt
(`python scripts/route_study.py model-baseline docs/route-accuracy/labels.csv --model llama3.2:3b --label writer_route`).
Every reply held a route name. Its answers are in
[`baseline-llama3.2-3b.csv`](route-accuracy/baseline-llama3.2-3b.csv).

| Set | Right | 95% interval (Wilson) |
|---|---|---|
| First, 100 prompts (final labels) | 48/100 | 38.5–57.7% |
| Second, 315 prompts (writer's label; 7 rows not yet settled) | 205/315 | 59.7–70.1% |

On the second set it got 103/155 plain and 102/160 indirect prompts right, and by route 58/80 code,
59/79 reasoning, 63/80 writing and 25/76 quick: it rarely answers `quick` (39 of its 415 answers). The
second-set row will be scored again on the final labels; at most 7 rows can change.

