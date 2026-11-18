"""The explorer: Ninshubur's open-web discovery, fenced off from the main crawler.

Reach adapters turn what Ninshubur already knows (logged links, profile topics, job-board tokens) into
*candidates*. The frontier decides which candidates are worth a request and refuses traps; the gate fetches
through the shared hardened ``Fetcher``. The explorer keeps its own database (``~/.ninshubur/explorer/``), so it
never contends with the main crawler's locks, and nothing it learns moves trust in the main database.
"""
