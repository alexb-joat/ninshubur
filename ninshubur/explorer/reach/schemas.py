"""Schemas for the reach APIs, registered into the shared registry on import. Only the fields below are ever read;
post text, descriptions and account data are not declared, so they are never stored."""
from __future__ import annotations

from ...schemas import SCHEMAS, validate_schema

REACH_SCHEMAS: dict[str, dict] = {
    # GitHub repository search (docs.github.com/en/rest/search/search). ``homepage`` is the project's own site.
    "github_repos.v1": {
        "root": "items",
        "max_items": 100,
        "required": ["full_name"],
        "fields": {
            "full_name": {"type": "str", "max": 140, "pattern": r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$"},
            "homepage": {"type": "url"},
            "stargazers_count": {"type": "int", "min": 0, "max": 10**8},
            "fork": {"type": "bool"},
            "archived": {"type": "bool"},
        },
    },
    # Mastodon hashtag timeline (docs.joinmastodon.org/methods/timelines/): a top-level list of statuses. Only the
    # link-preview card's URL is read, never the post's HTML.
    "mastodon_statuses.v1": {
        "max_items": 100,
        "required": ["id"],
        "fields": {
            "id": {"type": "str", "max": 24, "pattern": r"^\d{1,24}$"},
            "created_at": {"type": "datetime"},
            "card_url": {"type": "url", "path": "card.url"},
        },
    },
    # Lever postings (github.com/lever/postings-api): a top-level list. Descriptions are not declared.
    "lever_postings.v1": {
        "max_items": 2000,
        "required": ["id", "text", "hostedUrl"],
        "fields": {
            "id": {"type": "str", "max": 36, "pattern": r"^[0-9a-fA-F-]{36}$"},
            "text": {"type": "str", "max": 200, "text": True},
            "hostedUrl": {"type": "url"},
        },
    },
}

for _k, _v in REACH_SCHEMAS.items():
    validate_schema(_k, _v)
    SCHEMAS.setdefault(_k, _v)
