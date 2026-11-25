# Using Ninshubur: the operator guide

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
    value 34 USD  cost 15 USD  roi 1.3  confidence 0.26  profile agency-saas-eu
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

In every CSV export (suggestions, items, prospects), a cell that begins with `=`, `+`, `-` or `@` (or a tab or carriage return) is prefixed with an apostrophe, so crawled text does not run as a spreadsheet formula. Every data cell is quoted, so a `;` or a tab inside a value cannot start a new cell.

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
| `target_companies` | hiring signals | `name` matches "Who is hiring?" posts; a `greenhouse`, `ashby` or `lever` slug lets you admit their job board |
| `tools_in_use` | outage alerts | providers the client depends on |
| `home_base`, `home_country` | reserved for local-meeting suggestions (later phase) | where the person working this client sits |
| `weights.<kind>` | value multiplier | 1.2 if this client values hiring signals more, and so on |
| `qualify` | `ninshubur qualify`, the label sheet | `question`, `must`, `must_not`, `labels` (the first is the positive answer; default `yes`, `no`, `unsure`), `subject` (default `company`); see section 6c |

A key the code does not know (a misspelling, a retired block) is ignored by the crawler; `ninshubur profiles check`
names it. Inside `qualify:` an unknown key is refused, because a rule that silently dropped out would change every answer.

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

An Ashby board (`jobs.ashbyhq.com/<board>`) is added the same way with `--adapter ashby`, plus one more flag.
Ashby's API host answers 401 on `robots.txt`, which Ninshubur reads as "the whole host is off limits" unless you
record that status after reading Ashby's documentation and terms:

```bash
ninshubur sources add acme-ashby --adapter ashby --param 'boards=[acme]' --tier 0 \
    --owner "Ashby public job posting API" --owner-url https://developers.ashbyhq.com/docs/public-job-posting-api \
    --terms-url https://www.ashbyhq.com/resources/terms --licence "public API" \
    --rate-limit "ETag; <= 4/day" --reviewed-by <your name> --robots-reviewed-status 401
```

Only 401 or 403 can be recorded, only that exact answer is let through, and only for that source. The exception
is written to `logs/policy.jsonl` (`robots_reviewed_exception`) and named in H2 of `sources test`.

A Lever site (`jobs.lever.co/<site>`) needs no extra flag: `api.lever.co` serves a robots.txt that allows every
path and asks for one second between requests, which the fetcher keeps.

```bash
ninshubur sources add acme-lever --adapter lever --param 'boards=[acme]' --tier 0 \
    --owner "Lever postings API" --owner-url https://github.com/lever/postings-api \
    --terms-url https://www.lever.co/legal/terms-of-service --licence "public API" \
    --rate-limit "Crawl-delay 1; ETag; <= 4/day per page" --reviewed-by <your name>
```

Lever has no field filter, so every answer carries each posting's full description (about 19 KB a posting).
Ninshubur drops it at projection, and reads each site in pages of 200 so no answer comes near the fetcher's
8 MB cap; a larger answer would quarantine the whole source. Two pages (400 postings) are read by default.
For a bigger site set `--param max_postings=1000` (at most 2000); a run whose last page comes back full says
so in its notes. Sites on Lever's EU instance (`jobs.eu.lever.co`) are not read yet.

`sources add` runs the eight admission checks against the live source and refuses to write the
config if any fails. That is deliberate: a source nobody vouched for is never fetched.

A distributed team: profiles are per client *and* per person who works them. If two people in
different countries cover the same client, make two profiles with different home bases.

## 6a. Companies

Every hiring item (a Greenhouse posting, a "Who is hiring?" comment) gets one company key. The
first rule that applies decides it:

1. a domain the item itself carries: a company written as a domain (`Nordash.io`) or followed by
   one in brackets (`Chronoleaf (chronoleaf.pe)`);
2. a domain from `config.yaml` (`params.companies.<board>.domain` of a greenhouse source) or from a
   profile's `target_companies`, matched by board slug or by the exact name;
3. your aliases file, `~/.ninshubur/company_aliases.yaml`;
4. the name itself, compared after lowercasing, dropping punctuation, a trailing bracket such as
   `(YC S11)` and a trailing unambiguous legal form (`Inc`, `Ltd`, `GmbH`, `S.A.`, ...; a bare `AG`
   or `Co` stays, and `--dupes` lists the pair).

