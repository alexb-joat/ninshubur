# Privacy and lawful use

Ninshubur gathers business information to time outreach. It is designed so that the narrowest
lawful reading is the only reading the code can take.

## Person data

- **Scope.** B2B professional fields only: `name, handle, title, company, company_domain,
  office_city, country, tz, seniority, department`. Everything else (home address, street,
  postcode, personal email or phone, date of birth, photo) is dropped at the storage gate
  (`policy.allow_store`) and never written.
- **Sources.** Licensed providers under contract (Apollo, HubSpot, the operator's own platform),
  clearly public business pages, and official social APIs (Mastodon now; Bluesky after a
  reviewed spike). No LinkedIn, no Reddit HTML, no Facebook, no X, nothing behind a login.
- **Granularity.** City level only. Nominatim geocoding (Phase 4) resolves city and country,
  never addresses.
- **Legal basis.** Each record carries `legal_basis` (`legitimate_interest`, `contract`,
  `public_business_info` or `licensed`). The legitimate-interest assessment is reviewed and
  signed off by the operator (Alex, 2026-10-14).
- **Retention.** `retention_until = fetched_at + 12 months`, hourly sweep, `forget person|company`
  cascades through entities, item links, licensed items and signals; suggestions become
  `redacted`; the erasure is logged in `logs/policy.jsonl`.
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
- **Retention and erasure:** `retention_until = fetched_at + 12 months`; `ninshubur forget person "<name>"`
  removes the person and every link; the erasure is logged with hashes only.
- **Volume:** at most 20 events a day, five pages per event, one request per second per host.

## Per-source terms (to re-verify at each trust review)

| Source | Notes |
|---|---|
| confs.tech | MIT-licensed JSON on GitHub; attribution kept in the trust block. |
| Hacker News (Algolia API) | Public API without a key; we self-cap at 1 req/s and a few dozen requests a day. Comment text is user content: taint-scanned, Tier 3. The API documentation page, https://hn.algolia.com/api, answered 200 to a plain GET with a JavaScript-only shell (its body holds a title and scripts, no readable text), so no request ceiling and no terms could be read from it (read 2026-11-25); a person has to open it in a browser. |
| Greenhouse | Public job-board API, `content=false`; only id, title, dates, location, URL. |
| Lever | Public postings API, no key. The answer always carries each posting's description, lists, salary and apply link; none is a declared field, so all are dropped and never stored. Kept: id, title, creation date, location, department, team, commitment, workplace type, country, URL. Read 2026-11-25: the API's documentation (https://github.com/lever/postings-api) says "all job postings in the published state are publicly viewable" and "These jobs may be scraped by third parties"; its only rate limit is for application POST requests ("more than 2 application POST requests per second"), and we send GET only. https://api.lever.co/robots.txt answers 200 with `User-agent: *`, `Allow: /` and `Crawl-delay: 1`, which our one request per second per host meets. The terms at https://www.lever.co/legal/terms-of-service (last updated August 25, 2023) set their own scope: "By mutually executing an Order Form with Lever that references these Terms of Service or by using the Lever website, a customer agrees to these Terms of Service and the applicable Order Form". They say nothing about scraping, crawling, robots, bots or automated reading of public postings, and mention the API only in 9.5 (customers pulling their own data). They also say "Lever's direct competitors are prohibited from accessing the software and/or services provided by Lever, except with Lever's prior express written consent." The page links a legal index (https://www.lever.co/legal) that was not opened. (read 2026-11-25) |
| Ashby | Public job posting API, no key. The answer always carries each posting's description; it is not a declared field, so it is dropped and never stored. Kept: id, title, date, location, department, workplace and employment type, URL. Ashby's host answers 401 on robots.txt, so each source records `robots_reviewed_status: 401` after a person's review. Read 2026-11-25: https://api.ashbyhq.com/robots.txt answers 401 with the body "Unauthorized". The API page (https://developers.ashbyhq.com/docs/public-job-posting-api) says "This API allows you to get data for all currently published Job Postings for your organization. If you host your own careers page, you can use this data to populate it." and states no rate limit and no terms. The terms at https://www.ashbyhq.com/resources/terms are the "Ashby Customer Terms of Service" (last updated September 29, 2025); their scope sentence is "This Agreement is effective as of the date Customer first completes a purchase with Ashby (via an Order or otherwise) or logs into the Service", and the acceptable-use clause (5.1) binds "Customer". They say nothing about scraping, crawling, robots, bots or reading published postings through the API. The page links an index of further terms and policies (https://www.ashbyhq.com/resources/terms-and-policies) that was not opened. (read 2026-11-25) |
| Statuspage | Public JSON summaries of providers we depend on. |
| Event websites | Each organiser's own terms; robots.txt honoured; same-host public listings only; see above. |
| arXiv | At least 3 s between requests, identifying User-Agent. |
| Stack Exchange | CC BY-SA content needs attribution when quoted; quota 300/day keyless, honour `backoff`. |
| Nominatim (OSMF) | Max 1 req/s, at most 4 req/min for recurring scripts, identifying UA, results cached 180 d, ODbL attribution. |
| Mastodon | Per-instance rules reviewed on admission; 300 req / 5 min per IP. |
| Google News RSS | Pointer only: publisher domain recorded, article never fetched. |
| Apollo / HubSpot | Under Alex's contracts; export and retention terms apply; credits are spent only on accepted suggestions or explicit refresh and every credit is in the ledger. |

## Crawling floor

robots.txt and Crawl-Delay honoured, at most 1 request per second per host, identifying
User-Agent with a contact address, GET only except licensed APIs, no login, CAPTCHA or paywall
bypass, no proxies or fingerprint evasion. A source that answers with a CAPTCHA is marked
`broken` and replaced by an official API or a licensed feed.

Questions: open an issue at https://github.com/alexb-joat/ninshubur/issues.
