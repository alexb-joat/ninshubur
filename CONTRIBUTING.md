# Contributing

Ninshubur fetches from the open internet and stores what it finds, so the bar for changes is
"small, tested, and no new way for content to reach code, disk or a model without passing the
policy".

## Workflow

1. **Set up**
   ```bash
   python3 -m venv .venv && source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```
2. **Make the change** and add tests in `tests/`. The mock transport answers 599 for any host a
   test did not route, so a stray fetch fails loudly.
3. **Check** before pushing:
   ```bash
   python -m pytest -q
   python scripts/smoke.py
   ```
4. **Open a pull request.** CI, CodeQL and dependency review run automatically.

## Adding a source

Every adapter needs: an `AdapterSpec` with a fixed `hosts` set, a schema in `schemas.py` that
names only the fields a declared business question needs, a trust block in the config with all
H1-H8 fields, a trimmed synthetic sample (under 10 KB) in `tests/samples/<adapter>/`, and a test
that proves no follow-up URL comes from content. `ninshubur sources test NAME` must pass.

## Ground rules

- **Network.** Only `fetch.py` opens connections. Adapters are pure; no new network paths.
- **Content is data.** Never store a raw body, never fetch a URL found in a payload, never send
  tainted text to a model, never log prompt or response text.
- **No** `subprocess`, `eval`, `pickle`, browser automation or CAPTCHA/paywall tooling: tests fail on them.
- **Dependencies.** A new package needs a justification. Lockfiles (hash-pinned, via `uv pip
  compile --universal --generate-hashes`) arrive in Phase 5.
- **Secrets** live in environment variables named by the config, never in files or tests.
- **Security issues** go to the Security tab, not public issues.

## Style

Small functions, type hints, plain names. Match the code around you.
