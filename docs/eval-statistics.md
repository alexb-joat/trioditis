# Evaluation statistics: how to compare two route pickers

Trioditis is measured by counting: of n prompts, how many went to the route a person would pick. This page
teaches the statistics behind those counts, one method at a time. Each method lives in
[`trioditis_metrics/stats.py`](../trioditis_metrics/stats.py) (standard library only) or in
[`scripts/compare_results.py`](../scripts/compare_results.py). Each section uses the real numbers of one
worked example, and the full output of that example is in [The worked example](#the-worked-example).

The example sets two pickers side by side on the same 100 public prompts
([`tests/fixtures/routes.csv`](../tests/fixtures/routes.csv), 25 per route):

- **keywords**: the router's keyword rule alone, on `config.example.yaml`, with no embeddings.
- **llama3.2:3b**: a small local model that chose a route from the four route descriptions, in one saved
  run from 13 Nov 2026 ([`baseline-llama3.2-3b.csv`](route-accuracy/baseline-llama3.2-3b.csv)).

The numbers teach the methods. They are not a finding about either picker; [Limits](#limits) says why.

## Contents

- [The Wilson interval](#the-wilson-interval)
- [The paired table](#the-paired-table)
- [McNemar's test: exact, mid-p and chi-square](#mcnemars-test-exact-mid-p-and-chi-square)
- [The paired difference interval](#the-paired-difference-interval)
- [Power, and why it is a saw-tooth](#power-and-why-it-is-a-saw-tooth)
- [Disagreements needed](#disagreements-needed)
- [Stratified sampling and its estimate](#stratified-sampling-and-its-estimate)
- [Already in place: Holm and Cohen's kappa](#already-in-place-holm-and-cohens-kappa)
- [The worked example](#the-worked-example)
- [Limits](#limits)
- [References](#references)

## The Wilson interval

**Question.** One picker got k of n right. How sure can we be of its true share right?

**Formula.** With p = k/n and z = 1.96: (p + z²/(2n) ± z·√(p(1 − p)/n + z²/(4n²))) / (1 + z²/n).

**Use it** for any single share: right of 100, errors of 3,000 calls. It stays inside 0 to 1, and it is not
zero-width at 0 of n or n of n, as the plain p ± z·√(p(1 − p)/n) is.

**Don't use it** to compare two pickers on the same prompts. Two intervals that overlap do not show "no
difference", and two that don't overlap are not a paired test. Use the paired table and McNemar's test.

**In the example.** keywords 40/100, 30.9% to 49.8%. llama3.2:3b 48/100, 38.5% to 57.7%. The intervals
overlap a lot, but that is not the test.

**Function.** `wilson(k, n)`.

## The paired table

**Question.** On the same prompts, how often was each picker right, and how often were both?

**Formula.** Four counts that add up to n: both right, only the first right (b), only the second right (c),
both wrong.

**Use it** whenever two pickers answered the same prompts. Only b and c say which is better. A prompt both
got right, or both got wrong, says nothing about the difference.

**Don't use it** when the two pickers saw different prompts. Then the counts are not pairs.

**In the example.** Both right 20, only keywords 20, only llama3.2:3b 28, both wrong 32. So 48 prompts
carry all the evidence: 20 for keywords, 28 for llama3.2:3b.

**Function.** `paired_counts(first_right, second_right)`, one True or False per prompt for each picker.
`python scripts/compare_results.py table FIRST SECOND --out FILE` writes the same table, one row per prompt,
from two `--answers` files.

## McNemar's test: exact, mid-p and chi-square

**Question.** If the two pickers were equally good, how surprising is a split of b against c?

If they were equally good, each disagreement would be a fair coin. McNemar's test asks how often a fair coin
would split b + c tosses at least this unevenly. It comes in four forms.

| Form | Formula, with m = b + c and X ~ Binomial(m, ½) | Key in `mcnemar` | In the example |
|---|---|---|---|
| Exact | 2·P(X ≤ min(b, c)), at most 1 | `exact_p` | 0.312 |
| Mid-p | 2·(P(X ≤ min(b, c)) − ½·P(X = min(b, c))) | `mid_p` | 0.253 |
| Chi-square | (b − c)²/m, looked up with 1 degree of freedom | `chi2`, `chi2_p` | 1.333, p 0.248 |
| Chi-square with continuity correction | (\|b − c\| − 1)²/m | `chi2_cc`, `chi2_cc_p` | 1.021, p 0.312 |

**When to use which.**

- **Exact** never claims more than its level allows. With few disagreements it is cautious: it rejects less
  often than 5%. Fagerland, Lydersen and Laake (2013) recommend against reporting it for that reason.
  `scripts/compare_results.py compare` still decides by it, because a rule written down before the data
  must not over-claim; the price is power. The function `sign_test(b, c)` is the same test.
- **Mid-p** counts the observed split itself at half weight in each tail. It is less cautious. The same
  study found that it never went over its level in 9,595 simulated designs, but that is not guaranteed.
  They recommend it. Report it beside the exact p to show how much of the exact p is caution.
- **Chi-square** is the large-sample shortcut and the most powerful of the four. In the same study it went
  over its level in 29% of the designs, though not by much.
- **Chi-square with the correction** lands close to the exact p and is at least as cautious. The same study
  recommends against reporting it.

**Don't use** any of them when the pickers saw different prompts. Choose the form before you see the data,
not after.

**In the example.** All four p-values are well above 0.05. The data do not show that the two pickers differ.
In the mid-p, 0.312 − 0.253 = 0.059, the chance of exactly 20 of 48.

**Function.** `mcnemar(b, c)`.

## The paired difference interval

**Question.** How far apart are the two shares right, and how sure are we?

**Formula.** The difference is (b − c)/n. Its interval adds ½ to each of the four cells first: with
b̃ = b + ½, c̃ = c + ½, ñ = n + 2, it is (b̃ − c̃)/ñ ± z·√((b̃ + c̃) − (b̃ − c̃)²/ñ)/ñ (Agresti and Min's
"Wald+2").

**Use it** with McNemar's test. The p-value says whether there is a difference. The interval says how large
it could be.

**Don't use it** for two pickers on different prompts, or the plain Wald interval with few disagreements:
that one is too narrow, and it has zero width with none.

**In the example.** keywords minus llama3.2:3b: −8.0 points, from −21.2 to +5.5. The interval holds 0, as
the test said. It also holds a 21-point lead for llama3.2:3b, and a 5-point lead for keywords.

**Function.** `paired_difference(b, c, n)`.

## Power, and why it is a saw-tooth

**Question.** If one picker really wins a given share of the disagreements, what is the chance the exact
test notices?

**Formula.** For m disagreements, find every split k that the exact test rejects: 2·P(X ≤ k) < 0.05 with
X ~ Binomial(m, ½). The power is the chance of landing in one of those splits when each disagreement goes to
the first picker with probability s: the sum of the Binomial(m, s) chances of k and of m − k.

**Use it** before the data exist, to choose n. Use it after a result of "not shown to differ", to say what
the test could and could not have found.

**Don't use it** to turn a result into "the same". Low power means the test was weak, not that the pickers
are equal.

**The saw-tooth.** The exact test can only reject at whole counts. One more disagreement can move the
rejection line without adding a split, so power can fall. At a 65:35 split, 114 disagreements give 0.902 and
115 give 0.888. Near the example: 44 give 0.519, 46 give 0.433, 48 give 0.470 (`sign_test_power`, run on
25 Nov 2026).

**In the example.** 48 disagreements. The test rejects only at a split of 16 against 32 or more lopsided.
With a true 65:35 split it would have found it with a chance of 0.470. With 60:40, 0.215.

**Functions.** `sign_test_power(discordant, share)`. When the number of disagreements is not known in
advance, `paired_power(n, discordant_rate, share)` weighs each count m by its chance among n prompts.
At n = 100, a disagreement rate of 0.48 and a 65:35 split, it gives 0.5018.

## Disagreements needed

**Question.** How many disagreements does the exact test need to find a split with 80% power?

**Formula.** Two answers from the power above: the first m that reaches 80%, and the m from which every
count (up to 1,000) does. Because of the saw-tooth, the first can be followed by counts that fall short.
Plan on the second.

**Use it** to size a study. Count disagreements, not prompts: the prompts both pickers get right, or both
get wrong, add nothing.

**Don't use it** as a promise. It assumes a split that nobody knows in advance.

**In the example.** A 65:35 split needs 90 disagreements first, and every count from 97 on. A 60:40 split
needs 199, and every count from 210 on. The example has 48, so it could only find a large split.

**Function.** `discordant_needed(share, power=0.8)`.

## Stratified sampling and its estimate

**Question.** How do we score a small sample so that every route is present and the estimate holds for the
whole set?

**Formula.** Each stratum h (a route) of size N_h gets the whole part of size·N_h/N. The units left over go
to the largest remainders, ties by name. Inside a stratum the draw is a simple random sample without
replacement, with a fixed seed. The estimate is Σ W_h·p_h with W_h = N_h/N. Its standard error is
√(Σ W_h²·(1 − n_h/N_h)·s_h²/n_h), with s_h² the sample variance (divisor n_h − 1). The factor
(1 − n_h/N_h) credits drawing without replacement from a finite stratum. The interval is the estimate
± 1.96 standard errors, kept inside 0 to 1.

**Use it** when a small sample must hold every route, for example 20 prompts to label by hand. A plain
random draw of 20 from 4 routes can leave a route out.

**Don't use it** with fewer than 2 sampled rows in a stratum. One value has no spread, so there is no
standard error; the function then gives only the estimate.

**In the example.** 5 prompts per route, seed 0: keywords got 8 of 20 right. The estimate for the full set
is 40.0%, standard error 7.1 points, interval 26.1% to 53.9%. The full set gives 40/100. The interval holds it.

**Functions.** `stratum_allocation(sizes, size)`, `stratified_sample(items, key, size, seed)`,
`stratified_proportion(sample, population_sizes)`.

## Already in place: Holm and Cohen's kappa

**Holm's adjustment.** *Question:* with several candidates each tested against one baseline, how do we keep
the chance of any false "ahead" at 5%? *Formula:* sort the k p-values ascending; multiply the smallest by k,
the next by k − 1, and so on; each adjusted value is at least the one before, and at most 1. *Use it*
whenever more than one comparison is made from the same baseline. *Don't use it* for one comparison (it then
changes nothing). *Function:* `holm` in `scripts/compare_results.py`, used by `compare`.

**Cohen's kappa.** *Question:* two labellers labelled the same prompts. How much more do they agree than
their label frequencies would by chance? *Formula:* κ = (observed − chance)/(1 − chance), with a 95%
interval from 2,000 bootstrap resamples of the items (fixed seed). *Use it* for the agreement of two people,
or a person and a model, on labels. *Don't use it* to say which picker is right; it measures agreement, not
correctness. *Function:* `cohen_kappa(pairs)`.

## The worked example

Run on 25 Nov 2026, from the repository root:

```
python scripts/stats_walkthrough.py
```

It reads only committed files, asks no model and opens no connection. It refuses a config with a
`route_picker`. The full output, which a test checks word for word against the script:

```text
Two route pickers on the same 100 public development prompts (25 per route)
  keywords     the router's keyword rule alone (config.example.yaml); no model is asked
  llama3.2:3b  a small local model choosing from the route descriptions; one saved run

1. Right of 100, with the Wilson 95% interval
  keywords     40/100 = 40.0% (30.9% to 49.8%)
  llama3.2:3b  48/100 = 48.0% (38.5% to 57.7%)

2. The paired table: how the two pickers share the prompts
                      llama3.2:3b right   llama3.2:3b wrong
  keywords right                     20                  20
  keywords wrong                     28                  32

3. McNemar's test on the 48 prompts where they disagree (only keywords right 20, only llama3.2:3b right 28)
  exact two-sided p  0.312
  mid-p              0.253
  chi-square         1.333, p 0.248
  with correction    1.021, p 0.312

4. The difference in the share right, keywords minus llama3.2:3b (Wald+2 interval)
  -8.0 points (-21.2 to +5.5)

5. Power: the chance the exact test finds a real difference, given 48 disagreements
  a 65:35 split  0.470
  a 60:40 split  0.215
  disagreements needed for 80% power: the first count that reaches it; the count from which every count does
  a 65:35 split  90; 97
  a 60:40 split  199; 210

6. Right of 25 on each route, with the Wilson 95% interval (a description, not a test)
  route      keywords                                llama3.2:3b
  code       8/25 = 32.0% (17.2% to 51.6%)           9/25 = 36.0% (20.2% to 55.5%)
  reasoning  2/25 = 8.0% (2.2% to 25.0%)             18/25 = 72.0% (52.4% to 85.7%)
  writing    5/25 = 20.0% (8.9% to 39.1%)            13/25 = 52.0% (33.5% to 70.0%)
  quick      25/25 = 100.0% (86.7% to 100.0%)        8/25 = 32.0% (17.2% to 51.6%)

7. A stratified sample of 20 prompts (5 per route, seed 0), keywords only
  right in the sample  8/20
  estimate for the full set  40.0%, standard error 7.1 points, 95% interval 26.1% to 53.9%
  the full set, all 100 prompts  40/100 = 40.0% (30.9% to 49.8%)
  the interval holds the full-set value
```

**What it shows.** The two pickers are not shown to differ: 20 against 28 disagreements, exact p 0.312.
That does not mean they are the same. With 48 disagreements the test had about a 47% chance to find a true
65:35 split, and about a 22% chance for 60:40. The difference could be anywhere from 21 points for
llama3.2:3b to 5 points for keywords. Per route the pickers fail in different places: keywords get every
quick prompt and 2 of 25 reasoning prompts, llama3.2:3b 8 of 25 quick and 18 of 25 reasoning. Those rows
are a description, not a test. The stratified sample of 20 lands on 40.0%, the full-set value, with an
interval about 28 points wide.

## Limits

- The prompts are synthetic English, written by a model (Haiku) from the four route descriptions, with the
  routes confirmed by the maintainer ([route-accuracy.md](route-accuracy.md#what-is-already-known-first-set-100-prompts)).
- There are 100 of them. Small effects cannot show at this size.
- This set was in view while the example config was tuned: its 0.45 threshold was picked on half of it
  ([The development set](route-accuracy.md#the-development-set)). The keyword rule does not use the
  threshold, but the set is not a fair test of the example config.
- The llama3.2:3b picks are one run from 13 Nov 2026 (temperature 0, seed 0). Another run, version or
  machine can differ.
- For the held-out results, see [route-accuracy.md](route-accuracy.md#results). No number on this page comes
  from the held-out prompts.

## References

Only sources that were opened and read are listed, each with what was checked against it.

- Fagerland MW, Lydersen S, Laake P. The McNemar test for binary matched-pairs data: mid-p and asymptotic
  are better than exact conditional. *BMC Medical Research Methodology* 13:91, 2013.
  [doi:10.1186/1471-2288-13-91](https://doi.org/10.1186/1471-2288-13-91); full text read on 25 Nov 2026
  from Europe PMC (PMC3716987). Checked: `mcnemar` gives all eight p-values of its Table 6 to the printed
  four decimals (two worked examples, four forms each), in
  `tests/test_stats.py::test_mcnemar_reproduces_the_worked_examples_of_fagerland_lydersen_and_laake`.
  The statements above on mid-p and chi-square levels (9,595 designs; 29%) and on the continuity correction
  are from its results and conclusions.
- [route-accuracy.md](route-accuracy.md), this repository. Checked: three power figures it printed on 13 Nov
  2026, before its data existed (115 disagreements: 89% at 65:35, 54% at 60:40; 80: 72% at 65:35), in
  `tests/test_stats.py`; and keywords 40/100 (30.9–49.8%) in its first table, in
  `tests/test_stats_walkthrough.py`.

Named in the code but not opened for this page: Wilson's score interval, Agresti and Min (2005) for the
Wald+2 interval, Holm (1979), and Cochran's *Sampling Techniques* for the stratified estimate. Those
functions are checked against hand derivations and independent brute force in the tests (exact fractions,
full enumeration of every stratified sample of a small population), not against those texts.
