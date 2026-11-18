"""``ninshubur explore ...``: the explorer's commands. Each lane adds its subcommands in its own section below."""
from __future__ import annotations

import argparse
import asyncio
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


# -- frontier and gate (lane E1) ----------------------------------------------------------------------------------
def _frontier(app):
    from .frontier import Frontier
    conn = store.connect(app.home)
    return conn, Frontier(conn, app.policy, app.home, now=app.now)


def cmd_offer(args, app) -> int:
    conn, fr = _frontier(app)
    try:
        bad = 0
        for url in args.urls:
            o = fr.offer(url, dry_run=args.dry_run)
            bad += not o.queued
            verdict = ("would queue" if args.dry_run else "queued") if o.queued else f"refused: {o.code}"
            print(f"{verdict:<32} {o.url[:100]}  (site {o.site or '-'})")
        return 1 if bad and len(args.urls) == bad else 0
    finally:
        conn.close()


def cmd_queue(args, app) -> int:
    """Offer reach candidates (not boards: those are probed as sources) that the frontier hasn't seen."""
    conn, fr = _frontier(app)
    try:
        todo = rows(conn, """SELECT c.id, c.url FROM explorer_candidates c
                             WHERE c.status='candidate' AND c.kind != 'board'
                               AND NOT EXISTS (SELECT 1 FROM explorer_frontier f WHERE f.candidate_id = c.id)
                             ORDER BY c.count DESC, c.id LIMIT ?""", (args.limit,))
        tally: dict[str, int] = {}
        for c in todo:
            o = fr.offer(c["url"], candidate_id=c["id"], found_from=f"candidate #{c['id']}", dry_run=args.dry_run)
            key = "queued" if o.queued else o.code or "?"
            tally[key] = tally.get(key, 0) + 1
        verb = "would be" if args.dry_run else "were"
        print(f"{len(todo)} candidates {verb} offered: " + (", ".join(f"{k} {v}" for k, v in sorted(tally.items())) or "none"))
        return 0
    finally:
        conn.close()


def cmd_frontier(args, app) -> int:
    conn, fr = _frontier(app)
    try:
        by = rows(conn, "SELECT status, COUNT(*) AS n FROM explorer_frontier GROUP BY status ORDER BY status")
        sites = rows(conn, "SELECT status, COUNT(*) AS n FROM explorer_sites GROUP BY status ORDER BY status")
        print("urls:  " + (", ".join(f"{r['status']} {r['n']}" for r in by) or "none"))
        print("sites: " + (", ".join(f"{r['status']} {r['n']}" for r in sites) or "none"))
        stopped = rows(conn, "SELECT site, status_reason FROM explorer_sites WHERE status != 'open' "
                             "ORDER BY status_at DESC LIMIT ?", (args.limit,))
        for r in stopped:
            print(f"  stopped {r['site']}: {r['status_reason']}")
        lim = fr.limits
        print(f"caps: {lim.hosts_per_site} hosts and {lim.pages_per_site_day} pages a day per site, "
              f"{lim.pages_per_shape} URLs per shape, {lim.max_body_bytes:,} bytes a page, {lim.daily_bytes:,} bytes a day")
        return 0
    finally:
        conn.close()


def cmd_step(args, app) -> int:
    from .gate import ExplorerGate
    conn = store.connect(app.home)
    gate = ExplorerGate(conn, app.policy, app.cfg, app.home, now=app.now)
    try:
        if gate.stopped:
            print(f"the explorer is stopped: {gate.stop_path} exists (remove it to resume)", file=sys.stderr)
            return 2
        if args.dry_run:
            batch = gate.frontier.next_batch(args.limit)
            for r in batch:
                print(f"would fetch {r['url'][:110]}")
            print(f"{len(batch)} requests planned; nothing was sent")
            return 0
        outs = asyncio.run(gate.step(args.limit))
        for o in outs:
            extra = f"; site stopped: {o.stopped_site}" if o.stopped_site else ""
            print(f"{o.code:<24} {o.status or '-':<4} {o.bytes:>9,} B  {o.url[:90]}{extra}")
        print(f"{len(outs)} fetched (bodies are not kept until the parser lands, session E2)")
        return 0
    finally:
        asyncio.run(gate.aclose())
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
    o = xs.add_parser("offer", help="offer URLs to the frontier: queued, or the reason it refuses them")
    o.add_argument("urls", nargs="+", metavar="URL")
    o.add_argument("--dry-run", action="store_true", help="show the verdict; write and log nothing")
    o.set_defaults(fn=cmd_offer)
    q = xs.add_parser("queue", help="offer reach candidates the frontier hasn't seen yet (board tokens excepted)")
    q.add_argument("--limit", type=int, default=200, help="candidates to offer (default 200)")
    q.add_argument("--dry-run", action="store_true", help="count the verdicts; write and log nothing")
    q.set_defaults(fn=cmd_queue)
    f = xs.add_parser("frontier", help="queued, fetched and refused URLs; stopped sites and why; the caps in force")
    f.add_argument("--limit", type=int, default=20, help="stopped sites to list (default 20)")
    f.set_defaults(fn=cmd_frontier)
    st = xs.add_parser("step", help="fetch up to --limit queued URLs, one per site (needs network; see --dry-run)")
    st.add_argument("--limit", type=int, default=5, help="requests at most (default 5)")
    st.add_argument("--dry-run", action="store_true", help="list the requests it would send; send none")
    st.set_defaults(fn=cmd_step)
