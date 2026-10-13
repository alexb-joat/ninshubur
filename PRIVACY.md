# Privacy and lawful use

Ninshubur gathers business information to time outreach. It is designed so that the narrowest
lawful reading is the only reading the code can take.

## Person data

- **Scope.** B2B professional fields only: `name, handle, title, company, company_domain,
  office_city, country, tz, seniority, department`. Everything else (home address, street,
  postcode, personal email or phone, date of birth, photo) is dropped at the storage gate
  (`policy.allow_store`) and never written.
- **Sources.** Licensed providers under contract (Apollo, HubSpot, the LeadGet platform),
  clearly public business pages, and official social APIs (Mastodon now; Bluesky after a
  reviewed spike). No LinkedIn, no Reddit HTML, no Facebook, no X, nothing behind a login.
- **Granularity.** City level only. Nominatim geocoding (Phase 4) resolves city and country,
  never addresses.
- **Legal basis.** Each record carries `legal_basis` (`legitimate_interest`, `contract`,
  `public_business_info` or `licensed`). The legitimate-interest assessment is reviewed and
  signed off by a human before Phase 4 ships.
- **Retention.** `retention_until = fetched_at + 12 months`, hourly sweep, `forget person|company`
  cascades through entities, item links, licensed items and signals; suggestions become
  `redacted`; the erasure is logged in `logs/policy.jsonl`.
- **Models.** Anything containing person data goes to a local model only; the cloud endpoint is
  refused before any request is built. Social posts keep handle, display name, URL, a short
  excerpt and the date; accounts marked `noindex`, not `discoverable`, or labelled
  `!no-unauthenticated` are skipped.

## Per-source terms (to re-verify at each trust review)

| Source | Notes |
|---|---|
| confs.tech | MIT-licensed JSON on GitHub; attribution kept in the trust block. |
| Hacker News (Algolia API) | Public API without a key; published request ceiling to verify (commonly cited 10 000/h); we self-cap at 1 req/s and a few dozen requests a day. Comment text is user content: taint-scanned, Tier 3. |
| Greenhouse / Lever | Public job-board APIs, `content=false`; only id, title, dates, location, URL. |
| Statuspage | Public JSON summaries of providers we depend on. |
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

Questions: alex@leadget.ai.
