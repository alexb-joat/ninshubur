# Review: Ninshubur release prep

**Result:** finished · all 11 steps done and Done when (a)–(f) pass; Questions 2–9 below are open and each ran on its stated default, none blocks the merge · branch `worktree-release-prep` (worktree `/home/alexb/ninshubur/.claude/worktrees/release-prep`) · 11 commits ahead of `main` (aedbc01), nothing pushed

Written 25 Nov 2026 by the second run on this brief; the first run handed off after step 9 (context). `cockpit step` answered `unknown command` for steps 1–9, so no markers were logged for them; it worked for steps 10 and 11.

## Done when
Run from the worktree root, `PY=/home/alexb/ninshubur/.venv/bin/python`, 25 Nov 2026 16:57–17:05 UTC. Commands marked ◆ had `NINSHUBUR_PRIVATE_WORDS_FILE=logs/private-words.local.sha256` set (the local list of real words, outside the tracked tree).

- **(a)** ◆ `$PY -m pytest -q` → `1106 passed in 184.83s (0:03:04)`, exit 0. Main at launch: `973 passed`; 1106 − 973 = 133, the number of tests in the eight new files (see (d)). No `failed`, no `error`.
- **(b)** `$PY scripts/smoke.py` → last line `smoke ok`.
- **(c)** ◆ `$PY scripts/private_words.py check` → `private-words check: 186 files, 355887 tokens, 2 hashes, 0 hits`, exit 0 (the line before it: `private list: 1 hash(es) from NINSHUBUR_PRIVATE_WORDS_FILE`). This count was taken before this file replaced the hand-off note; the final run is in the closing message.
- **(d)** ◆ the eight test files → `133 passed in 39.30s`. Without the variable `tests/test_private_words.py` ends `23 passed, 1 skipped in 1.32s`; the skip says why (the real words are kept outside the repository, so CI and a fresh clone cannot check them).
- **(e)** `git diff main...HEAD --stat` → `24 files changed, 3552 insertions(+), 14 deletions(-)`, before this file's own change. Every path is one Scope names: CHANGELOG.md, README.md, REVIEW.md, docs/run-report.md, docs/settings.md, ninshubur/__main__.py, config.py, db.py, evaluate.py, runreport.py, settings.py, requirements-dev.in, scripts/fresh_clone.py, private_words.py, readme_sample.py, tests/private_words.sha256 and the eight `tests/test_*.py` files of the steps. `pyproject.toml`, `ninshubur/__init__.py`, `.github/`, `deploy/`, `stats.py`, `erase.py`, `PRIVACY.md`, `docs/USAGE.md` and `scripts/smoke.py` are untouched.
- **(f)** `git diff main...HEAD | grep -ci <the word>` → `0`; the same count over `git log main..HEAD --format=%B` → `0`. The word is not written in this file.
- **Dry merge** `git merge-tree --write-tree promises HEAD` → exit `0`, one line of output (a tree id, `adfb96f…`), no `CONFLICT` line, so no conflicted file. Informational: PR #2 is not merged, and this shows only that the text merges, not that the merged tree passes its tests.

## What changed
- `b80a9e7` Evaluate reads the database read-only, so scoring can never change it — `db.connect_readonly`; `ninshubur evaluate` never creates, migrates, chmods or writes the database; a database without `llm_runs` exits 2 with a message, not a traceback.
- `29d8ca6` `ninshubur export run-report` — one dated page of counts (Markdown or JSON), closed vocabularies only, no titles, hosts or names; `docs/run-report.md` says what each number means and what the page leaves out.
- `2779ef2` `scripts/private_words.py` — fails when a listed word reaches a tracked file, matched by hash (token, pair and triple), printing file and line, never the text.
- `5a1e90c` README: Known limits — 7 limits, each pointing at a path or `path:line` that a test checks.
- `54255d2` README first screen — general-crawler wording and a "What it looks like" block captured from the replay of `tests/samples`.
- `64c295a` CHANGELOG draft — one Unreleased section, 60 bullets (Added 28, Changed 10, Fixed 10, Security 12) from 122 non-merge commits.
- `86e7329` `scripts/fresh_clone.py` — rehearses each install path from a clone of the committed HEAD, and fixes `pip install -r requirements-dev.in` (it named a `requirements.txt` that does not exist).
- `81182cd` `ninshubur settings list` and `preview` — 24 learnable keys with bounds; guardrail keys shown, never set.
- `8e97f77` `ninshubur settings apply`, `undo`, `log` — writes `settings.yaml` (never `config.yaml`) and one audit line per change; `config.load_config` merges the overlay (learnable, valid entries only).
- `78db90a` the hand-off note; this file replaces it in the last commit.