Domains on hosts that give every customer a subdomain (`acme.github.io`) keep the full host; job
boards and social sites (`jobs.lever.co`, `linkedin.com`) never count as a company domain. A domain
or name read from a post only groups it with a company: trust, confidence and the display name come
from your config, profiles and aliases file.

The key is `company:<domain>` when a domain is known, else `company:<name>`. Similar names are never
merged by guesswork: `companies --dupes` lists them with the exact line that would merge each pair.

```
$ ninshubur companies
KEY                    NAME                         POSTINGS HELD  FIRST      LAST        SOURCES
company:acme.example   Acme Inc                            3    0  2026-09-20 2026-10-01  acme-jobs
company:acme-robotics  Acme Robotics                       1    0  2026-10-11 2026-10-11  hn
company:chronoleaf.pe  Chronoleaf                          1    0  2026-10-11 2026-10-11  hn
company:nordash        Nordash                             1    0  2026-10-11 2026-10-11  hn
company:nordash.io     Nordash.io                          1    0  2026-10-11 2026-10-11  hn
5 companies, 7 postings; `ninshubur companies --dupes` lists near-miss names

$ ninshubur companies --dupes
2 near-miss pair(s) among 5 companies; nothing is merged until you say so
  Nordash (company:nordash, 1)  ~  Nordash.io (company:nordash.io, 1)  [same name]
    add to company_aliases.yaml if they are one company:  Nordash: nordash.io
  Acme Robotics (company:acme-robotics, 1)  ~  Acme Inc (company:acme.example, 3)  ['robotics' added]
    add to company_aliases.yaml if they are one company:  Acme Robotics: acme.example
aliases file: /home/you/.ninshubur/company_aliases.yaml
```

The aliases file maps a name to a company name or a domain, one line each; a value with a dot
and no spaces is a domain. Paste only the lines you agree with:

```yaml
Nordash: nordash.io
Northwnd Labs: Northwind Labs
```

A malformed file never stops crawling: the last good aliases stay in use, the error (file, line,
fix) is printed and logged once, `check` fails and `companies` shows it above the table. Tainted items never
name a company; held items are counted in `HELD`. Both commands take `--json` and write nothing.
Each `run` also refreshes the `signals` table (one row per clean hiring posting, by company key;
postings published in the same second share a row)
and `company_aliases` (the decisions above) for anyone reading the database directly.

Hiring suggestions follow the same keys: one per company and profile, covering every posting of
the last 30 days (`<Name> is hiring: <n> roles (<most relevant title>)`, the five newest postings as
evidence, the total in the reasons). A new posting updates that suggestion instead of adding one.
A dismissal (or accept, or done) sticks until the company posts a role Ninshubur had not seen
when you decided; then a new suggestion starts.

Databases from before this change hold one hiring suggestion per posting. The next `run` expires
the ones a company suggestion now covers (never the ones you already decided). To see the numbers
first:

```
$ ninshubur suggest --regroup-dry-run
dry run, nothing written: 1 live per-posting hiring suggestion(s)
  1 of them belong to 1 company key(s) and expire on the next `ninshubur run`
  2 per-company suggestion row(s) will be created: agency-eu 1, agency-us 1
  0 live per-company suggestion(s) whose postings moved to another key (an alias) expire
  0 are past their own expiry date and expire on the next run anyway
  0 cover no posting that still qualifies; they stay live until their own expiry date
  1 per-posting suggestion(s) a person accepted, dismissed or marked done stay untouched
```

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

## 6c. Qualifying companies with a model

`ninshubur qualify` asks a model one question about each company, from the evidence the crawler
already stored, and keeps only answers that stand on that evidence. The question is yours: it
lives in the profile's `qualify:` block, and the code and the prompt know nothing about what a
"good" company is.

```yaml
qualify:
  subject: company                 # the noun for one row (default: company)
  question: Would this company plausibly buy outsourced B2B outbound in the next three months?
  must:                            # all of these must hold
    - it sells to other businesses
  must_not:                        # none of these may hold
    - it is itself a lead-generation or outbound agency
  labels: [yes, no, unsure]        # the only answers allowed; the first is the positive one
```

```bash
ninshubur qualify --profile acme --dry-run     # what would be asked and what it would cost; sends nothing
ninshubur qualify --profile acme               # ask the local model (llm.local in config.yaml)
ninshubur qualify --profile acme --show        # the stored answers
ninshubur qualify --profile acme --slot router --model haiku --max-usd 0.50   # a remote model, under a cap
```

