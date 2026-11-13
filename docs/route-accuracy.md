# Route accuracy: the study

How often does the router pick the route a person would pick? This page holds the method, the data and
the results behind every accuracy number Trioditis publishes. The command is
[`trioditis eval-routes`](staying-current.md#measuring-the-router).

Status, 13 November 2026: the plan below is written down. The second label set does not exist yet, and
no result on it has been seen.

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
