# Using Ninshubur: a guide for the LeadGet team

Ninshubur watches a short list of approved sources (conference calendars, job boards, public
"who is hiring" threads, provider status pages), keeps only the facts we declared we want, and
turns them into timed suggestions: "this company is hiring sales roles, write to them this week",
"this conference in a client city starts in 40 days, decide by Friday". A person accepts,
dismisses or completes each suggestion. Nothing is sent anywhere automatically.

Everything lives on the machine that runs it, in one folder: `~/.ninshubur/`.

## 1. Install (once per machine)

```bash
git clone git@github.com:alexb-joat/ninshubur.git ~/ninshubur
cd ~/ninshubur
python3 -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -e .
mkdir -p ~/.ninshubur
cp config.example.yaml  ~/.ninshubur/config.yaml
cp profiles.example.yaml ~/.ninshubur/profiles.yaml
ninshubur check
```

`check` must end with `ok`. If it says "refusing to run", a config file is malformed; the
message names the line.

Python 3.11 or newer is required. On Windows the time zone package is installed automatically.

## 2. First run

```bash
ninshubur sources test confs-2026        # the admission checklist against the live source
ninshubur run                            # fetch what is due, build suggestions
ninshubur status                         # source health, counts
ninshubur suggest                        # the ranked queue
```

`run` fetches a few hundred kilobytes and takes seconds. A second `run` right away says
"nothing due": each source has its own cadence (conference data daily, Hacker News every 2 to 12
hours). `run --force` fetches anyway.

## 3. Reading a suggestion

```
#19 [hiring_signal_outreach] Sundream Studio is hiring: growth, partnerships  (T3 unverified)
    value 34 USD  cost 15 USD  roi 1.3  confidence 0.26  profile leadget-saas-eu
    due 2026-11-01 (best: 2026-10-04 to 2026-10-23, Europe/Belgrade)  expires 2026-11-01
    - hiring for growth, partnerships: roles that imply an outbound need
    - value = 250 base x 1.00 weight x 0.50 relevance x 0.26 confidence x ...
```

- **kind** in brackets. Today: `conference_prospecting`, `cfp_submit`, `hiring_signal_outreach`,
  `service_request_lead`, `tool_outage`.
- **T0 to T3** is the trust tier of the evidence. T0 is first party or licensed, T1 curated
  open data, T2 an aggregator, T3 user-generated content. `unverified` means T2 or T3 from a
  single source: treat it as a lead to check, not a fact.
- **value / cost / roi**: expected value in USD, your time (and travel) in USD, and the ratio
  the queue is sorted by. The formula line shows every factor, so you can see which setting to
  change when a number looks wrong.
- **due / best window**: when it stops being worth doing, and the window in which doing it
  pays most. Dates are in your configured time zone.

`ninshubur show 19` prints the steps, every reason, and the evidence: the URL, the source, the
tier, when it was fetched and the exact fields that were extracted. Always open the evidence
link before acting on a T2 or T3 suggestion.

## 4. The daily loop

```bash
ninshubur run                                # or let the daemon do it, see section 8
ninshubur suggest --plan 45m                 # the best set for 45 minutes of your time
ninshubur show 19
ninshubur accept 19                          # goes into the calendar feed
ninshubur dismiss 7 --reason "not our market"
ninshubur done 19 --outcome reply --minutes 10
```

Accepted suggestions appear in `~/.ninshubur/feeds/timings.ics`; subscribe to that file from any
calendar app. `dismiss` and `done` are not bookkeeping only: they feed the counters that later
calibrate values per kind, so record them honestly.

### Held suggestions

```bash
ninshubur suggest --held
```

A suggestion is held, and never exported to a calendar, when its evidence was tainted (the text
contained something that looked like an injected instruction, a script, a key), its source is
under suspicion (one bad item puts a source in `suspect`; new items from it are held until it
behaves), or a single T2/T3 source claims something worth more than the corroboration threshold.
`ninshubur sources review <name>` shows why a source is suspect or quarantined, with hashes, not
the content; `--clear` restores it after a human looked.

## 5. The weekly review and exports

```bash
ninshubur export digest --profile <client> --format md      # Markdown
ninshubur export digest --profile <client> --format html    # open in a browser, Print to PDF
ninshubur export suggestions --format csv                   # one row per suggestion
ninshubur export items --format csv                         # everything gathered
```

