# Review: Ninshubur release prep

**Result:** context hand-off · stopped after step 9 (settings `list` and `preview`) because the session passed 150k tokens; steps 10 and 11 remain · branch `worktree-release-prep` (worktree `/home/alexb/ninshubur/.claude/worktrees/release-prep`) · 8 commits of work, then this note

This is the short hand-off note. The brief's Status line (`/home/alexb/.claude/plans/Ninshubur release prep (11.25.26).md`) says exactly what step 10 is and what step 11 must still do; the full REVIEW (Done when (a)–(f), trace tables, README before and after, release-day list, proposed repository description) is step 11's job, because run-guard refused the command that generated it (Question 9).

## Done when (interim, on the branch as it stands)
- (a) `NINSHUBUR_PRIVATE_WORDS_FILE=logs/private-words.local.sha256 python -m pytest -q` → `1086 passed in 172.80s` (main at launch: `973 passed`).
- (b) `python scripts/smoke.py` → last line `smoke ok`.
- (c) `NINSHUBUR_PRIVATE_WORDS_FILE=logs/private-words.local.sha256 python scripts/private_words.py check` → `private-words check: 185 files, 350245 tokens, 2 hashes, 0 hits`, exit 0 (run before this note was added).
- (e) `git diff main...HEAD --stat` ended `22 files changed, 2972 insertions(+), 13 deletions(-)` before this note; every file is one Scope names.
- (f) the diff held the private word 0 times (`grep -ci` printed `0`).
- (d) not yet run as a set.

## What changed
- `b80a9e7` Evaluate reads the database read-only (`db.connect_readonly`; `evaluate` never writes, creates or migrates it).
- `29d8ca6` `ninshubur export run-report`: one dated page of counts, no names on it.
- `2779ef2` `scripts/private_words.py`: fails when a listed word reaches a tracked file, by hash.
- `5a1e90c` README: Known limits section (7 limits, each with its evidence).
- `54255d2` README first screen: general-crawler wording and a real replay sample.
- `64c295a` CHANGELOG draft (Unreleased only).
- `86e7329` `scripts/fresh_clone.py`, and the fix for `pip install -r requirements-dev.in`.
- `81182cd` `ninshubur settings list` and `preview`, 24 learnable keys.

## Checks run
- Step 1: `973 passed`, `smoke ok`; the worktree was cut from `origin/main` (4a8bdb2), so I fast-forwarded the empty branch to local `main` (aedbc01) before any edit. PR #2 touches: PRIVACY.md README.md config.example.yaml docs/USAGE.md ninshubur/__main__.py ninshubur/config.py ninshubur/db.py ninshubur/erase.py ninshubur/ingest.py ninshubur/schedule.py tests/test_erase.py tests/test_qualify.py.
- Step 2: 7 passed. Plain `mode=ro` left `-wal` and `-shm` behind, so `connect_readonly` uses `mode=ro` beside a live WAL file and `mode=ro&immutable=1` when there is none.
- Step 3: 15 passed. Live page `logs/run-report-live-2026-11-25.md` (gitignored, not committed). Heading `# Ninshubur run report, 2026-11-25`; window `first fetch 2026-10-13 05:06:48 UTC, last fetch 2026-11-25 12:55:03 UTC (everything stored), 2,245 fetches`. Leak check (counts only): literal search of 5,358 names and titles found 8 hits, each a plain word or digit the page prints itself; searching the strings that are not such words: `0 hits out of 5349 strings`. The brief's literal `0 hits` was therefore not reached; method and scripts are in the job temp folder.
- Step 4: 24 passed with the local words list set (23 passed and 1 skipped without it, the skip says why). The real word's hash is only in the gitignored `logs/private-words.local.sha256`.
- Steps 5, 6, 7: 5, 6 and 5 tests passed; 7 limits; 59 CHANGELOG bullets traced to commits in `logs/changelog_bullets.py` (gitignored scratch in the worktree; `python3 logs/gen_changelog.py` regenerates `CHANGELOG.md` from it and checks every hash exists).
- Step 8: first real run found `install: FAIL [requirements-dev] pip install -r requirements-dev.in` (`requirements.txt` named but absent); fixed in `86e7329`. Second real run from that commit: `install: PASS` for README, CONTRIBUTING and requirements-dev, `pytest: PASS 1044 passed`, `smoke: PASS`, `check: PASS`, `all passed in 234 s` (full text in `logs/fresh_clone_run2.txt`). Not rehearsed (they fetch from the internet): `ninshubur sources test`, `ninshubur run`.
- Step 9: 41 passed.
- `cockpit step` answered `unknown command`, so the step markers were skipped.

## Not verified
- Only Linux was run; the macOS and Windows CI legs have not seen this branch.
- `connect_readonly`'s immutable path can fail or read a mix of states if a writer opens the file in the instant between its check and the end of a read; it never writes.
- The CHANGELOG wording rests on commit subjects and bodies.
- `budgets.daily_human_minutes` only feeds `ninshubur budget` (`ninshubur/__main__.py:355`) and `corroboration_value_usd` only decides a hold (`ninshubur/analyze/suggest.py:180`): neither changes a suggestion's rank, though the brief lists both as learnable. `docs/settings.md` says so.
- PR #2's dry merge (step 11) has not been run.

## Questions for Alex
Copied from the brief (Questions 0–9 there carry the options): 1 overlap with the ship-week thread (assumed (a)); 2 PR #2 (launched from `main`, nothing about `forget`); 3 real words (assumed (a): hash of the one word in `logs/private-words.local.sha256`, outside the tracked tree); 4 whether the live-run page is committed (not committed); 5 settings design (overlay file, 24 learnable keys, `--yes`, effect at next start); 6 README wording is yours; 7 the `[dev]` install fix (found and fixed one defect); 8 run-guard refused a Write to the job temp folder; 9 run-guard refused the command that generated the full REVIEW.

## Release day
Not prepared in this note: step 11 writes it.

## How to look
`git -C /home/alexb/ninshubur diff main...worktree-release-prep --stat` · `git -C /home/alexb/ninshubur log main..worktree-release-prep --oneline`
