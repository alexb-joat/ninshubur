# Case study: how far to trust Ninshubur's qualifier

**Short answer: not yet as a filter.** With the prompt fixed in advance, no Claude model was shown to agree with one person's labels more often than a rater that always says "no". The best of them, Opus 5.5, found most of the companies that person would say yes to, but also said yes to many they wouldn't.

<!-- gen:summary -->
With the pre-registered prompt P0, the best Claude model (Opus 5.5) agreed with the labeller on 67 of 100 companies on seed 7, against 68 for a rater that always says "no" (gap -1.0 points, 95% interval -11.0 to +9.0): not shown better. On seed 8 it agreed on 13 of 19 against 9 (+21.1 points, -10.5 to +52.6): not shown better at this sample size; the pre-registered rival prompt P4 was not shown better than P0 (-15.8 points, -36.8 to +5.3). For the job the qualifier does, finding the "yes" companies, Opus with P0 found 7 of 11 on seed 7 and 3 of 4 on seed 8, at $0.0075 and $0.0081 per company assessed at list price.
<!-- /gen -->

Every number on this page is printed from the evaluation reports by `scripts/case_study.py`, and `--check` fails if any of them differs or if a number in the prose was typed by hand ([how to rebuild](#rebuild-and-check)). The reports themselves stay private because their row tables name the companies, so a reader outside can check the method, the code and the attack tests, but not recompute the agreement numbers.

## The task

Ninshubur collects public postings and ranks the companies behind them against a profile: a question with must and must-not rules. The qualifier (`ninshubur qualify`) asks a model that question about each company, answers yes, no or unsure, and keeps only answers that cite evidence lines it was shown, so a digest carries one suggestion per company.

The question this page answers: when the qualifier says yes, no or unsure, how often does it match what the person behind the profile would say, and what does that cost?

## The data

One person, the author, labelled every company yes, no or unsure from the same evidence lines a model sees: the sheet shows each company's collected lines, and a model is asked on exactly those cells, never on live data.

<!-- gen:data -->
| Sheet | Drawn | Drawn from | n | yes | no | unsure |
|---|---|---|---|---|---|---|
| seed 7 | 16 Nov | 119 companies collected by 16 Nov | 100 | 11 | 68 | 21 |
| seed 8 | 23 Nov | the 19 of those 119 not drawn for seed 7 | 19 | 4 | 9 | 6 |
<!-- /gen -->

The labels were frozen before any rater ran: each labelled sheet was copied to a read-only file whose hash every report records, and the reports record when each model's output was written.

<!-- gen:freeze -->
| Sheet | Labels file sha256 (first 16) | Labels frozen (file time) | Earliest model output after | Model output older than the labels |
|---|---|---|---|---|
| seed 7 | `d346f779933f9434` | 23 Nov 06:56:05 UTC | llama3.2:3b, first stored answer 23 Nov 07:20:29 UTC, 24 min 24 s after | opus-draft-16nov (rater file last written 16 Nov) |
| seed 8 | `0aab4c127e2774e8` | 23 Nov 14:57:21 UTC | opus-P0, rater file last written 23 Nov 14:58:47 UTC, 1 min 26 s after | none |
<!-- /gen -->

Times are file times. For seed 7, the local answers are stored one by one with their time, so the order is plain. For seed 8, a rater file's time is its *last* write, so on its own it doesn't show the run started after the freeze; the run's answer log, checked by hand (its creation time, which the reports don't record), was created after the freeze. The one model output older than the labels is a draft rater (dated in the table), made by Opus in an interactive session before labelling began; it is reported with the exploratory results, never as a blind rater.

## Method

- **Raters.** Claude models (Haiku, Sonnet, Opus 5.5), called by a private rater script ("CC"), one call per company; an answer that cites no evidence line it was shown counts as failed. Two small local models through Ollama on the author's laptop. Every rater saw the same rows.
- **Pre-registered before any model ran:** score against the frozen sheet only; a failed or ungrounded answer counts as a disagreement. Primary: three-label agreement and Cohen's kappa. Secondary: precision and recall for "yes", because the qualifier's job is finding the yes companies. Baseline: answer "no" every time. Confirmatory: prompt P0 on seed 7; P0 against P4 against the baseline on seed 8. Everything else is exploratory and labelled that way below.
- **Added afterwards:** F1 for "yes" (in this run, beside kappa) and cost. Neither was in the pre-registration.
- **Intervals.**

<!-- gen:method -->
Agreement intervals are Wilson 95% intervals. Kappa, F1 and the paired gaps use a percentile bootstrap over rows: 10,000 resamples, seed 7, the same draws for every rater, so two raters are always compared on the same resampled rows. Resamples where F1 is undefined (no "yes" in the truth or the answers) are dropped. A local model takes over the bulk qualify role only if its paired gap to the best Claude rater has a lower bound of at least -0.1 and it fails at most 5 answers.
<!-- /gen -->

**Prompts.** P0 is the profile's question exactly as the sheets were drawn with it (its hash is on the sheet). The later prompts were written by reading results, so they are named here by hash with one line each; their text holds personal criteria and isn't published.

<!-- gen:prompts -->
| Prompt | Hash (first 8) | What it is | Runs |
|---|---|---|---|
| P0 | `4a298b36` | the profile's question as the sheets were drawn with it, written before any labelling; pre-registered | seed 7: confirmatory, exploratory; seed 8: confirmatory, exploratory |
| P1 | `6522c05c` | P0 plus rules written after reading the seed-7 labels and disagreements | seed 7: exploratory |
| P2 | `7c6e8f61` | P1 with one rule removed and two criteria widened, after reading the seed-7 results | seed 7: exploratory |
| P3 | `60da85db` | P2 plus one exclusion rule fitted on 2 seed-7 rows | seed 7: exploratory |
| P4 | `6c1595be` | P3 plus one widened criterion; frozen before the seed-8 labels, not run on seed 7 | seed 8: confirmatory |
| P5 | `55d0d53c` | P4 without P3's exclusion rule, written after seeing the seed-8 results | seed 8: exploratory |
<!-- /gen -->

## Results: pre-registered

Seed 7, prompt P0:

<!-- gen:results-7 -->
| Rater | Model | Prompt | Answered | Agreement (95%) | Kappa (95%) | F1 for yes (95%) | Precision for yes | Recall for yes | Cost per company assessed | Cost per true yes |
|---|---|---|---|---|---|---|---|---|---|---|
| haiku-P0 | Haiku 5.5 | P0 | 100/100 | 51/100 = 0.51 (0.41 to 0.61) | 0.24 (0.11 to 0.38) | 0.50 (0.26 to 0.69) | 38% (8/21) | 73% (8/11) | $0.0003 | $0.0041 |
| sonnet-P0 | Sonnet 5.5 | P0 | 100/100 | 43/100 = 0.43 (0.34 to 0.53) | 0.17 (0.06 to 0.29) | 0.37 (0.11 to 0.59) | 31% (5/16) | 45% (5/11) | $0.0033 | $0.0660 |
| opus-P0 | Opus 5.5 | P0 | 100/100 | 67/100 = 0.67 (0.57 to 0.75) | 0.34 (0.19 to 0.49) | 0.47 (0.21 to 0.67) | 37% (7/19) | 64% (7/11) | $0.0075 | $0.1077 |
| always "no" | none | – | 100/100 | 68/100 = 0.68 (0.58 to 0.76) | 0.00 (by construction) | undefined (never says yes) | undefined (never says yes) | 0% (0/11) | – | – |
<!-- /gen -->

Seed 8, prompts P0 and P4:

<!-- gen:results-8 -->
| Rater | Model | Prompt | Answered | Agreement (95%) | Kappa (95%) | F1 for yes (95%) | Precision for yes | Recall for yes | Cost per company assessed | Cost per true yes |
|---|---|---|---|---|---|---|---|---|---|---|
| opus-P0 | Opus 5.5 | P0 | 19/19 | 13/19 = 0.68 (0.46 to 0.85) | 0.52 (0.20 to 0.83) | 0.60 (0.00 to 0.91) | 50% (3/6) | 75% (3/4) | $0.0081 | $0.0515 |
| opus-P4 | Opus 5.5 | P4 | 19/19 | 10/19 = 0.53 (0.32 to 0.73) | 0.19 (-0.16 to 0.53) | 0.33 (0.00 to 0.80) | 50% (1/2) | 25% (1/4) | $0.0091 | $0.1738 |
| always "no" | none | – | 19/19 | 9/19 = 0.47 (0.27 to 0.68) | 0.00 (by construction) | undefined (never says yes) | undefined (never says yes) | 0% (0/4) | – | – |
<!-- /gen -->

Paired comparisons, both raters on the same resampled rows:

<!-- gen:tests -->
The pre-registration names these comparisons but fixes no rule for calling one rater better. This doc reads "better" as: the paired 95% interval of the agreement gap lies wholly above 0, and "worse" as wholly below 0; an interval that includes 0 is "not shown better". That reading was set on 24 Nov 2026, after the results were known.

| Sheet | Comparison | Agreed | Gap (points) | 95% interval | Verdict |
|---|---|---|---|---|---|
| seed 7 | haiku-P0 vs always no | 51 vs 68 of 100 | -17.0 | -31.0 to -3.0 | worse |
| seed 7 | sonnet-P0 vs always no | 43 vs 68 of 100 | -25.0 | -40.0 to -10.0 | worse |
| seed 7 | opus-P0 vs always no | 67 vs 68 of 100 | -1.0 | -11.0 to +9.0 | not shown better |
| seed 8 | opus-P0 vs always no | 13 vs 9 of 19 | +21.1 | -10.5 to +52.6 | not shown better |
| seed 8 | opus-P4 vs always no | 10 vs 9 of 19 | +5.3 | -15.8 to +26.3 | not shown better |
| seed 8 | opus-P4 vs opus-P0 | 10 vs 13 of 19 | -15.8 | -36.8 to +5.3 | not shown better |
<!-- /gen -->

What this says plainly:

- **On seed 7 no Claude model beat "always no".** Haiku and Sonnet with P0 were worse than it; Opus with P0 was about level with it. Agreement rewards the many "no" rows, which a constant answer gets right for free.
- **Agreement is the wrong lens for the qualifier's job, and the job still isn't done well.** Opus with P0 found most of the companies labelled yes (recall), but most of its own yes answers on seed 7 were not yes for the labeller (precision). As a first pass that a person reads, that trade can be fine; as a filter that drops companies unseen, it isn't.
- **Seed 8 is too small to decide anything.** Opus with P0 scored above "always no", but the interval runs from a clear loss to a large gain. P4, the one rewritten prompt frozen before fresh labels, did worse than P0 on the same rows, though not by enough to call it worse.

## Results: exploratory

These rows are not tests. The prompts after P0 were written from the seed-7 results, so their seed-7 scores are in-sample and flatter them; the local models are listed with the verdict of the pre-registered adoption rule in each row.

Seed 7:

<!-- gen:exploratory-7 -->
| Rater | Model | Prompt | Answered | Agreement (95%) | Kappa (95%) | F1 for yes (95%) | Precision for yes | Recall for yes | Cost per company assessed | Cost per true yes | Why exploratory |
|---|---|---|---|---|---|---|---|---|---|---|---|
| llama3.2:3b | llama3.2:3b (local) | P0 | 99/100 | 38/100 = 0.38 (0.29 to 0.48) | 0.15 (0.08 to 0.22) | 0.29 (0.15 to 0.42) | 17% (11/65) | 100% (11/11) | $0.0000 | $0.0000 | local model, run 1, 2; adoption rule: not shown non-inferior (gap to opus-P0 -29.0 points, -40.0 to -18.0) |
| llama3.2:3b | llama3.2:3b (local) | P1 | 100/100 | 25/100 = 0.25 (0.18 to 0.34) | 0.06 (-0.03 to 0.14) | 0.35 (0.19 to 0.50) | 22% (10/46) | 91% (10/11) | $0.0000 | $0.0000 | local model, run 3; adoption rule: not shown non-inferior (gap to opus-P1 -45.0 points, -58.0 to -31.0) |
| qwen2.5-coder:7b | qwen2.5-coder:7b (local) | P0 | 100/100 | 45/100 = 0.45 (0.36 to 0.55) | 0.12 (-0.01 to 0.24) | 0.38 (0.18 to 0.56) | 26% (8/31) | 73% (8/11) | $0.0000 | $0.0000 | local model, run 4, 5; adoption rule: not shown non-inferior (gap to opus-P0 -22.0 points, -33.0 to -11.0) |
| opus-draft-16nov | Opus 5.5, 16 Nov draft | own | 100/100 | 71/100 = 0.71 (0.61 to 0.79) | 0.41 (0.25 to 0.55) | 0.48 (0.22 to 0.69) | 39% (7/18) | 64% (7/11) | – | – | written 16 Nov, before labelling; no token counts; can't be run again |
| haiku-P1 | Haiku 5.5 | P1 | 100/100 | 61/100 = 0.61 (0.51 to 0.70) | 0.26 (0.11 to 0.42) | 0.29 (0.00 to 0.59) | 67% (2/3) | 18% (2/11) | $0.0003 | $0.0165 | prompt written after seeing seed-7 results (in-sample) |
| sonnet-P1 | Sonnet 5.5 | P1 | 100/100 | 60/100 = 0.60 (0.50 to 0.69) | 0.27 (0.12 to 0.42) | 0.27 (0.00 to 0.56) | 50% (2/4) | 18% (2/11) | $0.0036 | $0.1819 | prompt written after seeing seed-7 results (in-sample) |
| opus-P1 | Opus 5.5 | P1 | 100/100 | 70/100 = 0.70 (0.60 to 0.78) | 0.30 (0.14 to 0.46) | 0.42 (0.12 to 0.67) | 50% (4/8) | 36% (4/11) | $0.0081 | $0.2017 | prompt written after seeing seed-7 results (in-sample) |
| opus-P2 | Opus 5.5 | P2 | 100/100 | 73/100 = 0.73 (0.64 to 0.81) | 0.44 (0.28 to 0.59) | 0.55 (0.29 to 0.75) | 44% (8/18) | 73% (8/11) | $0.0078 | $0.0981 | prompt written after seeing seed-7 results (in-sample) |
| opus-P3 | Opus 5.5 | P3 | 100/100 | 75/100 = 0.75 (0.66 to 0.82) | 0.44 (0.26 to 0.60) | 0.70 (0.42 to 0.89) | 67% (8/12) | 73% (8/11) | $0.0080 | $0.0997 | prompt written after seeing seed-7 results (in-sample) |
<!-- /gen -->

Seed 8:

<!-- gen:exploratory-8 -->
| Rater | Model | Prompt | Answered | Agreement (95%) | Kappa (95%) | F1 for yes (95%) | Precision for yes | Recall for yes | Cost per company assessed | Cost per true yes | Why exploratory |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen2.5-coder:7b | qwen2.5-coder:7b (local) | P0 | 19/19 | 8/19 = 0.42 (0.23 to 0.64) | 0.16 (-0.13 to 0.45) | 0.29 (0.00 to 0.58) | 20% (2/10) | 50% (2/4) | $0.0000 | $0.0000 | local model, run 6; adoption rule: not shown non-inferior (gap to opus-P0 -26.3 points, -52.6 to 0.0) |
| opus-P5 | Opus 5.5 | P5 | 19/19 | 12/19 = 0.63 (0.41 to 0.81) | 0.43 (0.09 to 0.74) | 0.60 (0.00 to 0.91) | 50% (3/6) | 75% (3/4) | $0.0081 | $0.0513 | prompt written after seeing these results |
<!-- /gen -->

- The rewritten prompts score higher on seed 7, the sheet they were written from. The one rewrite carried to fresh labels (P4 on seed 8) lost ground against P0, so the seed-7 gains are best read as fitting that sheet.
- Neither local model met the adoption rule, so Claude keeps the bulk qualify role.
- The Opus draft scores well, but it predates the labels, kept no token counts and can't be run again.

## Cost

Cost here is a **list-price equivalent**: tokens as each call reported them, priced at the published per-token prices in Ninshubur's own provider-facts export. The Claude calls actually ran on a subscription, so this is what the same calls would cost as API calls, not what was paid. Local runs cost nothing at list price; electricity and the laptop's time aren't counted. Cost per true yes divides a rater's cost by the companies where both it and the labeller said yes.

<!-- gen:cost -->
Prices: `provider-facts.json` (sha256 `3f1c78cbfc5e4846`), dated 2026-10-30, taken from https://platform.claude.com/docs/en/about-claude/pricing; per million tokens: Haiku 5.5 $0.1 in / $0.5 out; Opus 5.5 $4 in / $20 out; Sonnet 5.5 $2 in / $10 out. Input and output are priced separately. In the reports' words: list-price equivalents: what these calls would cost at the published per-token prices on the file's date, not what was paid; local models cost 0, electricity isn't counted.

| Sheet | Rater | Model | Rows with tokens | Input tokens | Output tokens | Median latency per row | List-price cost |
|---|---|---|---|---|---|---|---|
| seed 7 | llama3.2:3b | llama3.2:3b (local) | 100/100 | 53,644 | 5,897 | 6.4 s | $0.0000 |
| seed 7 | llama3.2:3b | llama3.2:3b (local) | 100/100 | 64,823 | 6,531 | 6.5 s | $0.0000 |
| seed 7 | qwen2.5-coder:7b | qwen2.5-coder:7b (local) | 100/100 | 53,948 | 5,189 | 12.9 s | $0.0000 |
| seed 7 | opus-draft-16nov | Opus 5.5, 16 Nov draft | 0 | – | – | – | no token counts recorded |
| seed 7 | haiku-P0 | Haiku 5.5 | 100/100 | 119,489 | 41,001 | 89.7 s | $0.0324 |
| seed 7 | sonnet-P0 | Sonnet 5.5 | 100/100 | 116,079 | 9,803 | 1.8 s | $0.3302 |
| seed 7 | opus-P0 | Opus 5.5 | 100/100 | 115,664 | 14,577 | 4.8 s | $0.7542 |
| seed 7 | haiku-P1 | Haiku 5.5 | 100/100 | 129,775 | 40,213 | 3.3 s | $0.0331 |
| seed 7 | sonnet-P1 | Sonnet 5.5 | 100/100 | 127,661 | 10,850 | 1.8 s | $0.3638 |
| seed 7 | opus-P1 | Opus 5.5 | 100/100 | 127,279 | 14,891 | 4.7 s | $0.8069 |
| seed 7 | opus-P2 | Opus 5.5 | 100/100 | 128,557 | 13,534 | 5.5 s | $0.7849 |
| seed 7 | opus-P3 | Opus 5.5 | 100/100 | 131,262 | 13,614 | 5.3 s | $0.7973 |
| seed 8 | qwen2.5-coder:7b | qwen2.5-coder:7b (local) | 19/19 | 10,589 | 948 | 12.9 s | $0.0000 |
| seed 8 | opus-P0 | Opus 5.5 | 19/19 | 22,473 | 3,234 | 6.2 s | $0.1546 |
| seed 8 | opus-P4 | Opus 5.5 | 19/19 | 25,589 | 3,573 | 6.6 s | $0.1738 |
| seed 8 | opus-P5 | Opus 5.5 | 19/19 | 24,945 | 2,701 | 6.2 s | $0.1538 |
<!-- /gen -->

The Haiku run with P0 has a long median latency because its timings include time spent waiting in a queue; later runs time the call alone.

## The crawler's attack table

The qualifier only ever sees evidence that Ninshubur's crawler collected, so the crawler's limits are part of what you're trusting. Its explorer (the part that visits sites no one has reviewed yet) has a table of attacks, one test per row, in [`tests/test_explorer_attacks.py`](../tests/test_explorer_attacks.py). Each row drives a hostile site through the explorer's own path (frontier, gate, shared fetcher) on a simulated network, so no real host is contacted, and asserts that the attack is stopped. Some rows also assert the log entry; the table says which.

<!-- gen:attacks -->
20 test functions in 3 sections, 44 cases collected; 7 of the 20 functions also assert what the explorer's log recorded. Result of `python -m pytest tests/test_explorer_attacks.py -q` in this build: **44 passed**.

| Section | Attack (test name) | Cases | Checks the log |
|---|---|---|---|
| where it goes | a name resolving to a denied address is never contacted | 10 | yes |
| where it goes | a redirect to an internal target is offered never followed and refused | 10 | yes |
| where it goes | a location header httpx cannot parse ends the fetch unfollowed | 1 | yes |
| where it goes | a redirect to a public name that resolves private is refused when its turn comes | 1 | no |
| where it goes | dns rebinding cannot swap the address after the check | 1 | no |
| where it goes | wildcard subdomain trap gets four hosts then nothing | 1 | yes |
| where it goes | calendar trap is capped by shape and by date | 1 | no |
| where it goes | soft404 site is stopped by its probe | 1 | yes |
| where it goes | endless generated maze stops its site | 1 | no |
| what it touches | gzip bomb is stopped near the cap | 1 | no |
| what it touches | ten megabyte page is cut at the explorer cap | 1 | no |
| what it touches | slow drip page hits the total deadline | 1 | no |
| what it touches | images and binaries are refused before the body | 1 | no |
| what it touches | robots redirect off site closes the host | 1 | no |
| what it touches | oversized robots is read to the limit not ignored | 1 | no |
| kill switch | stop file blocks every request and leaves the queue alone | 1 | no |
| kill switch | stop file is checked before each connection not once per fetch | 1 | no |
| kill switch | the explorer keeps its own logs ledger and database | 1 | no |
| kill switch | a redirect target is one hop further and refused past max hops | 1 | yes |
| kill switch | a redirect to another scheme counts only when it names an address | 7 | yes |
<!-- /gen -->

Still to come, in later sessions, because they need the parser or the web app: hidden Unicode and alt-text injection, hostile HTML in a title, a forged cross-site write, and a wrong Host header.

## Limits

- **One labeller.** The author labelled every row, and the notes show the labels reflect the author's own fit, not only the question as written. No second person labelled, so there is no human-to-human agreement to compare against.
- **Small samples.** Seed 8's intervals are wide enough that most comparisons on it can't come out either way.
- **In-sample prompt edits.** The prompts after P0 were tuned on seed 7. Of the rewrites, only P4 was frozen before it met fresh labels.
- **Rules set after the results.** The reading of "better" in the paired comparisons, F1 and cost were all added after the runs; the doc says so where each appears.
- **Prices** come from one export dated <!-- gen:prices-date -->30 Oct 2026<!-- /gen -->. Subscription calls are priced as if they were API calls.
- **Private inputs.** The reports, sheets and rater files name companies and hold personal criteria, so they stay on the author's machine. What's public: the code that produced them, this page's rebuild script, and the attack tests.
- **Freeze evidence is file times** plus one creation time checked by hand (seed 8), not a signed timestamp.

## Rebuild and check

The reports come from `ninshubur evaluate`, run on each frozen sheet with every rater file named by `--rater`, stored local answers picked up from the database, and `--prices` pointing at the provider-facts export. Then, from the repo root:

```bash
python scripts/case_study.py REPORT.json...                              # print every generated fragment
python scripts/case_study.py --check docs/case-study.md REPORT.json...   # succeeds only if every number matches
python scripts/case_study.py --update docs/case-study.md REPORT.json...  # rewrite the fragments from the reports
```

`--check` also runs the attack tests for their result line. How `evaluate` scores a sheet, with its flags, is in [docs/USAGE.md](USAGE.md).
