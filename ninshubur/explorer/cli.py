"""``ninshubur explore ...``: the explorer's commands. Each lane adds its subcommands in its own section below."""
from __future__ import annotations

import argparse

from ..db import rows
from . import store


def cmd_candidates(args, app) -> int:
    conn = store.connect(app.home)
    try:
        where, params = ("WHERE status=?", [args.status]) if args.status else ("", [])
        if args.kind:
            where += (" AND " if where else "WHERE ") + "kind=?"
            params.append(args.kind)
        total = rows(conn, f"SELECT kind, status, COUNT(*) AS n FROM explorer_candidates {where} GROUP BY kind, status "
                           "ORDER BY kind, status", params)
        if not total:
            print("no candidates yet")
            return 0
        for r in total:
            print(f"{r['kind']:<12} {r['status']:<12} {r['n']}")
        if args.limit:
            print()
            for r in rows(conn, f"SELECT id, url, kind, via, count FROM explorer_candidates {where} "
                                "ORDER BY count DESC, id LIMIT ?", [*params, args.limit]):
                print(f"#{r['id']:<6} x{r['count']:<4} {r['kind']:<12} {r['url'][:100]}  (via {r['via']})")
        return 0
    finally:
        conn.close()


def add_commands(sub: argparse._SubParsersAction) -> None:
    x = sub.add_parser("explore", help="the explorer: open-web discovery with its own database and fences")
    xs = x.add_subparsers(dest="xcmd", required=True)
    c = xs.add_parser("candidates", help="what the reach adapters have proposed, by kind and status")
    c.add_argument("--status", choices=store.STATUSES)
    c.add_argument("--kind", choices=store.KINDS)
    c.add_argument("--limit", type=int, default=20, help="rows to list after the counts (0: counts only; default 20)")
    c.set_defaults(fn=cmd_candidates)
    # -- reach (lane D) ---------------------------------------------------------------------------
    # -- frontier and gate (lane E1) --------------------------------------------------------------