What happens for each company:

1. **Evidence.** Up to 12 lines, one per stored item: the title, the declared fields (for a job
   posting: location, department, workplace, employment, remote, roles) and the start of the text.
   Lines naming most of the profile's own terms come first (unwanted terms count too), newest
   first among equals. Tainted and held items and anything marked as person data are left out.
   E-mail addresses, links, @handles and phone numbers are replaced by placeholders before the
   line leaves the database; names of people in running text are not detected.
2. **Prompt.** The lines go in a marked block that the instructions call data, not instructions.
3. **Check.** The reply must be one JSON object with an allowed label and at least one cited line
   number, and every cited number must be a line that was shown. A reply that fails gets one
   retry; after that the company is recorded as failed with the cause (`not_json`, `bad_shape`,
   `bad_label`, `no_citation`, `bad_citation`). A reason that trips a taint detector is dropped.
4. **Record.** Table `llm_runs` has one row per run (model, counts, tokens, cost, how it ended),
   table `qualifications` one row per company and run (label, cited lines, the items behind the
   lines, reason). `logs/llm.jsonl` records each call without prompt or reply text.

A company whose evidence, question and model are unchanged since its last good answer is not asked
again; `--again` asks anyway. `--limit N` asks only the first N.

**Cost.** The local model is the default and costs nothing. A remote slot (`--slot router` or
`cloud`) runs only with `--max-usd`, and the run stops before a batch whose worst case could pass
the cap; what was answered is kept and the next run asks the rest. The estimate counts four
characters per token, so read it as a guide. The daily caps under `llm.caps` apply as well. The only
network call is to the slot's `base_url`.

**Turning the router slot on.** `config.example.yaml` has an `llm.router` block with `enabled: false`. In your own
`config.yaml`, set `enabled: true`, point `base_url` at your router, and list under `models` each model you will ask
for, with `price_usd_per_mtok` giving its `input` and `output` price in US dollars per million tokens. Ninshubur
ships no prices: take them from your router's or provider's price list. The name you list is the name `--model` takes.
`api_key_env` is the name of the environment variable that holds the key, never the key. With the router enabled and
no priced model, or with a price below 0, `ninshubur check` refuses the config and names the key.

**Limits.** The answer is the model's reading of at most 12 lines; a company with hundreds of
postings is judged on the 12 that match your terms best. The check proves the cited lines exist,
not that the reason follows from them. How often a model agrees with a person is measured
separately, against a label sheet (below).

### The label sheet

To know how far to trust a model's answers, label a sample yourself and compare.

```bash
ninshubur export label-sheet --profile acme --sample 100 --seed 7
```

writes `feeds/label-sheet-acme-seed7.csv` and, beside it, `label-sheet-acme-seed7.csv.meta.json`.
Open the CSV in a spreadsheet, read each row's `evidence`, and type one of the profile's labels in
`label` (`note` is free text). Judge from the evidence shown: it is what the model is given. `links`
holds the web address of the page behind each line, if you need the whole posting.

- **Blind.** The sheet holds the name, the evidence lines and two empty cells. Nothing a model
  answered is read when it is built.
- **Stratified.** Companies are grouped by the sources their evidence came from and by whether they
  have few, some or many evidence lines (thirds of all companies). Rows are shared out over the
  groups in proportion, each group gets at least one row when the sample allows, and the row order
  does not follow the groups. The counts are in the meta file.
- **Repeatable.** The same seed draws the same rows on any machine. If more rows are asked for
  than there are companies with evidence, all are taken and the command says so.
- **Safe.** An existing sheet is never replaced unless you pass `--force`; `-o` writes elsewhere.
- **New rows only.** `--exclude feeds/label-sheet-acme-seed7.csv` (repeatable) leaves out every company
  already on that sheet, so a second sheet tests a prompt on companies you haven't judged yet. The
  meta file names the sheets left out, with a hash of each.

The meta file records the question, the labels, the seed and, per row, the group, a hash of the
evidence, the item ids and the line total, so a later comparison can prove the model was asked on
the same lines.

### Scoring a model against your labels

When every row has a label, ask the model on the sheet itself, then score it:

