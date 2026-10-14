<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/banner-dark.svg">
  <img alt="Ninshubur: trusted sources in, declared fields out, timed suggestions" src="docs/img/banner-light.svg" width="720">
</picture>

**A policy-first crawler for B2B signals.** Ninshubur gathers conferences, public service requests, hiring signals and provider status from human-admitted sources only, keeps only the fields each source declared, and turns them into timed, costed suggestions a person accepts or dismisses. Nothing is ever executed automatically.

[![ci](https://github.com/alexb-joat/ninshubur/actions/workflows/ci.yml/badge.svg)](https://github.com/alexb-joat/ninshubur/actions/workflows/ci.yml)
[![codeql](https://github.com/alexb-joat/ninshubur/actions/workflows/codeql.yml/badge.svg)](https://github.com/alexb-joat/ninshubur/actions/workflows/codeql.yml)
![python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-555)
![dependencies](https://img.shields.io/badge/runtime%20dependencies-2-5fd6ae)
![network](https://img.shields.io/badge/fetches-admitted%20sources%20only-5fd6ae)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Quick start](#quick-start) &nbsp;·&nbsp; [How a team uses it](#how-a-team-uses-it) &nbsp;·&nbsp; [Layout](#layout) &nbsp;·&nbsp; [What it will not do](#what-it-will-not-do) &nbsp;·&nbsp; [Operator guide](docs/USAGE.md)

<sub><i>Ninshubur (𒀭𒎏𒋚): the Sumerian messenger who carried word between realms and did exactly what was asked, nothing more.<br>Here it carries what reviewed sources say, and acts on nothing by itself.</i></sub>

</div>

<br>

## Three rules

| Source trust policy | Declared extraction | Fail closed, degrade gracefully |
|---|---|---|
| A source is fetched only after a named reviewer admitted it through the H1–H8 checklist (`ninshubur sources test NAME`). Tiers 0–3 set starting reputation, review cadence and evidence weight. Links found in content become *candidates* for a human, never fetch targets. | Every adapter ships a schema; `project()` keeps only those fields, sanitises them and drops the rest. Raw bodies never touch disk. | Transport, structure and content detectors quarantine bad items (hashes only), downgrade the source (`suspect`, then `quarantined`), hold the affected suggestions and keep serving from the last known-good snapshot. |

These are tested invariants: 272 tests, including a malicious-input corpus, run on Linux, macOS and Windows with Python 3.11, 3.13 and 3.14. Two runtime dependencies (httpx, PyYAML); no LLM calls in this phase.

## Status

Phase 1 (policy core, rules-only analysis): `confs_tech`, `greenhouse`, `hn`, `statuspage`
adapters, plus `provider_docs` (LLM providers' model documentation, exported with `ninshubur export euboulia-facts` for the Euboulia extension); builders `cfp_submit`, `conference_trip`, `service_request_lead`,
`hiring_signal_outreach`, `tool_outage`; ICS calendar sink; Markdown/HTML/CSV exports; CLI. No LLM calls yet.

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

## How a team uses it

The full operator guide is [docs/USAGE.md](docs/USAGE.md). In short:

1. **One profile per client** in `~/.ninshubur/profiles.yaml` (start from `client-template` in
   `profiles.example.yaml`): cities and countries, titles, keywords and pains, target companies,
   providers in use, home base and home country. `ninshubur profiles check` says which suggestion
   kinds each profile can feed and what is missing.
2. **Admit the client's sources.** Target companies with a Greenhouse board:
   `ninshubur sources add acme-jobs --adapter greenhouse --param 'boards=[acme]' --tier 0 ...`
   runs the H1-H8 checklist and refuses anything that fails it. `sources candidates` lists hosts
   seen in content; they are never fetched until a human admits them (`--reject` clears noise).
3. **Run.** `ninshubur run` once, or `deploy/linux/install.sh` for the daemon. Each run fetches
   what is due, applies the policy, rebuilds suggestions and the calendar feed.
4. **Review.** `ninshubur suggest --profile <client>` ranks by return on human time;
   `suggest --held` shows what the policy is holding and why (tainted evidence, suspect source,
   needs corroboration). `show ID` prints steps, reasons with the full value/cost formula, and
   evidence with the exact extracted fields and fetch time. `accept`, `dismiss --reason`, `done
   --outcome` record the decision; accepted items land in `feeds/timings.ics`.
5. **Export for the weekly review.** `ninshubur export digest --profile <client> --format md`
   (or `html`, then Print to PDF in a browser; or `csv`) writes to `feeds/`. `export items --format csv`
   dumps everything gathered. The daemon writes one Markdown digest per profile each ISO week.
6. **Tune.** Costs and base values are in `config.yaml` (`costs`, `base_value_usd`); the formula
   in every suggestion shows which number to change. Accept/dismiss/done feed the calibration
   counters that later phases use.

Suggestion kinds today: `conference_prospecting` (the companies and speakers listed on an upcoming or recently held event's site; see docs/USAGE.md),
`cfp_submit` (call for papers closing in 3-90 days), `hiring_signal_outreach` (a target company or
a matching role is hiring: Greenhouse boards and HN "Who is hiring?" posts), `service_request_lead`
(a public "seeking freelancer" style request matching the profile), `tool_outage` (a major incident
at a provider in `tools_in_use`). Nothing leaves the machine: no push, no telemetry, no model calls
in this phase.

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
