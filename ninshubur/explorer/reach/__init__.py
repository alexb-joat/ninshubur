"""Reach adapters: what turns Ninshubur's own knowledge into explorer candidates.

Every network call this package makes, all GET, unauthenticated, through the shared hardened ``Fetcher`` (robots.txt,
pinned public addresses, deadline, byte caps) bound to the explorer's own database and logs:

- ``hn.algolia.com/api/v1/search``: stories matching a profile topic or keyword (``search.py``)
- ``api.github.com/search/repositories``: repositories with a profile topic (``search.py``)
- ``mastodon.social/api/v1/timelines/tag/<tag>``: public posts with a profile topic as hashtag (``search.py``)
- ``boards-api.greenhouse.io``, ``api.ashbyhq.com``, ``api.lever.co``: one probe per job-board token (``boards.py``)

Logged links (``logged.py``) read the main database read-only and make no request. Queries come only from
templates over profile topics and keywords; URLs come only from declared JSON fields, never from page text.
"""