```bash
ninshubur qualify --profile acme --from-sheet feeds/label-sheet-acme-seed7.csv                  # local model
ninshubur qualify --profile acme --from-sheet feeds/label-sheet-acme-seed7.csv --model other:7b # another one
ninshubur evaluate --sheet feeds/label-sheet-acme-seed7.csv
```

- **Same evidence.** `--from-sheet` asks on the sheet's rows exactly as drawn, not on what the crawler
  has stored since. Each row's evidence hash is rebuilt from the cells and checked against the meta
  file, so an edited evidence cell, a deleted row or an unknown label stops the command with the row
  number. A spreadsheet's re-save (byte-order mark, an extra empty column) is fine. Keep a read-only
  copy of the labelled sheet and score against that copy (its meta file beside it as
  `<copy>.meta.json`, or `--meta` pointing at the original).
- **A second prompt.** `--qualify-file p2.yaml` asks a `qualify:` block from its own file, checked
  like a profile's. It must keep the same answers, so it is scored against the same labels. If you
  wrote it after seeing where a model went wrong on this sheet, its score here is optimistic: test it
  on a fresh sheet (another seed) before trusting it.
- **What is scored.** `evaluate` reads every model and prompt that answered on this sheet's evidence
  (or `--model` / `--prompt-hash` only), plus any `--rater copy.csv[=name]`: a copy of the sheet with
  its `label` column filled by someone or something else. A row with no usable answer counts as a
  disagreement and is also shown on its own.
- **The numbers.** Agreement with your labels (Wilson 95% interval); Cohen's kappa, agreement beyond
  chance (seeded bootstrap interval, `--reps`, `--seed`); precision and recall of the first label; an
  exact McNemar test of whether the model says the first label more or less often than you; and the
  baseline of always giving your commonest label. A local model gets a verdict against the best remote
  one: non-inferior when the paired bootstrap interval of the difference in agreement stays above
  -0.10 and at most 5 rows lack an answer.
- **The report.** Printed, and written to `feeds/eval-<profile>-seed<N>-<date>.md` and `.json`
  (readable by you only; `-o` elsewhere, `--no-save` writes nothing). It lists each model's confusion
  table and every row where a model differs from you. `evaluate` sends nothing.

## 6d. Finding new sources: explorer reach

The explorer (sections 6d to 6f) is **Linux only**: its parser runs in bubblewrap, its stop and hand-off files use
Linux file flags, and its timer is a systemd unit. On macOS and Windows every `ninshubur explore` command says so,
exits 2 and touches nothing; the rest of Ninshubur runs there as before.

`ninshubur explore reach` gathers *candidates* for the explorer into its own database
(`~/.ninshubur/explorer/explorer.db`). It never touches `ninshubur.db` except to read it, and never writes
`config.yaml`.

```bash
ninshubur explore reach --dry-run            # what would be asked; no request, nothing written
ninshubur explore reach --only boards        # one adapter: logged, hn, github, mastodon or boards
ninshubur explore reach --profile job-search --max-queries 2
ninshubur explore candidates                 # counts by kind and status, then the top rows
```

| Adapter | Where candidates come from | Requests |
|---|---|---|
| `logged` | hosts the main crawler logged as candidates | none |
| `hn`, `github`, `mastodon` | profile `topics`, then `keywords`, as search terms; one declared URL field per answer (story URL, repository homepage, post link card) | up to `--max-queries` (4) per API, least recently asked first |
| `boards` | Greenhouse, Ashby and Lever tokens in links the main database holds, minus the ones already configured | one per token, up to `--max-probes` (10) |

A board with open jobs is marked `trial` and the run prints the YAML lines to add to `config.yaml` by hand.
Every request goes through the same fetcher as the main crawler (robots.txt, public addresses only, caps),
logged in `~/.ninshubur/explorer/logs/fetch.jsonl`.

## 6e. The explorer: frontier and gate

The explorer is Ninshubur's open-web discovery, kept apart from your sources. It has its own database and logs in
`~/.ninshubur/explorer/`; deleting that folder resets it without touching anything else. It has a frontier (what
may be asked for), a gate (how it is asked), a sandboxed parser, a trial ladder and a one-way hand-off into the main
crawler (section 6f).

```
ninshubur explore offer --dry-run https://example.org/events/   # the verdict, nothing written
ninshubur explore queue --dry-run          # how the reach candidates would fare
ninshubur explore frontier                 # queued/fetched/refused, stopped sites and why, the caps in force
ninshubur explore step --dry-run           # the next requests, one per site; drop --dry-run to send them
touch ~/.ninshubur/explorer/STOP           # kill switch: no request leaves while this file exists
```

