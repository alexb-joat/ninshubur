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
              max_queries=args.max_queries, max_probes=args.max_probes)
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
    if rep.probes:
        print("board probes: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.probes.items())))
    for n in rep.notes:
        print(n)
    if rep.yaml:
        print("\nconfig.yaml is not changed; to read these boards, add:")
        print("\n".join(rep.yaml))
    if not args.dry_run:
        print(f"requests: {rep.requests}")
    return 0


# -- sandbox, ladder, handoff, kill switch (lane E2) ----------------------------------------------------------------
def _sandbox(app):
    from .frontier import Limits
    from .sandbox import Sandbox, limits_from
    return Sandbox(limits_from(Limits.from_policy(app.policy)))


def cmd_check(args, app) -> int:
    """Prove the parser sandbox's fences against a control (a plain child process running the same probe)."""
    from pathlib import Path

    from .sandbox import SandboxError
    sb = _sandbox(app)
    why = sb.unavailable()
    if why:
        print(f"explore check: {why}; the explorer won't parse (or fetch) until it can", file=sys.stderr)
        return 2
    home = Path.home()
    paths = [app.home, home / ".ssh", home]
    try:
        r = sb.self_test(paths)
    except SandboxError as e:
        print(f"explore check: the sandbox didn't run: {e}", file=sys.stderr)
        return 2
    show = lambda p: "~" + str(p)[len(str(home)):] if str(p).startswith(str(home)) else str(p)  # noqa: E731
    print(f"sandbox: {sb.bwrap}, python {sb.python}")
    print(f"{'':<22} {'inside the sandbox':<28} plain child (control)")
    for p in paths:
        print(f"{show(p):<22} {r['inside']['paths'][str(p)]:<28} {r['control'].get('paths', {}).get(str(p), '?')}")
    print(f"{'127.0.0.1 listener':<22} {r['inside']['loopback']:<28} {r['control'].get('loopback', '?')}")
    print(f"{'route out':<22} {r['inside']['internet']:<28} {r['control'].get('internet', '?')}")
    print(f"{'environment':<22} {', '.join(r['inside']['env']) or 'empty':<28} {len(r['control'].get('env', []))} variables")
    if not r["control_ok"]:
        print("verdict: inconclusive: the control couldn't reach the listener, so the probe proves nothing")
        return 1
    print("verdict: " + ("fenced: no home, no network, no secrets inside" if r["fenced"] else "NOT fenced; don't run the explorer"))
    return 0 if r["fenced"] else 1


def cmd_run(args, app) -> int:
    """One cycle: probe, fetch, parse in the sandbox, follow links, move sites on the trial ladder."""
    from .cycle import run_cycle
    from .gate import ExplorerGate
    conn = store.connect(app.home)
    gate = ExplorerGate(conn, app.policy, app.cfg, app.home, now=app.now)
    try:
        if gate.stopped:
            print(f"the explorer is stopped: {gate.stop_path} exists (`ninshubur explore resume` clears it)", file=sys.stderr)
            return 2
        sb = _sandbox(app)
        why = sb.unavailable()
        if why:
            print(f"explore run: {why}; nothing is fetched without the parser sandbox", file=sys.stderr)
            return 2
        if args.dry_run:
            batch = gate.frontier.next_batch(args.limit)
            for r in batch:
                probe = " (+ soft-404 probe)" if r["stage"] == "candidate" else ""
                print(f"would fetch {r['url'][:100]}{probe}")
            print(f"{len(batch)} pages planned; nothing was sent")
            return 0
        from .handoff import Writer, out_dir
        writer = Writer(app.home, gate.frontier.log, now=app.now)
        rep = asyncio.run(run_cycle(gate, sb, args.limit, on_records=writer.write))
        for x in rep.lines:
            moved = f" ({x.moved})" if x.moved else ""
            stop = f"; site stopped: {x.stopped}" if x.stopped else ""
            print(f"{x.code:<24} {x.status or '-':<4} {x.bytes:>9,} B  {x.stage}{moved}  links {x.links_queued}/{x.links_offered}"
                  f"  records {x.records}  {x.url[:80]}{stop}")
        print(f"{len(rep.lines)} pages, {rep.probes} probes, {len(rep.records)} records"
              + (f"; halted: {rep.halted}" if rep.halted else ""))
        if rep.records or writer.failed:
            print(f"handoff: {writer.written} written to {out_dir(app.home)}, {writer.refused} not written"
                  + (f" ({writer.failed})" if writer.failed else ""))
        return 2 if rep.halted else 0
    finally:
        asyncio.run(gate.aclose())
        conn.close()


def cmd_sites(args, app) -> int:
    """The trial ladder: how many sites on each rung, and the sites themselves."""
    from .frontier import Frontier
    from .ladder import Ladder
    conn = store.connect(app.home)
    try:
        fr = Frontier(conn, app.policy, app.home, now=app.now)
        c = Ladder(fr).counts()
        print("sites: " + ", ".join(f"{k} {v}" for k, v in c.items()))
        lim = fr.limits
        print(f"rungs: 1 page a day until a clean page, {lim.trial_pages_per_day} in trial, {lim.pages_per_site_day} admitted; "
              f"admitted after {lim.trial_clean_fetches} clean pages, or {lim.trial_days} days and {lim.trial_min_clean}")
        where = "WHERE status='open' AND stage=?" if args.stage and args.stage != "stopped" else (
            "WHERE status!='open'" if args.stage == "stopped" else "")
        params = [args.stage] if where.endswith("?") else []
        for r in rows(conn, f"SELECT * FROM explorer_sites {where} ORDER BY COALESCE(stage_at, first_seen) DESC LIMIT ?",
                      [*params, args.limit]):
            state = r["stage"] if r["status"] == "open" else f"stopped ({r['status_reason']})"
            age = f", trial {(app.now() - r['trial_started']) / 86400:.1f} d" if r["trial_started"] and r["stage"] == "trial" else ""
            print(f"  {r['site'][:40]:<40} {state[:70]:<12} clean {r['clean_fetches']}, pages {r['pages_total']}{age}")
        return 0
    finally:
        conn.close()


def cmd_inbox(args, app) -> int:
    """Main Ninshubur's side of the handoff: read the explorer's files now (the daemon does it every tick)."""
    from .handoff import read_inbox
    rep = read_inbox(app.conn, app.policy, app.cfg, app.home, now=app.now, dry_run=args.dry_run)
    if not rep.enabled:
        print("explorer inbox: off (config.yaml explorer.read_inbox: false); nothing read")
        return 0
    verb = "would be stored" if args.dry_run else "stored"
    print(f"{rep.lines} lines from {rep.files} files: {rep.stored + rep.updated if not args.dry_run else rep.lines - rep.bad - rep.dropped_taint} "
          f"{verb} (held, Tier 3; {rep.tainted} tainted), {rep.dropped_taint} dropped for taint, {rep.bad} refused"
          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(rep.reasons.items())) + ")" if rep.reasons else ""))
    for f in rep.skipped_files:
        print(f"  skipped {f}")
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
    r.add_argument("--max-probes", type=int, default=10, help="job-board tokens probed per run, one request each (default 10)")
    r.add_argument("--dry-run", action="store_true", help="list what would be asked; no request, nothing written")
    r.set_defaults(fn=cmd_reach)
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
    # -- sandbox, ladder, handoff, kill switch (lane E2) ------------------------------------------
    ck = xs.add_parser("check", help="prove the parser sandbox has no home, network or secrets (with a control)")
    ck.set_defaults(fn=cmd_check)
    rn = xs.add_parser("run", help="one cycle: probe, fetch, parse in the sandbox, follow links, move sites on the ladder")
    rn.add_argument("--limit", type=int, default=10, help="pages at most, one per site (default 10)")
    rn.add_argument("--dry-run", action="store_true", help="list the pages it would fetch; send nothing")
    rn.set_defaults(fn=cmd_run)
    si = xs.add_parser("sites", help="the trial ladder: sites per rung, and each site's rung, clean pages and trial age")
    si.add_argument("--stage", choices=("candidate", "probed", "trial", "admitted", "stopped"))
    si.add_argument("--limit", type=int, default=20, help="sites to list (default 20)")
    si.set_defaults(fn=cmd_sites)
    ib = xs.add_parser("inbox", help="main Ninshubur reads the explorer's handoff files now (held, Tier 3)")
    ib.add_argument("--dry-run", action="store_true", help="check and count the lines; store nothing")
    ib.set_defaults(fn=cmd_inbox)
