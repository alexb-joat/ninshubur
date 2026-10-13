# Ninshubur (𒀭𒎏𒋚)

A policy-first intelligence crawler for a B2B lead-generation business. It gathers conferences,
public service requests, hiring signals and provider status from **human-admitted sources only**,
extracts **only declared fields**, and turns them into timed, costed suggestions a human accepts
or dismisses. Nothing is ever executed automatically.

Three rules shape every module:

1. **Source trust policy.** A source is fetched only after a named reviewer admitted it through
   the H1-H8 checklist (`ninshubur sources test NAME`). Tiers 0-3 set starting reputation,
   review cadence and evidence weight. Links found in content become *candidates* for a human,
   never fetch targets.
2. **Declared extraction.** Every adapter ships a schema; `project()` keeps only those fields,
   sanitises them and drops the rest. Raw bodies never touch disk.
3. **Fail closed, degrade gracefully.** Transport, structure and content detectors quarantine
   bad items (hashes only), downgrade the source (`suspect` -> `quarantined`), hold affected
   suggestions and keep serving from the last known-good snapshot.

## Status

Phase 1 (policy core, rules-only analysis): `confs_tech`, `greenhouse`, `hn`, `statuspage`
adapters; builders `cfp_submit`, `conference_trip`, `service_request_lead`,
`hiring_signal_outreach`, `tool_outage`; ICS calendar sink; CLI. No LLM calls yet.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
mkdir -p ~/.ninshubur
cp config.example.yaml ~/.ninshubur/config.yaml
cp profiles.example.yaml ~/.ninshubur/profiles.yaml
ninshubur check                     # policy, config, schemas, review dates
ninshubur sources test confs-2026   # admission checklist H1-H8 against the live source
ninshubur run                       # fetch due sources, rebuild suggestions
ninshubur suggest --plan 45m        # best set for 45 minutes of human time
ninshubur show 3 && ninshubur accept 3
ninshubur status                    # health, held, quarantined, candidates
```

Offline: `ninshubur run --replay tests/samples` runs the real pipeline on the shipped samples.
`python scripts/smoke.py` does that end to end in a temporary home; `pytest -q` runs the suite,
including the malicious-input corpus in `tests/test_malicious.py`.

## Layout

```
ninshubur/policy.yaml   shipped floor; ~/.ninshubur/policy.yaml may tighten, never loosen
ninshubur/policy.py     allow_fetch, admission_check, allow_store, allow_llm
ninshubur/schemas.py    SCHEMAS registry + project()
ninshubur/detectors.py  transport / structure / content / behaviour detectors
ninshubur/fetch.py      the ONLY network gate (robots, pacing, caps, redirects, conditional GET)
ninshubur/ingest.py     quarantine, reputation, health, snapshots, items
ninshubur/schedule.py   due sources, run_source, growth/backoff
ninshubur/analyze/      relevance, timing, value, suggestion builders
ninshubur/sinks/        ics (trello, google_calendar, platform in later phases)
```

Data lives in `~/.ninshubur` (`NINSHUBUR_HOME`): `ninshubur.db`, `logs/{fetch,policy}.jsonl`,
`feeds/timings.ics`. All files are 0600 on POSIX.

## What it will not do

No login, CAPTCHA or paywall bypass, no proxies or fingerprint evasion, no LinkedIn / Facebook /
X / Reddit HTML, no fetching of URLs found in content, no person data outside the B2B
professional whitelist, no cloud model for anything containing person data. These are tested
invariants, not configuration.

License: MIT.