Files land in `~/.ninshubur/feeds/`, named by ISO week and profile. The digest has a section per
kind sorted by value, a "Held" section, the sources table and the candidate hosts. It is written
for us: it shows formulas, tiers and held reasons. Do not forward it to a client as is.

The daemon writes one Markdown digest per profile automatically when the ISO week changes.

## 6. Onboarding a client

One profile per client, in `~/.ninshubur/profiles.yaml`. Copy the `client-template` block from
`profiles.example.yaml`, rename it to the client's slug and fill in:

| Field | Feeds | Notes |
|---|---|---|
| `geos.cities`, `geos.countries` | trips, local CFPs | cities first; countries are the fallback |
| `titles` | hiring matches | the roles the client sells to |
| `keywords`, `pains` | relevance of public posts | whole-word matches; keep them specific |
| `topics` | conference categories | confs.tech topics: general, javascript, devops, data, python ... |
| `target_companies` | hiring signals | `name` matches "Who is hiring?" posts; a `greenhouse` slug lets you admit their job board |
| `tools_in_use` | outage alerts | providers the client depends on |
| `home_base`, `home_country` | reserved for local-meeting suggestions (later phase) | where the person working this client sits |
| `weights.<kind>` | value multiplier | 1.2 if this client values hiring signals more, and so on |

Then:

```bash
ninshubur profiles check              # which suggestion kinds this profile can feed, and what is missing
ninshubur sources add acme-jobs --adapter greenhouse --param 'boards=[acme]' --tier 0 \
    --owner "Greenhouse Job Board API" --owner-url https://developers.greenhouse.io/job-board.html \
    --terms-url https://developers.greenhouse.io/job-board.html --licence "public API" \
    --rate-limit "ETag; <= 4/day" --reviewed-by <your name>
ninshubur run --force
ninshubur suggest --profile acme
```

`sources add` runs the eight admission checks against the live source and refuses to write the
config if any fails. That is deliberate: a source nobody vouched for is never fetched.

A distributed team: profiles are per client *and* per person who works them. If two people in
different countries cover the same client, make two profiles with different home bases.

## 6b. Conference prospecting

Conferences are prospect lists, not trips. The companies that paid for a stand, a sponsorship or a
talk are the target; speakers are secondary. Ninshubur crawls each event's own website for those
lists, under the `event-sites` source (see `config.example.yaml`).

**What is crawled.** For every event in the conference dataset that is upcoming or was held in the
last year: the event homepage and up to four pages on the *same host* whose link path or text says
speaker, sponsor, exhibitor, partner, lineup or agenda. At most 20 events a day by default
(`events_per_day`), one request per second per host, robots.txt honoured. Upcoming events are
re-crawled weekly, held events once. Pages on another host (a separate speakers site, a ticketing
platform) are never fetched; they appear in `sources candidates` for a human.

**What is kept.** Per person: name, title, company, talk, role, and a public LinkedIn profile URL if
the page shows one (stored as text, never visited: LinkedIn is a denied host). Per organisation:
name, website domain (never visited), role (sponsor, exhibitor, partner, organizer). Emails, phone
numbers, photos, bios and addresses on a page are counted and discarded. Person rows keep the
12-month retention and `forget` rules.

**What a hostile page can and cannot do.** Scripts, frames and styles never run: the page is parsed
into a bounded tree and those elements are discarded. Every extracted text is taint-scanned; a
speaker "bio" carrying an injected instruction drops that row (a quarantine row records only
hashes) and holds the page while clean rows stay. A wall of thousands of planted cards, a page
that is not HTML, a redirect to another host, or a host that resolves to a private address flags
that one event and never reaches the others. Everything extracted is marked **unverified**: a
planted fake speaker can at worst produce a suggestion that says "verify before contact".

```bash
ninshubur suggest --kind conference_prospecting          # one suggestion per crawled event
ninshubur show 12                                         # ... with the prospect table
ninshubur export prospects --format csv --event 2026-saastock   # or --profile <client>, --since 30d
ninshubur lookup "SaaStock" --dry-run                     # crawl one event now, store nothing, print what was found
ninshubur lookup "#12"                                    # crawl the event behind suggestion 12 now (outside the daily cap)
ninshubur sources review event-sites                      # per-event flags; --clear-event <uid> resets one
```

