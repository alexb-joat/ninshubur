"""``ninshubur explore ...``: the explorer's commands. Each lane adds its subcommands in its own section below."""
from __future__ import annotations

import argparse
import sys

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


def cmd_reach(args, app) -> int:
    from .reach.run import Ctx, reach
    profiles = app.profiles
    if args.profile:
        if args.profile not in profiles:
            print(f"explore reach: no profile {args.profile!r} in profiles.yaml; have: {', '.join(sorted(profiles))}",
                  file=sys.stderr)
            return 2
        profiles = {args.profile: profiles[args.profile]}
    ctx = Ctx(home=app.home, policy=app.policy, cfg=app.cfg, profiles=profiles, now=app.now, dry_run=args.dry_run,
              max_queries=args.max_queries)
    try:
        rep = reach(ctx, only=args.only)
    except FileNotFoundError as e:
        print(f"explore reach: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print("dry run: no request made, nothing written")
        for adapter, what in rep.planned:
            print(f"  {adapter:<9} {what}")
    for kind, a in rep.added.items():
        print(f"{kind:<12} new {a.new}, seen again {a.seen_again}, refused {a.refused}")
    for n in rep.notes:
        print(n)
    if not args.dry_run:
        print(f"requests: {rep.requests}")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    x = sub.add_parser("explore", help="the explorer: open-web discovery with its own database and fences")
    xs = x.add_subparsers(dest="xcmd", required=True)
    c = xs.add_parser("candidates", help="what the reach adapters have proposed, by kind and status")
    c.add_argument("--status", choices=store.STATUSES)
    c.add_argument("--kind", choices=store.KINDS)
    c.add_argument("--limit", type=int, default=20, help="rows to list after the counts (0: counts only; default 20)")
    c.set_defaults(fn=cmd_candidates)
    # -- reach (lane D) ---------------------------------------------------------------------------
    from .reach.run import ADAPTERS
    r = xs.add_parser("reach", help="turn logged links, profile topics and job-board tokens into candidates")
    r.add_argument("--only", choices=ADAPTERS, help="run one reach adapter (default: all)")
    r.add_argument("--profile", help="use only this profile's topics and keywords (default: every profile)")
    r.add_argument("--max-queries", type=int, default=4, help="queries per search API per run (default 4)")
    r.add_argument("--dry-run", action="store_true", help="list what would be asked; no request, nothing written")
    r.set_defaults(fn=cmd_reach)
    # -- frontier and gate (lane E1) --------------------------------------------------------------
