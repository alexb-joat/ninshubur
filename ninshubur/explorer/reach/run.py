"""One reach run: gather candidates from the chosen adapters into the explorer database, or (dry run) list what
would be asked without a single request."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import store
from ..store import Added
from . import logged

ADAPTERS = ("logged", "hn", "github", "mastodon", "boards")


@dataclass
class ReachReport:
    added: dict[str, Added] = field(default_factory=dict)  # candidate kind -> counts
    planned: list[tuple[str, str]] = field(default_factory=list)  # (adapter, what would be asked)
    notes: list[str] = field(default_factory=list)


def run_logged(home: Path, conn, report: ReachReport, *, dry_run: bool, now: float) -> None:
    main = logged.open_main_readonly(home)
    try:
        cands = logged.logged_links(main)
    finally:
        main.close()
    if dry_run:
        report.planned.append(("logged", f"logged hosts in the main database: {len(cands)} (no request)"))
        return
    report.added["logged_link"] = store.add_candidates(conn, cands, now)


def reach(home: Path, *, only: str | None = None, dry_run: bool = False, now: float) -> ReachReport:
    """A dry run opens nothing for writing: no explorer database is created and no request is made."""
    report = ReachReport()
    conn = None if dry_run else store.connect(home)
    try:
        if only in (None, "logged"):
            run_logged(home, conn, report, dry_run=dry_run, now=now)
    finally:
        if conn is not None:
            conn.close()
    return report