Two phases per event: **upcoming** (window from four weeks to one week before the start: pre-event
outreach, meetings during the event week) and **held** (up to six months after: "what did the event
deliver?" outreach to the companies that paid for presence). Value scales with how many companies
bought presence and how many match the client profile; cost is research minutes only.

## 7. Tuning before real work starts

All numbers live in `~/.ninshubur/config.yaml`. After an edit: `ninshubur check`, then
`ninshubur run` (no refetch needed; suggestions are rebuilt from stored items).

| Setting | What it changes | Default |
|---|---|---|
| `timezone` | all displayed dates | Europe/Belgrade |
| `costs.minutes.<kind>` | human minutes charged per kind | prospecting 20 (+3 per matched prospect), cfp 120, hiring 15, request 10 |
| `budgets.hourly_rate_usd` | what a minute of your time is worth | 60 |
| `base_value_usd.<kind>` | expected value before scaling | prospecting 600, cfp 800, request 400, hiring 250, outage 50 |
| `corroboration_value_usd` | above this, single T2/T3 evidence is held | 300 |
| `budgets.daily_bytes` | download cap per day | 200 MB |
| `scheduler.tick_s`, `scheduler.per_host_rps` | daemon cadence, politeness | 30 s, 1 request/s |
| `sources.<name>.params` | what a source looks for (e.g. HN `show_hn_queries`) | per source |
| `profiles.<name>.thresholds.relevance` (profiles.yaml) | how strict matching is | 0.55 |

Start with the two that decide the ranking: `base_value_usd.conference_prospecting` and
`base_value_usd.hiring_signal_outreach` (what one has historically been worth in closed business). The formula line in every suggestion shows the
effect immediately.

## 8. Running it unattended

```bash
deploy/linux/install.sh          # systemd user service, hardened; --remove to undo
deploy/macos/install.sh          # launchd agent
deploy\windows\install.ps1       # Scheduled Task at logon
journalctl --user -u ninshubur -f
```

The daemon runs `run` every tick, rebuilds suggestions, refreshes the calendar feed and writes
the weekly digests. It opens no network port.

## 9. Moving it to a shared machine

The folder `~/.ninshubur/` is the entire state. To move: install on the new machine (section 1),
copy the folder, run `ninshubur check`, install the service. Keep the folder private (the
installer sets 0700). Once licensed person data is enabled (Apollo, HubSpot; a later phase),
treat that machine like the CRM: disk encryption, no shared logins, backups handled as CRM
exports are.

## 10. What it costs

- **Money:** nothing by default. The cloud model, Apollo credits and other paid parts are off,
  each behind its own switch and daily cap, and every spend is written to `ninshubur budget`.
- **Machine:** a run takes well under a second and about 40 MB of memory; the database grows by
  a few MB per month. Bandwidth is a few MB per day.
- **Your time:** the real cost. A first pass over a day's queue is 15 to 20 minutes;
  `suggest --plan` fits it to the time you have. Each source needs a few minutes of re-review every
  90 to 180 days, and fetching stops automatically when a review lapses.

## 11. When something looks wrong

| Symptom | Where to look |
|---|---|
| "refusing to run" | the message names the config or policy line |
| a source shows `suspect` or `quarantined` | `ninshubur sources review <name>`; `--clear` after reading the reasons |
| a source shows `review_overdue` | update `reviewed_at` in its trust block after re-checking its terms |
| a source shows `broken` | it answered 403/404/410 five times; the URL or the service changed |
| nothing fetched | `ninshubur run --force`; check `~/.ninshubur/logs/fetch.jsonl` |
| something was refused | `~/.ninshubur/logs/policy.jsonl` has the reason and the rule |
| the queue is empty | `ninshubur profiles check`; the profile may lack the fields a kind needs |

`~/.ninshubur/logs/fetch.jsonl` is every request ever made: host, status, bytes, duration. If a
host is not in an adapter's allowlist it never appears there, because it was never contacted.

## What it will not do

Log into anything, solve CAPTCHAs, bypass paywalls, use proxies, scrape LinkedIn, Facebook, X
or Reddit, follow links found inside content, store a raw page, send person data to a cloud
model, or push anything anywhere without a human accepting it first. These are tested
invariants, not settings.