Files per step: 2 `db.py`, `evaluate.py`, `tests/test_evaluate_readonly.py` · 3 `runreport.py`, `__main__.py` (parser lines), `docs/run-report.md`, `tests/test_run_report.py` · 4 `scripts/private_words.py`, `tests/private_words.sha256`, `tests/test_private_words.py` · 5 `README.md`, `tests/test_known_limits.py` · 6 `README.md`, `scripts/readme_sample.py`, `tests/test_readme_sample.py` · 7 `CHANGELOG.md`, `tests/test_changelog.py` · 8 `scripts/fresh_clone.py`, `requirements-dev.in`, `tests/test_fresh_clone.py` · 9–10 `ninshubur/settings.py`, `config.py` (the merge hook, 2 lines and a comment), `__main__.py` (parser lines), `docs/settings.md`, `tests/test_settings.py`, `CHANGELOG.md`.

## Checks run
- Step 1: import path inside the worktree; `973 passed`, `smoke ok`. PR #2 touches PRIVACY.md README.md config.example.yaml docs/USAGE.md ninshubur/__main__.py ninshubur/config.py ninshubur/db.py ninshubur/erase.py ninshubur/ingest.py ninshubur/schedule.py tests/test_erase.py tests/test_qualify.py. The worktree was cut from `origin/main` (4a8bdb2); I fast-forwarded it to local `main` (aedbc01) before any edit.
- Step 2: `tests/test_evaluate_readonly.py` `7 passed in 0.38s`. Plain `mode=ro` left `-wal`/`-shm` files behind in my experiment (3 of 7 tests failed with it), so `connect_readonly` uses `mode=ro` beside a live WAL file and `mode=ro&immutable=1` otherwise.
- Step 3: `tests/test_run_report.py` 15 passed. Live page `logs/run-report-live-2026-11-25.md`: heading `# Ninshubur run report, 2026-11-25`; window `first fetch 2026-10-13 05:06:48 UTC, last fetch 2026-11-25 12:55:03 UTC (everything stored), 2,245 fetches`. Leak check from a backup-API snapshot, counts only: literal search of 5,358 titles, hosts, source, subject and entity names found **8 hits, not 0**; each of the 8 equals a plain word or digit the page prints itself (template text or a closed vocabulary, e.g. an entity named with one digit). Searching the strings that are not such words: `0 hits out of 5349 strings`. The brief's literal `0 hits` was therefore not reached. Two values from earlier builds, adapter `explorer` and suggestion kind `conference_trip`, were outside my closed lists and were added. The scripts are in `/home/alexb/.claude/jobs/d77ad0a7/tmp/` (`leak_check.py`, `snapshot.py`, `vocab_gap.py`).
- Step 4: `tests/test_private_words.py` 24 passed with the variable, 23 passed and 1 skipped without. A plain `git grep` for the word finds 0 files. The word's hash lives only in the gitignored `logs/private-words.local.sha256`.
- Step 5: `tests/test_known_limits.py` 5 passed.
- Step 6: `tests/test_readme_sample.py` with the limits test, 11 passed. README hunks against `main`: `@@ -8 +8 @@` (headline), `@@ -25,0 +26,25 @@` (What it looks like), `@@ -93,0 +119,12 @@` (Known limits). None touches old lines 32, 87, 91 or 105 (PR #2's).
- Step 7: `tests/test_changelog.py` 5 passed. 122 non-merge commits read (112 on `main` at launch, plus this branch's); 60 bullets.
- Step 8: first real run found `install: FAIL [requirements-dev] pip install -r requirements-dev.in` (`Could not open requirements file: … requirements.txt`); fixed in `86e7329`. Second run from that commit: `install: PASS` ×3 (`pip install -e .`, `pip install -e ".[dev]"`, `pip install -r requirements-dev.in`), `pytest: PASS … 1044 passed`, `smoke: PASS`, `check: PASS`, `all passed in 234 s`. Text in `logs/fresh_clone_run2.txt`. **Not rehearsed**, because they fetch from the internet: `ninshubur sources test` and `ninshubur run`.
- Step 9: `tests/test_settings.py` 41 passed.
- Step 10: `tests/test_settings.py` 61 passed. One replay run: `base_value_usd.cfp_submit` 800 → 1600 moved the two cfp_submit suggestions' `est_value` from 303.2 to 606.4 and from 272.88 to 545.76 (exactly ×2: value is linear in the base), and `undo` brought them back. Hand run in a scratch home: apply without `--yes` exits 2; apply, a second apply on the same key, `undo 1` refused ("changed again since"), `undo`, `undo 1` all behaved as `docs/settings.md` says; `settings.yaml` and `logs/settings.jsonl` were mode 0600.

### Known limits → evidence (what sits at each cited line, read 25 Nov)
| Limit | Reference | At that line |
|---|---|---|
| The qualifier is not yet a filter | docs/case-study.md:3 | "Short answer: not yet as a filter." |
| One labeller, small samples | docs/case-study.md:204 | "One labeller. The author labelled every row…" |
| The explorer runs on Linux only | tests/test_linux_only.py:1; ninshubur/explorer/__init__.py:19 | the file's docstring; `def linux_only()` |
| robots.txt 401/403 is off limits unless reviewed | ninshubur/fetch.py:557; tests/test_fetch.py:215 | the exact-status condition; `test_robots_401_passes_only_with_the_exact_reviewed_status` |
| Only a source with an adapter can be read | ninshubur/adapters/base.py:147 | `def get_adapter(name)` |
| Pages are read as the server sends them | pyproject.toml:12 | `dependencies = [` (httpx, PyYAML) |
| Detectors match known patterns | ninshubur/detectors.py:85 | `_INJECTION = re.compile(` |

### CHANGELOG bullet → commits (hashes stay out of the file)
| # | Section | Bullet (start) | Commits |
|---|---|---|---|
| 1 | Added | Policy-first crawling of admitted sources… | 8349e25 |
| 2 | Added | Exports: export digest, suggestions and items… | 5c9b9b4 |
| 3 | Added | Hacker News "Who is hiring?" comments become hiring items | 5c9b9b4 |
| 4 | Added | An operator guide, docs/USAGE.md | a6f00d8 |
| 5 | Added | A provider_docs adapter… | 0705766 |
| 6 | Added | Event pages: the event_sites adapter… | b464417 145152d baaab79 33ed495 |
| 7 | Added | Ashby and Lever adapters… | f049772 518c208 |
| 8 | Added | ninshubur companies… | c4ff934 |
| 9 | Added | A source can record the exact 401 or 403… | de1f4b1 d97a32c |
| 10 | Added | ninshubur qualify… | b5d7211 f25551f |
| 11 | Added | A language-model client with a router slot… | 2d52855 |
| 12 | Added | E-mail addresses, links, handles and phone numbers… | 1b1ae81 |
| 13 | Added | ninshubur export label-sheet… | 0f0a4fe cc18983 89c7620 |
| 14 | Added | ninshubur qualify --from-sheet… | 3dce289 8ea5a60 |
| 15 | Added | ninshubur evaluate scores stored qualify answers… | dfee1fc 4438237 563c6e1 c63fd6e 0e2139f |
| 16 | Added | docs/case-study.md… | aedbc01 |
| 17 | Added | The explorer (Linux only), with its own database… | cdf7e14 ee81bda 6c8bef5 ce33b3f bbf752b 3646331 d3f5890 e1878e9 1f23696 |
| 18 | Added | ninshubur explore commands… | cdf7e14 977dd96 da030f2 1f23696 3b18d2f |
| 19 | Added | A one-way hand-off from the explorer to the crawler | 3b18d2f |
| 20 | Added | A kill switch for the explorer… | da030f2 faee42f a1a9293 |
| 21 | Added | A hardened systemd timer and a disk quota for the explorer | 68f12c8 |
| 22 | Added | Operator guide sections on running the explorer | 8d08383 0fad3a2 ec6d9d9 d3b8123 |
| 23 | Added | A README with a banner, badges and the three rules | f23d7ba |
| 24 | Added | ninshubur export run-report… | 29d8ca6 |
| 25 | Added | README: a "What it looks like" block… | 54255d2 5a1e90c |
| 26 | Added | scripts/private_words.py… | 2779ef2 |
| 27 | Added | scripts/fresh_clone.py… | 86e7329 |
| 28 | Added | ninshubur settings… | 81182cd 8e97f77 |
| 29 | Changed | Hiring suggestions come one per company and profile | 08f5445 |
| 30 | Changed | A hiring role counts as a signal on its own | dd2323e |
| 31 | Changed | A suggestion's tier badge shows the highest tier number… | 923f8a8 |
| 32 | Changed | ninshubur evaluate opens the database read-only | b80a9e7 |
| 33 | Changed | Relicensed to AGPL-3.0-or-later, with a naming clause | 953e08b |
| 34 | Changed | The project's wording is neutral and contact goes through the issue tracker… | 7487735 |
| 35 | Changed | Every explore command says the explorer is Linux only… | 1c5fbc6 0435b74 f1166ea |
| 36 | Changed | Explorer stop rules tightened after review… | 06666fe 530c266 e76ad03 6190fdd 9bf1cfb 217a7df |
| 37 | Changed | The example config disables the status-page source… | c3b7b27 |
| 38 | Changed | A failed connect in the fetch log names the socket's own error | 966f23e |
| 39 | Fixed | A follow-up page on the www twin of an event's host… | 923f8a8 33ed495 |
| 40 | Fixed | The daemon waits for the network after the machine wakes | d42b2bf |
| 41 | Fixed | Pausing a source keeps its quarantine… | 257289c |
| 42 | Fixed | A tainted item counts toward its source's taint events once | 1a3d379 |
| 43 | Fixed | PowerShell counts as a shell fragment only when… | f10e714 |
| 44 | Fixed | A redirect whose Location cannot be built into a request… | adc20d5 |
| 45 | Fixed | On Windows the CLI falls back to UTC with a warning… | 8e72e92 |
| 46 | Fixed | The admission probe no longer records fetch state | 0705766 |
| 47 | Fixed | Explorer edge cases… | f4d9b66 2f839c4 2c30c6b 9771721 3d2a20a ec70882 5d17d85 34486b8 6fda9db 42c4894 |
| 48 | Fixed | pip install -r requirements-dev.in works from a clean clone | 86e7329 |
| 49 | Security | The URL gate allows port 443 only… | f0562ad |
| 50 | Security | The fetcher resolves each host first… | b464417 931d0bb |
| 51 | Security | The fetcher connects to the address it checked | a1c4f44 |
| 52 | Security | Every request has one total deadline… | 24f4b85 cd7dab8 |
| 53 | Security | Gzip is decoded in small steps… | b507adf 0423a81 |
| 54 | Security | robots.txt fails closed… | d26698f 8b4acd8 ee8fd5b c7c0adf |
| 55 | Security | JSON from the network cannot crash a parse by nesting | c858298 302a818 |
| 56 | Security | Characters that hide text are flagged… | 9e77b8a 51f348d |
| 57 | Security | Regular expressions on crawled or model text run in linear time | 2a1946d ec6d9d9 |
| 58 | Security | Terminal control sequences in crawled text… | 5389052 d9e7db6 83dc37c |
| 59 | Security | The explorer's parser runs in a bubblewrap sandbox | 456e980 |
| 60 | Security | The crawl invariants are pinned by a test | c9c595f |

(Bullet 28's own commit, `8e97f77`, is the one that added it; the generator and the full bullet text are in the gitignored `logs/changelog_bullets.py` and `logs/gen_changelog.py`; running `python3 logs/gen_changelog.py` regenerates `CHANGELOG.md` and checks that every hash exists.)

### README, before and after (Question 6: the wording is yours)
**Before** (line 8): *A policy-first crawler for B2B signals.* Ninshubur gathers conferences, public service requests, hiring signals and provider status from human-admitted sources only, keeps only the fields each source declared, and turns them into timed, costed suggestions a person accepts or dismisses. Nothing is ever executed automatically.

**After** (line 8): *A policy-first crawler for public sources.* Ninshubur reads only the sources a person has admitted, keeps only the fields each source declared, and turns what it finds (conferences, public service requests, hiring postings, provider status) into timed, costed suggestions you accept or dismiss. What counts as a good find is set by a profile, a short YAML file of keywords, titles and companies: a job search is one profile, a client's list of target companies another. Nothing is ever executed automatically.

New under it, lines 26–50: "What it looks like", the output of `ninshubur status` and `ninshubur suggest --top 1` from the replay of `tests/samples` in a temporary home, with a note that it is not live data and the command that rebuilds it (`python scripts/readme_sample.py --update`). New before "Layout": "Known limits", 7 lines, table above.

## Not verified
- Only Linux was run. The macOS and Windows CI legs have not seen this branch; the settings file-mode test is skipped off POSIX, and I did not check the other new tests for POSIX-only assumptions.
- The merged tree with PR #2 was not tested: the text merges cleanly, but PR #2 moves the schema to 5, and I did not run `run-report`, `evaluate` or the suite on a merged tree. If `promises` adds a table (the brief mentions `forgotten`), `run-report` leaves that section out with a line saying why; that path is tested only with a synthetic missing table.
- `connect_readonly`'s immutable path can fail, or read a mix of states, if a writer opens the file in the instant between its check and the end of a read; it never writes.
- The live-run page's leak check did not reach the literal `0 hits` (step 3 above).
- `budgets.daily_human_minutes` only feeds `ninshubur budget` (`ninshubur/__main__.py:355`) and `corroboration_value_usd` only decides a hold (`ninshubur/analyze/suggest.py:180`): neither changes a suggestion's rank, though the brief lists both as learnable. `docs/settings.md` says so. Say if you want them out of the list.
- Settings, known rough edges: two `apply` commands started at the same instant could take the same id (no lock); with an unusable `settings.yaml`, `settings list` shows each warning twice (standard error from loading the config, standard output from the list); `undo` with no id means the last apply only, and says so if that one is already undone.
- The CHANGELOG wording rests on commit subjects and bodies, not on re-reading each change.
- The README sample's "next …" times come from the replay's clock on 25 Nov; they will differ on the next `--update`.
- The README quick-start lines `ninshubur sources test` and `ninshubur run` were not rehearsed (network).
- I did not send a desktop alert; that is the run queue's job.
- Nothing under `~/.ninshubur` was written, moved or deleted in either run; the live page came from a backup-API snapshot in a job temp folder.

## Questions for Alex
Answered: **Q1** (25 Nov): Ninshubur first priority, option (a), the run took its full list; the ship-week thread is told and takes this branch at review. Q2–Q7 were not answered; the run used each default. All of them are still open for you:
2. **PR #2 (`promises`)** — launched from `main`, README lines 32, 87, 91, 105 and the `forget` parser untouched, nothing said about `forget` or the sweep. Dry merge: clean (exit 0). Options: (a) merge PR #2 first, then take this branch, then add the `forget`/sweep lines to README, Known limits and CHANGELOG; (b) default: take this branch first, the thread adds those lines after PR #2.
3. **Real words** — default (a): no real word or hash of one is tracked; the one word's hash is in `logs/private-words.local.sha256` (gitignored), the check reads it through `NINSHUBUR_PRIVATE_WORDS_FILE`, the tracked `tests/private_words.sha256` holds comment lines and a synthetic test word's hash. **That file is inside the worktree: copy it out before the worktree is removed**, for example `mkdir -p ~/.config/ninshubur && cp logs/private-words.local.sha256 ~/.config/ninshubur/private-words.sha256`, and for other words (former employers, person rows) run `python scripts/private_words.py hash < words.txt >> ~/.config/ninshubur/private-words.sha256` on your machine. Option (b): a tracked hash file (CI enforces; anyone who guesses the word can confirm it; never for person names).
4. **Live-run page** — not committed; `logs/run-report-live-2026-11-25.md` in the worktree (gitignored) and `/home/alexb/.claude/jobs/d77ad0a7/tmp/run-report-live.md`. Options: tell me "commit it as `docs/live-run-<date>.md`", or it stays out of the repo and the README shows only the replay sample.
5. **Settings design** — defaults used: overlay file `settings.yaml` over `config.yaml`; 24 learnable keys (`base_value_usd.*`, `costs.*`, `budgets.daily_human_minutes`, `budgets.hourly_rate_usd`, `corroboration_value_usd`; the list is in `docs/settings.md`); everything that changes what is fetched, stored, sent or admitted is a guardrail key; `apply` needs `--yes`; a value takes effect at the next process start. Confirm or change the key list (see the two keys under Not verified).
6. **README first-screen wording** — drafted on the branch, shown above; it is yours to change. Proposed one-line GitHub repository description (text only, not set): *A policy-first crawler for public sources: it reads only the sources a person has admitted, keeps only the fields each declares, and turns what it finds into timed, costed suggestions you accept or dismiss. Nothing runs automatically.* (the current one says "B2B signals / sales-intelligence").
7. **The `[dev]` install fix** — not defined in the brief I could read; step 8 ran every documented install path and found one defect, `requirements-dev.in` starting with `-c requirements.txt` while no such file exists; fixed with `-r requirements.in`. Say if the plan meant something else.
8. **run-guard refusal (step 7, earlier run)** — a Write to `/home/alexb/.claude/jobs/d77ad0a7/tmp/changelog_bullets.py` was refused as "edit outside the run's folder" although earlier writes there had passed. Not retried; the scratch went to `logs/`. Options: (a) nothing; (b) check why the job temp folder stopped counting as the run's own.
9. **run-guard refusal (hand-off after step 9)** — a `python3 - <<'PYEOF'` heredoc that generated a long REVIEW.md and ran `scripts/private_words.py check` was refused as a push/upload/remote write; it did none of those. Not retried. This run wrote REVIEW.md with the Write tool and was not refused. Options: (a) nothing; (b) look at what the guard matched (my guess, unchecked: the release-day text or the `export …` line).

## Release day (prepared, not done)
1. Read this file and, if you want, the live page (Question 4). Take the branch: `git -C /home/alexb/ninshubur merge worktree-release-prep` (or cherry-pick); the ship-week thread does this before its own sessions 4 and 5. Nothing was pushed, tagged or merged by the run.
2. Version: `pyproject.toml:7` `version = "0.1.0"` and `ninshubur/__init__.py:10` `__version__ = "0.1.0"`, both to `0.2.0`.
3. CHANGELOG: rename `## Unreleased` to `## 0.2.0 - <date>` (and open a new empty `## Unreleased` above it if you want one). **`tests/test_changelog.py` will fail on that rename** — it is a guard for the draft (exactly one `## Unreleased`, no date or version under it, no `## …` heading with a digit). Change the test in the same commit.
4. After the commits: `python scripts/fresh_clone.py` (about 4 minutes, needs the network for pip; `--dry-run` prints the plan). It clones the committed HEAD, so commit first.
5. Your tag and push; the repository description from Question 6.
6. The real-words file (Question 3): copy `logs/private-words.local.sha256` out of the worktree and `export NINSHUBUR_PRIVATE_WORDS_FILE=<that path>` where you run the check, for example before a push: `python scripts/private_words.py check`.
7. After PR #2 merges: add `forget` and the sweep to README, Known limits and CHANGELOG; re-run `python scripts/readme_sample.py --update` if the sample commands change.

## How to look
`git -C /home/alexb/ninshubur diff main...worktree-release-prep --stat` · `git -C /home/alexb/ninshubur log main..worktree-release-prep --oneline`
