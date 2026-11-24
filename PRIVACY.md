# Privacy and lawful use

Ninshubur gathers business information to time outreach. It is designed so that the narrowest
lawful reading is the only reading the code can take.

## Person data

- **Scope.** B2B professional fields only: `name, handle, title, company, company_domain,
  office_city, country, tz, seniority, department`. Everything else (home address, street,
  postcode, personal email or phone, date of birth, photo) is dropped at the storage gate
  (`policy.allow_store`) and never written.
- **Sources.** One source stores person records today: the public speaker and sponsor pages of
  event websites (next section). Hacker News items keep the comment author's public username. No
  licensed contact provider and no social API is connected; none is built. No LinkedIn, no Reddit
  HTML, no Facebook, no X, nothing behind a login.
- **Granularity.** City level only: no street, postcode or coordinates.
- **Legal basis.** Each record carries `legal_basis` (`legitimate_interest`, `contract`,
  `public_business_info` or `licensed`). The legitimate-interest assessment is reviewed and
  signed off by the operator (Alex, 2026-10-14).
- **Retention.** Every person record carries `retention_until = fetched_at + 12 months`; a new
  sighting moves the date. A sweep runs once an hour under `ninshubur run --daemon`, and at any
  `ninshubur run` when an hour has passed since the last one. It deletes person records past their
  date with their page links, replaces their names in suggestion, feedback and qualify texts with
  `[removed]`, and deletes snapshots older than 30 days (the newest of each source stays) and
  quarantine rows older than 90 days. `ninshubur status` shows when it last ran and what it removed.
- **Erasure.** `ninshubur forget person "<name>"` and `ninshubur forget company "<name or domain>"`
  show what they would remove, ask, and then delete it: the record and its page links; for a
  company also its stored postings, signals, aliases and qualify answers, and its own suggestions
  are emptied and marked `redacted`. The name is replaced with `[removed]` in the texts Ninshubur
  wrote. A salted hash of the name goes on a forgotten list, so a later crawl does not store it
  again (for a person that includes namesakes); `--lift` takes it off. Deleted rows are overwritten
  in the database file. `logs/policy.jsonl` records the erasure with a hash and counts, no name.
  **Limits, each shown in the preview:** your own files (exported digests, CSVs, label sheets,
  config and profiles) are listed, not changed; crawled text that mentions a person's name (a
  posting title, a comment) is counted, not edited; people who name a forgotten company as their
  employer keep their records until you forget them by name.
- **Models.** Anything containing person data goes to a local model only; the cloud endpoint is
  refused before any request is built. Social posts keep handle, display name, URL, a short
  excerpt and the date; accounts marked `noindex`, not `discoverable`, or labelled
  `!no-unauthenticated` are skipped.
- **Qualifying.** `ninshubur qualify` runs only when you start it. It sends a model up to 12 evidence
  lines per company, built from items without person data; e-mail addresses, links, @handles and
  phone numbers are replaced first (names of people in running text are not detected). The default
  is the local model; a remote model needs `--slot` and a `--max-usd` cap. Prompts and replies are
  not logged; the label, the cited line numbers and a one-sentence reason are stored.

## Event websites (speakers, sponsors, exhibitors)

- **What:** the public speaker and sponsor listings that event organisers publish on their own sites,
  for events in an admitted curated dataset. Per person: name, title, company, talk, role and the
  public LinkedIn profile URL the organiser printed next to the name (stored as text; LinkedIn is a
  denied host and is never visited). Per organisation: name, domain, role.
- **Not:** bios, photos, emails, phone numbers, addresses, anything behind a login, attendee lists
  (which are not public), pages on other hosts than the event's own.
- **Legal basis:** `public_business_info`. A speaker or sponsor has chosen to be listed in a
  professional capacity at a business event; the use is B2B outreach about a service relevant to
  that capacity. Legitimate-interest assessment (signed off by Alex, 2026-10-14): purpose
  (B2B prospecting), necessity (the listing is the only source of who paid for presence), balance
  (professional context only, no sensitive data, 12-month retention, erasure on request via `forget`,
  no automated decisions, no profiling beyond title/company matching), transparency (identifying
  User-Agent with a contact address on every request; robots.txt honoured).
- **Retention and erasure:** `retention_until = fetched_at + 12 months`, enforced by the hourly sweep;
  `ninshubur forget person "<name>"` removes the person and every link and keeps the name from being
  stored again; the erasure is logged with a hash and counts (see Person data above).
- **Volume:** at most 20 events a day; per event the homepage and at most four pages it links to; one request per second per host.

## Per-source terms (to re-verify at each trust review)

| Source | Notes |
|---|---|
| confs.tech | MIT-licensed JSON on GitHub; attribution kept in the trust block. |
| Hacker News (Algolia API) | Public API without a key; published request ceiling to verify (commonly cited 10 000/h); we self-cap at 1 req/s and a few dozen requests a day. Comment text is user content: taint-scanned, Tier 3. |
| Greenhouse | Public job-board API, `content=false`; only id, title, dates, location, URL. |
| Lever | Public postings API, no key. The answer always carries each posting's description, lists, salary and apply link; none is a declared field, so all are dropped and never stored. Kept: id, title, creation date, location, department, team, commitment, workplace type, country, URL. |
| Ashby | Public job posting API, no key. The answer always carries each posting's description; it is not a declared field, so it is dropped and never stored. Kept: id, title, date, location, department, workplace and employment type, URL. Ashby publishes no terms for this API and its host answers 401 on robots.txt, so each source records `robots_reviewed_status: 401` after a person's review. |
| Statuspage | Public JSON summaries of providers we depend on. |
| Event websites | Each organiser's own terms; robots.txt honoured; same-host public listings only; see above. |

No adapter exists for arXiv, Stack Exchange, Nominatim, Mastodon, Google News or a licensed contact
provider, so nothing is fetched from them. Each gets a row here when its adapter is admitted.

## Crawling floor

robots.txt and Crawl-Delay honoured, at most 1 request per second per host, identifying
User-Agent with a contact address, GET only except licensed APIs, no login, CAPTCHA or paywall
bypass, no proxies or fingerprint evasion. A source that answers with a CAPTCHA is marked
`broken` and replaced by an official API or a licensed feed.

Questions: open an issue at https://github.com/alexb-joat/ninshubur/issues.