What it refuses, and logs to `explorer/logs/explorer.jsonl`:
- **Anything aimed at this machine or its network**, logged as `ssrf_attempt`: IP literals, decimal or octal IP
  spellings, ports other than 443, userinfo, single-label names, and names that resolve to a private, loopback,
  link-local, metadata, NAT64 or tunnel address.
- **Traps**, counted per registrable domain (from a vendored, dated Public Suffix List): more than 4 subdomains of
  one site, more than 8 URLs of one shape (calendars, pagination), dates walked out of range, repeated path
  segments, deep or long URLs, too many query parameters.
- **Sites that waste requests** are stopped: more than 6 of the last 10 pages near-copies of each other (generated
  mazes), a random path that answers 200 (soft-404), a parked or for-sale page, or 30 pages with nothing useful
  (a page is useful when it carries an event or a job with a date or a link).

A redirect to another site is never followed; its target is offered to the frontier like any other link. The caps
live in the shipped `policy.yaml` under `explorer:`; your `~/.ninshubur/policy.yaml` can tighten them, never loosen.

## 6f. The explorer: running it

```
ninshubur explore check                    # proves the parser sandbox is fenced (no home, no network, no secrets)
ninshubur explore run --dry-run            # the next pages and probes; nothing is sent
ninshubur explore run                      # one cycle: probe, fetch, parse in the sandbox, follow links, ladder
ninshubur explore sites --stage trial      # sites on a rung, with their counts
ninshubur explore status                   # stopped or running, sandbox, bytes, tripwire, disk, ladder, hand-off
ninshubur explore stop --reason "odd"      # the kill switch; `explore resume` undoes it and says why it was set
ninshubur explore inbox --dry-run          # what main Ninshubur would take from the hand-off
deploy/linux/install-explorer.sh --print   # the hourly systemd timer; drop --print to install, --remove to undo
deploy/linux/install-explorer.sh --check   # `explore check` under the timer's own limits; installs nothing
```

**The parser runs in bubblewrap** (`bwrap`, install it from your distribution: `apt install bubblewrap`). Inside:
no network, no home folder, an empty environment, read-only system files, 1 GB of memory, 10 s of CPU and 20 s of
wall clock per page. Without `bwrap` the explorer fetches nothing: an unparsed page is never used. Its output is
checked again outside (exact keys, types, sizes) and scanned for injection; a page with any taint offers no links
(a page with a `curl ... | sh` install line counts, so the explorer finds fewer sites through such pages), and one
with high taint hands nothing on. The page's text is never kept, so the detectors it tripped are what is recorded:
`explore run` prints them, each such page is a `page_taint` line in `explorer/logs/explorer.jsonl` (URL, severity,
the codes at that severity, lesser ones under `also`), and a site stopped for high taint carries the high-severity
codes in its reason, as in `taint: high-severity content (prompt_injection) on https://…`.

**The trial ladder.** A site moves candidate → probed (the random-path probe answered 404) → trial (one page a day
until its first clean page, then 5) → admitted (20 a day) after 20 clean pages, or after 7 days with at least 3.
Admitted is still the lowest trust: only you promote a site to a real source. An admitted site goes back to trial when
its address moves network (another IPv4 /16 or IPv6 /32), its front page drifts more than 24 of 64 simhash bits from
the one it was admitted with, or it hasn't had a clean page in 30 days. (The drift rule waits for a piece not built
yet: the explorer reads no URL twice, so an admitted site's front page isn't read again.) High taint, a soft-404, an
integrity failure (certificate error, decompression bomb, oversized body, invalid UTF-8) or a parser failure (timeout,
memory, crash, bad output) stops it. A link that turns out to be a PDF, an image or a feed stops nothing: its body is
never read, and it counts as a page that yielded nothing. (A certificate that fails while `robots.txt` is being read
shows as `robots_unreachable` instead: nothing is fetched from the site, and the probe and no-yield rules stop it.) A
site that doesn't answer the probe (it's down, its robots.txt says no, or it answers 429 or 503) is asked again once a
day and stopped after three tries (`probe_tries`), so it can't hold up the queue.

