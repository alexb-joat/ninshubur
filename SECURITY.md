# Security

Ninshubur treats every byte from the network as hostile data.

- **Single network gate.** `ninshubur/fetch.py` is the only module that opens a connection.
  It refuses anything the policy does not allow (https only, host must be in the adapter's
  fixed set, no userinfo, no IP literals, no punycode, denied hosts, robots.txt, source health,
  review expiry), never follows a redirect off-host or to http, streams under a byte cap with a
  decompression guard, and logs every request to `logs/fetch.jsonl`.
- **Declared extraction only.** Responses are parsed in memory and projected through a schema;
  undeclared fields and raw bodies are never stored. Text fields are NFC-normalised, HTML- and
  hidden-Unicode-stripped, length-capped and taint-scanned.
- **Quarantine stores hashes, not content.** `quarantine` rows carry a URL hash and an excerpt
  hash; `logs/policy.jsonl` records the codes. `tests/test_malicious.py` greps the whole data
  directory to prove injected text never lands on disk.
- **No code paths for evasion.** No `subprocess`, `eval`, `pickle`, browser automation, CAPTCHA
  or paywall bypass anywhere in the package (asserted by tests).
- **Local files 0600, SQL parameterised, WAL.** Secrets only via environment variable names in
  config; `.env` loading warns when the file is group/world readable.
- **LLM isolation (Phase 3).** Tainted text is never sent to a model; clean text goes inside
  `<data>` tags as untrusted content; model output is never a fetch target, command or evidence.

Report issues privately to alex@leadget.ai.
