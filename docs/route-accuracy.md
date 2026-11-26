# Route accuracy: the study

How often does the router pick the route a person would pick? This page holds the method, the data and
the results behind every accuracy number Trioditis publishes. The command is
[`trioditis eval-routes`](staying-current.md#measuring-the-router).

**Result, 13 November 2026.** On 315 held-out prompts the router picked the labelled route for 205
(65.1%, 95% interval 59.7–70.1%), on the example config with `embeddings.threshold: 0.45`. With the
earlier value, 0.55, it picked 134 (42.5%). The test that decided between the two was written down
before the prompts existed and ran once. The numbers are in [Results](#results).

The page keeps the order of the work: what was known, the plan (committed first, commit `bb6cf32`), what
was done, the results.

## What is already known (first set, 100 prompts)

`tests/fixtures/routes.csv`: 100 prompts, 25 per route of `config.example.yaml`. A small Claude model
(Haiku) wrote them from the four route descriptions alone, and the maintainer confirmed every route.

| Setting | Right | 95% interval (Wilson) |
|---|---|---|
| Keywords only | 40/100 | 30.9–49.8% |
| Keywords + embeddings, threshold 0.55 (the value shipped up to 0.5.0) | 46/100 | 36.6–55.7% |

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

**The local-model baseline.** `llama3.2:3b` (Q4_K_M, on a local Ollama, temperature 0, seed 0;
versions in [`environment.txt`](route-accuracy/environment.txt)) was given
the four route descriptions and asked for one word per prompt
(`python scripts/route_study.py model-baseline docs/route-accuracy/labels.csv --model llama3.2:3b --label writer_route`).
Every reply held a route name. Its answers are in
[`baseline-llama3.2-3b.csv`](route-accuracy/baseline-llama3.2-3b.csv).

| Set | Right | 95% interval (Wilson) |
|---|---|---|
| First, 100 prompts (final labels) | 48/100 | 38.5–57.7% |
| Second, 315 prompts (writer's label, before the 7 rows were settled) | 205/315 | 59.7–70.1% |

On the second set it got 103/155 plain and 102/160 indirect prompts right, and by route 58/80 code,
59/79 reasoning, 63/80 writing and 25/76 quick: it rarely answers `quick` (39 of its 415 answers). The
second-set row is against the writer's label; [Results](#results) has the score on the final labels.

**Settling.** The maintainer decided the seven second-set rows where the two labels differ
([`settled.csv`](route-accuracy/settled.csv)): six decisions follow the second labeller, one follows the
writer, and no row was dropped. `python scripts/route_study.py heldout` then wrote the test labels,
[`trioditis/data/routes-heldout.csv`](../trioditis/data/routes-heldout.csv): 315 prompts, 76 code, 78
reasoning, 83 writing and 78 quick. They were committed (`30d3080`) before the router was run on them. (They lived in `tests/fixtures/` until 0.8.0, which moved them into the package unchanged; the dated records below name the old path.)

## Results

All of it is from 13 November 2026, on `config.example.yaml`, with `nomic-embed-text` (137M, F16, digest
`0a109f422b47`) on Ollama 0.40.0 ([`environment.txt`](route-accuracy/environment.txt)).

### The test of the threshold

The test ran once, with the config still at 0.55
([`heldout-test.txt`](route-accuracy/heldout-test.txt) is its full output).

| `embeddings.threshold` | Right, of 315 | 95% interval (Wilson) |
|---|---|---|
| 0.55 | 134 = 42.5% | 37.2–48.1% |
| 0.45 | 205 = 65.1% | 59.7–70.1% |

The two settings differ on 95 prompts: 0.45 alone was right on 83 and 0.55 alone on 12. Exact sign test,
two-sided: p = 3 × 10⁻¹⁴. By the decision rule, **0.45 is now the value in `config.example.yaml`**, and
in the config `trioditis init` writes. The plan expected the settings to differ on about 115 prompts; they
differed on 95 (30%), and 87% of those favoured 0.45.

### How often the router is right

The same 315 prompts and labels for every row. Nothing was tuned on them.

| Who picks the route | Right, of 315 | 95% interval (Wilson) |
|---|---|---|
| Always the default route (`quick`) | 78 = 24.8% | 20.3–29.8% |
| The router, keywords only | 116 = 36.8% | 31.7–42.3% |
| **The router, keywords + embeddings at 0.45** | **205 = 65.1%** | **59.7–70.1%** |
| `llama3.2:3b`, choosing from the four descriptions | 202 = 64.1% | 58.7–69.2% |

The router with embeddings is level with the small local model, without a chat-model call per request; the
intervals overlap and no test was run on that difference. The two fail in different places. The model
got 57/76 code, 57/78 reasoning, 64/83 writing and 24/78 quick: it answered `quick` for only 28 of the
315 prompts. Its answers are not fully repeatable, even at temperature 0 with a fixed seed: this run
([`baseline-llama3.2-3b-heldout.csv`](route-accuracy/baseline-llama3.2-3b-heldout.csv), output in
[`baseline-llama3.2-3b-heldout.txt`](route-accuracy/baseline-llama3.2-3b-heldout.txt)) differs from the
first run on 12 of the 315 prompts, and the first run's answers score 206/315 on the same labels.

By route and by stratum, with the threshold at 0.45
([`heldout-at-0.45.txt`](route-accuracy/heldout-at-0.45.txt)). These describe; no test was run on them.

| Prompts | Right | 95% interval (Wilson) |
|---|---|---|
| Labelled code | 31/76 = 40.8% | 30.4–52.0% |
| Labelled reasoning | 45/78 = 57.7% | 46.6–68.0% |
| Labelled writing | 65/83 = 78.3% | 68.3–85.8% |
| Labelled quick | 64/78 = 82.1% | 72.1–89.0% |
| Plain wording | 116/155 = 74.8% | 67.5–81.0% |
| Indirect wording | 89/160 = 55.6% | 47.9–63.1% |

Where the misses go: of the 110 prompts routed wrongly at 0.45, 65 went to `quick`, the default route
(32 code, 21 reasoning, 12 writing). At 0.55 that was 172 of 181. The lower threshold
has a price on the other side: 14 quick prompts were pulled into another route, against 2 at 0.55. Code
is the weakest route: 32 of its 76 prompts still fall to `quick`.

### The development set

The first 100 prompts, reported apart because 0.45 was picked on half of them: keywords only 40/100, with
embeddings 46/100 at 0.55 and 78/100 (68.9–85.0%) at 0.45
([`first-set-rehearsal.txt`](route-accuracy/first-set-rehearsal.txt)). The 78% flatters the setting; quote the
held-out figure.

### To reproduce

```
python scripts/route_study.py heldout docs/route-accuracy/labels.csv \
    --settled docs/route-accuracy/settled.csv --out trioditis/data/routes-heldout.csv
trioditis eval-routes --labels trioditis/data/routes-heldout.csv --config config.example.yaml \
    --compare-threshold 0.55
```

The second command needs a local Ollama with `nomic-embed-text` and sends the prompts nowhere else. It
prints both thresholds on every row and tries no other value. A newer build of the embedding model can
move single prompts across the threshold.

### What was run on the held-out prompts

- The two labellers and the local-model baseline, before the labels were final. The router was not.
- The test, once (`heldout-test.txt`). Before it, the same command was rehearsed on the first set only
  (`first-set-rehearsal.txt`).
- One more run after the config changed to 0.45, for the per-route and per-stratum figures
  (`heldout-at-0.45.txt`). It used the same two thresholds.
- The local-model baseline again, scored on the final labels.

### Limits of the result

The limits [known in advance](#limits-known-in-advance) all hold. Three more came with the data:

- A person decided 7 of the 315 labels. The other 308 are labels two models agreed on.
- 0.45 is a value for `nomic-embed-text` and the example config's `examples`. Another embedding model
  has another scale; run the sweep on your own labels.
- Half of the prompts were written to avoid the usual words. Traffic with a different mix of plain and
  indirect prompts gives a different overall number; the per-stratum rows show the spread.

## A local model picks the route (19 November 2026)

**Question.** Does a local model, asked for the route directly, pick the labelled route more often than
the router (keywords, then embeddings at 0.45) on the same 315 held-out prompts? This is the evidence
behind the opt-in [`route_picker:`](../README.md#how-a-request-is-routed) block.

**Method.** `qwen2.5-coder:7b` on a local Ollama, one call per prompt: the route names and descriptions
of `config.example.yaml` as the instruction, the prompt fenced, temperature 0, seed 0, at most 8 tokens.
The route is the first word of the reply that names one; a reply with none counts as wrong. This is the
request `scripts/route_study.py model-baseline` sends, and the one `route_picker:` sends
(`tests/test_route_picker.py` pins it). The router had to reproduce its committed 205/315 first, and did.

**Pass mark, written at 18:17, before the run (its report is dated 18:23).** More than 205 of 315 right, and an exact
two-sided sign test against the router on the same prompts with p < 0.05. One run, nothing tuned on
these prompts.

**Result: pass.** qwen picked the labelled route for 231 of 315 (73.3%, 95% interval 68.2–77.9%), the
router for 205 (65.1%, 59.7–70.1%). On the prompts only one of them got right, qwen won 74 and the router
48 (p = 0.023).

| Route (labelled) | Router | qwen |
|---|---|---|
| code | 31/76 | 48/76 |
| reasoning | 45/78 | 77/78 |
| writing | 65/83 | 80/83 |
| quick | 64/78 | **26/78** |

The two err in opposite directions. qwen sent 33 of the 78 quick prompts to `reasoning` and 19 to
`writing`, routes that usually cost more. The router sends its misses down: 65 of its 110 misses went
to `quick`, the default route. Every reply named a route; no call failed.

**Time, on one machine** (a laptop CPU, no GPU; [environment.txt](route-accuracy/environment.txt)): median 1.05 s
per call once the model was loaded, 95th percentile 1.68 s, slowest 2.38 s (the held-out prompts: median
length 78 characters, at most 206). On 20 November the first
call `trioditis check` made took 17.8 s and the next 0.34 s (n = 1; the gap fits the model being loaded
into memory, which was not checked, and the second call repeated the first one's text). Later that day three cold starts took 16–21 s, and one more, with
no model loaded (`ollama ps`, which lists the models in memory, was empty), 19.7 s; in the router check below it was 15.6 s. A request that arrives while the model loads falls back to the router at the
default 2.5 s timeout; the call goes on, so the model finishes loading. Since 22 November 2026 such a
timeout is logged as outcome `cold` when Ollama's `/api/ps` doesn't list the model yet (it lists a model
only once it has loaded), and `eval-routes --log` also gives the picker's rate without cold starts; the
three timeouts in the first shadow counts were all cold. One picker call runs at a
time: until that call ends, other requests skip the picker (outcome `busy` in the decision log); after
it, they get the picker. A turn counts as a first turn when nothing is pinned for its conversation, and
a conversation is known by its session id (`X-Session-Id` or `user`) or, for a request with tools, by
its task. A client that sends neither is asked on every turn. `qwen2.5-coder:7b` is 4.7 GB on disk.

**Time by prompt length, same machine.** The call slows down as the prompt grows. On 20 November
`scripts/picker_timing.py` sent the picker's request five times per length, in a shuffled order, with a
different text each time (slices of this repository's docs), model loaded. The laptop was in ordinary
use: a browser and other programs running, memory short enough that swap was in use, 1-minute load
average 3.8 to 10.6 with the model's own threads included. Every call returned
([picker-timing.csv](route-accuracy/picker-timing.csv), one row per call with the server's token count).

| Characters | Median | Fastest | Slowest | Over 2.5 s |
|---|---|---|---|---|
| 100 | 1.03 s | 0.94 s | 1.37 s | 0 of 5 |
| 150 | 1.54 s | 1.30 s | 1.96 s | 0 of 5 |
| 200 | 2.04 s | 1.78 s | 2.45 s | 0 of 5 |
| 250 | 2.22 s | 1.79 s | 2.67 s | 1 of 5 |
| 300 | 2.61 s | 2.28 s | 3.17 s | 4 of 5 |
| 500 | 4.70 s | 3.15 s | 5.58 s | 5 of 5 |
| 750 | 6.72 s | 4.43 s | 8.57 s | 5 of 5 |
| 1,000 | 7.24 s | 6.52 s | 8.71 s | 5 of 5 |
| 1,500 | 11.76 s | 8.47 s | 13.05 s | 5 of 5 |
| 2,000 | 14.79 s | 13.51 s | 17.22 s | 5 of 5 |
| 4,000 | 27.60 s | 23.49 s | 34.30 s | 5 of 5 |

A first turn whose call passes `timeout_ms` waits the whole 2.5 s and then gets the router's route
anyway, so prompts over `max_chars` skip the picker. The default is 200, about the longest prompt of
the accuracy test (206 characters). At that length about one call in ten passed the default timeout:
none of 5 in the table above, 2 of 20 in the larger run below. One of the 315 held-out prompts is over 200 characters, so with the default it would skip
the picker; the accuracy above was measured with every prompt reaching it.

A larger run that evening, 20 calls per length with new texts, the laptop again in use (1-minute load
3.0 to 10.5; [picker-timing-n20.csv](route-accuracy/picker-timing-n20.csv), seed 20261122):

| Characters | Median | Fastest | Slowest | Over 2.5 s | 95% interval of that share |
|---|---|---|---|---|---|
| 100 | 1.18 s | 0.76 s | 2.15 s | 0 of 20 | 0 to 16% |
| 150 | 1.60 s | 1.10 s | 2.28 s | 0 of 20 | 0 to 16% |
| 200 | 1.94 s | 1.11 s | 2.78 s | 2 of 20 | 3 to 30% |
| 250 | 2.34 s | 1.42 s | 3.50 s | 7 of 20 | 18 to 57% |
| 300 | 2.54 s | 2.02 s | 3.39 s | 12 of 20 | 39 to 78% |

The intervals are Wilson intervals. A default of 150 would have had no call over the timeout in this
run, at the cost of more prompts skipping the picker.

Limits of these times: one machine in ordinary use, one afternoon and evening. The first table
is the second run of the afternoon, five calls per length; its first calls (about nine, none of them at
200 to 300 characters) ran while a test suite and a lint ran on the same machine. The first run, half an hour earlier with eight of the lengths
([picker-timing-first-run.csv](route-accuracy/picker-timing-first-run.csv), the same request from an
earlier copy of the script), had 2 of 5 calls over 2.5 s at 250 characters and medians of 1.32 s at 100
and 3.87 s at 500, so single cells move between runs. Another five-call run with new texts, made
while other checks ran ([picker-timing-other-seed.csv](route-accuracy/picker-timing-other-seed.csv),
seed 20261121), had 1 of 5 calls over 2.5 s at 200 characters, 1 of 5 at 250 and 3 of 5 at 300.
Repeating the first table's seed straight after a run does not measure new prompts
([picker-timing-same-seed.csv](route-accuracy/picker-timing-same-seed.csv)): 20 of its 40 calls at 250
characters and up came back in 0.31 to 1.27 s, the 4,000-character ones included, where no call that
long did in the other runs. Why was not checked. The script now draws a new seed for every run unless `--seed` is given. One call each earlier that day had taken less than half these medians (500 characters 1.8 s,
1,000 2.6 s, 4,000 10.7 s); those calls' records were not kept, nor what else the machine was doing. The texts were slices of Markdown docs, not chat prompts (500 characters were 193 to 272
tokens), so your prompts may count differently. And the script times the picker's call alone: in the
router the embeddings call runs at the same moment on the same server. To set `max_chars` for another
machine or model, run this from a clone of the repository:

```
python scripts/picker_timing.py --model qwen2.5-coder:7b --lengths 100,150,200,250,300 --n 20
```

and take the longest length at which few enough calls pass your
`timeout_ms`: each one that does costs that turn the whole wait.

**In the running router** (20 November, one run, made-up prompts;
[picker-router-check.txt](route-accuracy/picker-router-check.txt)). With the picker on and every model
the router forwards to replaced by a stub on the same machine, 12 requests gave these outcomes
(`max_chars` was 500 in this run, the default at the time): the
first turn, with the model not loaded, fell back at 2.5 s; Ollama finished that call after 15.6 s, and
two first turns sent before then were `busy`; a second turn, a prompt over `max_chars` and a tool list
matching `code` did not reach the picker; six later first turns got its answer in 0.67 to 1.32 s. One
`busy` turn still took 1.1 s, most likely because its embeddings call waited behind the picker's call on the same
Ollama server. Against a stub server that is not Ollama and takes 8 s on its first call, the router
kept that call open until it was answered; with that server stopped, the picker's call failed at once
and the request was served on the router's route in 19 ms. No other real server (llama.cpp, LM Studio, vLLM) was tried.

**Exploratory, no pass mark.** The committed `llama3.2:3b` answers scored 202 (no different from the
router, p = 0.87). Keeping the router's route whenever keywords decide, and asking qwen otherwise,
scored 229 and would need a model call on 86% of the prompts.

**Files.** [picker-heldout.txt](route-accuracy/picker-heldout.txt) (the full report, with per-stratum
rows and qwen's confusion matrix), [picker-heldout.csv](route-accuracy/picker-heldout.csv) (each row's
label and the route each method picked, no prompt text). The config hash in the report is the example
config before the commented `route_picker:` block was added; the routes are unchanged.

**To reproduce** (sends the prompts only to the local server):

```
python scripts/route_study.py model-baseline trioditis/data/routes-heldout.csv \
    --model qwen2.5-coder:7b --config config.example.yaml
```

**Limits.** Everything under [Limits of the result](#limits-of-the-result) holds. Also: one model at
temperature 0, one sample per prompt, and Ollama is not fully repeatable at temperature 0 (one prompt
of a separate set changed answer between two runs). The prompts are at most 206 characters, so long
agent prompts are untested, and none carried a tool list: that is why requests whose tools match a
route's `tool_keywords` keep the router's route and never reach the picker.

## The picker on longer prompts: what a 500-character limit costs in waiting (20 November 2026)

**Question.** `max_chars` 200 keeps the picker to short prompts, and the tables above show why: at 500
characters a call took about 4 to 5 s on this machine, well over the default 2.5 s timeout. Raising
`max_chars` therefore means raising `timeout_ms` with it. Which timeout does each length up to 500
characters need here, so that a call outlives it as rarely as the default does now?

**The test, written at 23:43 on 20 November, before the run** (this text was committed before the data existed).
`scripts/picker_timing.py` as committed, `--model qwen2.5-coder:7b --lengths 200,250,300,400,500 --n 20`,
a new seed, the routes of `config.example.yaml`: 100 calls in a shuffled order, each with a text the
server has not seen. The run starts with the model loaded and with no test suite, lint, download or
other model call running beside it. For each length and each timeout in the fixed list 2.5, 4, 5, 6, 8
and 10 s it reports how many of the 20 calls took longer, with a 95% Wilson interval.

- **The timeout a length needs** is the smallest in that list that at most 2 of its 20 calls outlive.
  Two of 20 is what the default has: 2 of 20 at 200 characters and 2.5 s in the larger run above.
- **Pass mark.** 500 characters passes if it needs 6 s or less. If it needs more, the longest listed
  length that needs 6 s or less is the one to consider, and if none over 200 does, 200 stays.
- **Validity.** If more than 8 of the 20 calls at 200 characters pass 2.5 s, the machine was in another
  state than in the run above; the run is then reported as it is and repeated once, and both are shown.
  A failed call ends the script and is handled the same way. No other repeat.

This test is about time only. Whether the picker still names the right route more often than the router
on prompts of 201 to 500 characters is a separate test that needs labelled prompts of that length; the
held-out prompts stop at 206. The shipped default stays 200 whatever this run shows: it is one machine.

**Result: pass. On this machine 500 characters needs a 5 s timeout** (run from 23:48 to 23:53 on 20 November,
seed 1795214896, [picker-timing-500.csv](route-accuracy/picker-timing-500.csv)). All 100 calls returned.

| Characters | Median | Fastest | Slowest | Over 2.5 s | Over 4 s | Over 5 s | Over 6 s | Timeout it needs |
|---|---|---|---|---|---|---|---|---|
| 200 | 2.02 s | 1.57 s | 3.73 s | 4 of 20 | 0 | 0 | 0 | 4 s |
| 250 | 2.27 s | 1.61 s | 3.56 s | 4 of 20 | 0 | 0 | 0 | 4 s |
| 300 | 2.43 s | 1.93 s | 4.18 s | 9 of 20 | 1 | 0 | 0 | 4 s |
| 400 | 3.26 s | 2.38 s | 5.60 s | 19 of 20 | 2 | 1 | 0 | 4 s |
| 500 | 3.73 s | 3.16 s | 6.18 s | 20 of 20 | 8 | 1 | 1 | 5 s |

No call took longer than 8 s. The 95% Wilson intervals: 0 of 20 is 0 to 16%, 1 of 20 is 1 to 24%, 2 of 20
is 3 to 30%, 4 of 20 is 8 to 42%, 8 of 20 is 22 to 61%.

What this says: with `max_chars: 500` and `timeout_ms: 5000`, a first turn of 400 to 500 characters
waited a median of 3.3 to 3.7 s here for the picker's route, and 1 call in 20 would have waited the
whole 5 s and then got the router's route. With 4 s, prompts up to 400 characters pass by the same
rule and 500 does not (8 of 20 over).

The validity check held, 4 of 20 calls at 200 characters passed 2.5 s against a limit of 8, but that is
twice the 2 of 20 of the earlier run: by this run the default pair of 200 characters and 2.5 s misses
more often than the rule's own reference rate.

Limits of this run: one machine, five minutes of one evening, 20 calls per length, so "1 of 20" could
be anything from 1 to 24%. The one 500-character call over 5 s was also over 6 s, so the 5 s and 6 s
columns do not differ there. The model was not in memory before the script's untimed first call loaded
it. The 1-minute load average rose from 1.3 at the start to 9.8 at the end (2.0 to 10.3 in the rows,
the model's own threads included); available memory fell from 6.6 GB to 2.0 GB and swap in use rose
from 9.5 GB to 10.8 GB. A model download had ended half a minute before the start; no test suite,
lint or other model call of ours ran beside it, and what the laptop's other programs did was not
recorded. The texts were slices of Markdown docs (500 characters were 200 to 297 tokens). The limits
written before the run hold too: the picker's call alone was timed, and longer calls keep the picker
busy for longer, which this run does not measure.

## Measuring on other machines: `trioditis results` (0.8.0)

The numbers above come from one laptop. `trioditis results` runs the same test anywhere: the picker and the
router on these 315 held-out prompts with the shipped settings (fixed in advance; nothing is swept or tuned on
them, so the set stays a test set), and the picker's waiting time at 100, 200, 500, 1,000 and 2,000 characters,
three calls each, on slices of the held-out prompts sent before the accuracy run. It writes the counts to
`trioditis-results.json` and asks whether to share them through GitHub's results form (default No).

What shared results can show: how picker models rank on this public set (accuracy barely depends on the
machine), and how long the picker takes across real hardware. What they cannot show: accuracy on real use.
These prompts were written for the test, so a ranking on them is a ranking on them, not on anyone's own
prompts. `--own-labels` adds counts on a person's own labelled prompts, never the prompts.

## Which small picker model: the plan, written before any run (24 November 2026)

- **Question.** Does any small model pick the route more often than `qwen2.5-coder:7b` on the public held-out
  set (231/315 on this laptop, 19 and 24 November)? A smaller picker that is as good waits less.
- **Models.** Eight tags taken from Ollama's library page on 24 November 2026, each confirmed to exist in
  Ollama's registry that day: `llama3.2:3b`, `qwen2.5:3b`, `qwen2.5-coder:3b`, `gemma3:4b`,
  `phi4-mini:3.8b`, `granite4:3b`, `ministral-3:3b`, `smollm2:1.7b`. The rule that chose them: general or
  coding instruct models of at most 4B parameters, not tagged "thinking" (the picker's request has no way
  to turn reasoning off), no vision or embedding models. No model is added after the first run.
- **Where.** `.github/workflows/bench.yml`, started by hand: a GitHub shared runner, Ollama 0.40.0 pinned
  by digest, the shipped settings, `nomic-embed-text` for the router. `qwen2.5-coder:7b` runs there too, so
  every comparison is between two runs on the same kind of machine.
- **Runs.** Two per model, the 7B included, because a repeat at temperature 0 changed 12 of 315 answers
  before. The test uses run 1. Run 2 is reported as how many answers changed. If run 2 would reverse a
  conclusion, both are shown and that conclusion is not claimed.
- **Test.** For each small model against the 7B: the prompts where exactly one of the two is right,
  exact two-sided sign test, Holm correction over the eight tests at 0.05. A model "beats" the 7B only if
  its corrected test passes and it is ahead.
- **What this n can show.** At Holm's first threshold (0.05/8), with 100 prompts where the two disagree,
  the test finds a 70:30 split with probability 0.88, a 65:35 split with 0.55 and a 60:40 split with 0.18;
  with 60 such prompts, 0.67, 0.35 and 0.12. Computed exactly from the binomial distribution. So a small
  model a few points behind or ahead will most likely read as "no difference shown", not as "equal".
- **Reported either way.** Every model's count with its 95% interval, per route, the sign tests, run 2's
  changes, and the runner's waiting times, labelled indicative only.
- **Limits.** These prompts were written for the test. A ranking on them is a ranking on them, not on
  real use. One shared runner type, so the timings say little about other hardware.