**Offline.** A laptop's timer fires the moment it wakes, often before the network is back. When a request gets no
answer, `explore run` first asks whether this machine is online (a fixed host of an enabled source resolves and has a
route; nothing is sent). If not, the cycle ends there: nothing is counted against any site, the queue stays as it
was, the log gets an `offline` line and the exit code is 0.

**The hand-off is one-way.** Events and jobs from sites in trial or admitted go to
`explorer/out/YYYY-MM-DD.jsonl` (4 KB a line, 2 MB a file, 14 files kept). The main daemon reads them each tick,
checks every line again, scans it again and stores what passes as **held** Tier 3 items of the source `explorer`,
marked "found by explorer": a suggestion built on one is marked held too and never leaves this machine. Each batch
it takes is one `explorer_inbox` line in `logs/policy.jsonl` (stored, updated, refused and why). Neither side opens
the other's database. `explorer: {read_inbox: false}` in `config.yaml` stops reading.

**Two switches stop it.** `explore stop` writes `explorer/STOP`: the gate checks it before every request, so a
running cycle halts at its next one, and the systemd unit won't start while it exists. The second layer is
`systemctl --user stop ninshubur-explorer.timer ninshubur-explorer.service`.

**The tripwire.** A request that would reach a private or reserved address (a site resolving to one, a redirect
pointing at one or at a port other than 443) stops that site and counts; three in 24 hours write the STOP file
(`by: tripwire`). Links a page only mentions (`http://localhost:8000` in a tutorial) are refused and logged but
don't count, and neither does a redirect to an app link or an FTP server. `explore resume` starts the count again from
zero, and so does deleting the STOP file by hand after a tripwire stop. `explore run` also refuses when the
explorer folder is over 500 MB (`disk_quota_bytes`).

**The service** (`install-explorer.sh`) runs one cycle an hour, writes only `~/.ninshubur/explorer/`, has no
capabilities and is capped at 1.5 GB, half a CPU and 64 tasks. It allows the namespaces, netlink and mount calls
`bwrap` needs, and nothing else beyond the main unit's set. Before it installs, the script runs `explore check`
inside a transient unit with those same limits, and installs nothing if the sandbox isn't fenced there.

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
the weekly digests. It opens no network port. When sources are due but none of their hosts
resolves or has a route (a laptop waking before Wi-Fi is back), it waits instead of fetching and
writes `offline` and `online` to `logs/policy.jsonl`; a waiting source is not counted as failing.

A running daemon follows changes to `sources` in `config.yaml` within one tick (`sources enable`,
`sources disable`, `sources add`, a hand edit). Any other setting, and `profiles.yaml`, needs a
restart: `systemctl --user restart ninshubur`. Pausing leaves a source's health alone, so a
quarantine lasts until `sources review <name> --clear`.

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

- **Models:** nothing is sent to a model unless you run `ninshubur qualify` (section 6c). The local
  model is free; a remote one needs `--max-usd`, and `--dry-run` prints the estimate first.

## 11. When something looks wrong

| Symptom | Where to look |
|---|---|
| "refusing to run" | the message names the config or policy line |
| a source shows `suspect` or `quarantined` | `ninshubur sources review <name>`; `--clear` after reading the reasons |
| a source shows `paused` | someone ran `sources disable`; `sources enable <name>` resumes it |
| a source shows `review_overdue` | update `reviewed_at` in its trust block after re-checking its terms |
| refused with `robots.txt on <host> answers 401` | the host hides robots.txt behind a login; after reading its terms, set `robots_reviewed_status` in the source's trust block (section 6) |
| a source shows `broken` | it answered 403/404/410 five times; the URL or the service changed |
| nothing fetched | `ninshubur run --force`; check `~/.ninshubur/logs/fetch.jsonl`, where a network error carries its reason in `detail` |
| something was refused | `~/.ninshubur/logs/policy.jsonl` has the reason and the rule |
| the queue is empty | `ninshubur profiles check`; the profile may lack the fields a kind needs |

`~/.ninshubur/logs/fetch.jsonl` is every request ever made: host, status, bytes, duration. If a
host is not in an adapter's allowlist it never appears there, because it was never contacted.

## What it will not do

Log into anything, solve CAPTCHAs, bypass paywalls, use proxies, scrape LinkedIn, Facebook, X
or Reddit, follow links found inside content, store a raw page, send person data to a cloud
model, or push anything anywhere without a human accepting it first. These are tested
invariants, not settings.
