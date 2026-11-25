# Changelog

Everything notable that changes for a person who uses or runs Ninshubur, written in plain words. The layout follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The release session turns `Unreleased` into the version
heading and its date.

## Unreleased

### Changed

- `PRIVACY.md` quotes what Lever's and Ashby's terms, API documentation and robots.txt say about reading public job postings, with their URLs; it drops the unchecked Hacker News request ceiling and says that page needs a person's eyes.

## 0.2.0 - 2026-11-25

### Added

- Policy-first crawling of admitted sources: a source is fetched only after a named reviewer admitted it through the H1–H8 checklist, only the fields it declared are kept, hostile or malformed input is quarantined, and rules (no model) build timed, costed suggestions, with an ICS calendar sink and a command line. Adapters for confs.tech, Greenhouse, Hacker News and status pages.
- Exports: `ninshubur export digest`, `suggestions` and `items` as Markdown, printable HTML or CSV, and one Markdown digest per profile each ISO week from the daemon; a profile can set its home country, costs come from config, and `profiles check` says which suggestion kinds each profile can feed.
- Hacker News "Who is hiring?" comments become hiring items (the company is read from the post's first line); "Who wants to be hired?" is skipped.
- An operator guide, `docs/USAGE.md`.
- A `provider_docs` adapter that reads LLM providers' own model documentation (prices, context windows, status, retirement dates, effort levels), and `ninshubur export euboulia-facts`, which writes those facts with a source on every figure.
- Event pages: the `event_sites` adapter crawls event home pages from the confs.tech list plus a few same-site speaker, sponsor and exhibitor pages and builds conference-prospecting suggestions; `ninshubur lookup` crawls one event now (`--dry-run` stores nothing), `show` prints the prospect table, and `export prospects` writes a CSV with the page each person or organisation came from.
- Ashby and Lever adapters read public job boards as hiring items; descriptions are never stored.
- `ninshubur companies` lists hiring companies, one row per company key; `--dupes` lists near-miss names of different companies and the alias line that merges each pair.
- A source can record the exact 401 or 403 its host's robots.txt answers, once a person has reviewed the host's terms; the exception covers an adapter's fixed hosts only.
- `ninshubur qualify`: a model answers a profile's own question about each company, and only answers that cite evidence lines it was shown are kept. Profiles gain a `qualify:` block, and `profiles check` names keys nobody reads.
- A language-model client with a router slot, which always counts as a remote model.
- E-mail addresses, links, handles and phone numbers are removed from any text bound for a model or a label sheet.
- `ninshubur export label-sheet` draws a blind, stratified, repeatable sample of companies for labelling by hand; `--exclude` leaves out subjects already on an earlier sheet, and a links column gives the page behind each evidence line.
- `ninshubur qualify --from-sheet` asks a model on a label sheet's own evidence exactly as drawn, and a filled sheet is read back with any edited evidence refused.
- `ninshubur evaluate` scores stored qualify answers, and filled copies of a sheet, against one person's labels: agreement and kappa with intervals, F1 for the positive label, tokens per rater, and list-price cost with `--prices`.
- `docs/case-study.md`: how far to trust the qualifier, with every number rebuilt from the evaluation reports by `scripts/case_study.py`.
- The explorer (Linux only), with its own database: it finds candidate sites from links the crawler already logged, from Hacker News, GitHub and Mastodon searches on a profile's topics and from job-board tokens, and moves each through a trial ladder (candidate, probed, trial, admitted) under per-site budgets and trap rules.
- `ninshubur explore` commands: candidates, reach, offer, queue, frontier, step, run, check, sites, inbox, stop, resume and status; `offer`, `queue`, `frontier` and `step` take `--dry-run`.
- A one-way hand-off from the explorer to the crawler: the explorer appends what it reads on trial and admitted sites to capped JSONL files, and the crawler re-checks every line and stores it held at Tier 3, without ever opening the explorer's database.
- A kill switch for the explorer (`explore stop`, `resume`, `status`, and a STOP file checked before every connection) and a tripwire that stops it after three attempts to reach a denied address.
- A hardened systemd timer and a disk quota for the explorer: `deploy/linux/install-explorer.sh` (`--print` shows what it would install, `--remove` undoes it).
- Operator guide sections on running the explorer: reach, the frontier, the gate, the kill switch, caps and what it never writes.
- A README with a banner, badges and the three rules as a table above the fold.
- `ninshubur export run-report`: one dated page of counts (fetches, refusals and quarantines, items, suggestions, sources, model runs and the explorer ladder) with no title, link, host or name on it, built read-only from the database and rebuilt by one command; `docs/run-report.md` says what each number means and what the page leaves out.
- README: a "What it looks like" block with real output captured by `scripts/readme_sample.py` from the replay of the shipped samples, and a "Known limits" section where each limit points at the test, doc or line that shows it.
- `scripts/private_words.py`: a check that fails when a word on a list of hashes (a former employer, a person) appears in the name or text of any tracked file; it never prints the word.
- `scripts/fresh_clone.py`: rehearses each install path from a clone of the committed HEAD (the README's quick start, CONTRIBUTING's set up and the requirements file), then runs the tests, the smoke script and `ninshubur check` in the CONTRIBUTING clone and prints one result line per step; `--dry-run` shows the plan and runs nothing.
- `ninshubur settings`: change the numbers that decide how suggestions are valued (what a kind of suggestion is worth, what your hour is worth, what a trip or a task costs; 24 keys, each with bounds) by command. `list` shows each value and where it came from, `preview` shows old and new and writes nothing, `apply --yes` writes a separate `settings.yaml` (your `config.yaml` is never rewritten) and one line in `logs/settings.jsonl`, `undo` puts back what an apply replaced, and `log` prints the lines. Keys that change what is fetched, stored, sent or admitted are shown and never set. A new value takes effect at the next process start.

### Changed

- Hiring suggestions come one per company and profile, not one per posting.
- A hiring role counts as a signal on its own, so a post matches without a profile keyword in its title, and a conference trip whose decide-by date has passed says so instead of printing a past date.
- A suggestion's tier badge shows the highest tier number among its evidence.
- `ninshubur evaluate` opens the database read-only: it never creates, migrates or writes it, and does not hold up a running crawler; a database without the tables it reads is refused with a message.
- Relicensed to AGPL-3.0-or-later, with a naming clause.
- The project's wording is neutral and contact goes through the repository.
- Every `explore` command says the explorer is Linux only and does nothing on another system; the explorer installer names systemd's own failure and checks the sandbox under the unit's own limits.
- Explorer stop rules tightened after review: a certificate error stops a site, a throttled probe is no answer and the cycle does not wait for it, a site that never answers cannot hold up the queue, links are followed only from a page with no taint, an offline machine costs no site anything, and a stopped candidate is rejected or quarantined according to who stopped it.
- The example config disables the status-page source whose host's robots.txt disallows `/api/`; it stays as a commented template.
- A failed connect in the fetch log names the socket's own reason, not the HTTP library's wrapper.

### Fixed

- A follow-up page on the www twin of an event's host is accepted, and a redirect between a host and its www twin counts as the same site.
- The daemon waits for the network after the machine wakes, instead of failing every due source and backing them off.
- Pausing a source keeps its quarantine, and the running daemon follows edits to the source list.
- A tainted item counts toward its source's taint events once, not once per fetch, so one ordinary post no longer quarantines a growing thread.
- PowerShell counts as a shell fragment only when it is in command shape, not as a word in prose.
- A redirect whose Location cannot be built into a request (`mailto:`, `tel:`, `javascript:`) ends that fetch, not the whole run.
- On Windows the CLI falls back to UTC with a warning when a time zone is missing, and `tzdata` is a runtime dependency there.
- The admission probe no longer records fetch state, so a source's first real run parses the page, and it parses with the adapter itself, so H4 measures real extraction.
- Explorer edge cases: a link that is no page stops nothing, a redirect target counts one hop further and never back to one, a redirect to another scheme counts only when it names an address, only a site's own front page is compared for drift, a taint stop names its detectors, a probe refused for its address is reported as the stop it is, deleting the STOP file by hand does not trip the tripwire again, an event link over the cap costs the link and not the site, an event with an http:// link reaches the crawler, and the explorer's own failures use up no page.
- `pip install -r requirements-dev.in` works from a clean clone: the file named a `requirements.txt` that is not in the repository, and now pulls in the runtime requirements instead.

### Security

- The URL gate allows port 443 only and refuses disguised IP addresses (such as `127.1` or a decimal number) and single-label names before any lookup.
- The fetcher resolves each host first and never contacts loopback, private, link-local or carrier-grade addresses, and the tunnel and translation ranges (NAT64, 6to4, Teredo and others) are denied on every supported Python.
- The fetcher connects to the address it checked, so a changed DNS answer cannot redirect it.
- Every request has one total deadline, so a server that sends one byte at a time cannot hold a fetch; requests to a host never overlap, across sources.
- Gzip is decoded in small steps and encodings nobody asked for are refused; the content type is checked from the headers before the body is read.
- robots.txt fails closed: redirects are followed within the same site only, an oversized file with no newline is never read as "no rules", an unreadable file disallows and is asked again in ten minutes, and it is asked for every redirect target.
- JSON from the network cannot crash a parse by nesting, and the depth is counted on the text before the parser sees it.
- Characters that hide text are flagged and honest invisible ones are removed; a single joiner is plain text and a run of them is flagged.
- Regular expressions on crawled or model text run in linear time (an audit of five, and cases for the explorer's trap rules).
- Terminal control sequences in crawled text cannot reach the CLI's output, a carriage return in crawled text is escaped in the ICS calendar, and CSV cells that start with `=`, `+`, `-` or `@` are guarded.
- The explorer's parser runs in a bubblewrap sandbox with no network, no home folder and no secrets, at bounded cost.
- The crawl invariants are pinned by a test (no bypass or evasion tooling is imported, no private-state file is tracked).
