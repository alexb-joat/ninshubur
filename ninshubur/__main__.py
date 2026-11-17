"""ninshubur command line."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import __version__
from .adapters.base import all_adapters, get_adapter, run_projection
from .analyze import qualify as qualifier
from .analyze.suggest import Suggester
from .companies import ALIASES_FILE, companies as company_list, dupes as company_dupes, evidence_items
from .config import ConfigError, SourceConfig, load_config
from .db import connect, j, ledger_sum, one, rows, uj
from .envvars import load_dotenv
from .export import export as export_files, parse_since, prospects_for_event, weekly_digests
from .export_facts import export_provider_facts
from .feedback import Feedback, FeedbackError
from .fetch import Fetcher
from .home import data_home, ensure_home
from .ingest import Ingestor
from .labelsheet import LabelSheetError, meta_path as label_meta_path, write as write_label_sheet
from .llm import LLMClient, LLMError
from .logs import write_private
from .policy import Policy, PolicyError, host_of
from .profiles import PROFILE_KEYS, ProfileError, load_profiles
from .schedule import Scheduler, health_label, status_summary
from .schemas import SchemaError, check_registry
from .sinks.ics import IcsSink
from .termsafe import guarded_output


class App:
    """Everything a command needs, built once. Fails closed on policy, config or schema errors."""

    def __init__(self, *, transport=None, verbose: bool = False):
        load_dotenv()
        self.home = ensure_home(data_home())
        self.policy = Policy.load(self.home)
        self.cfg = load_config(self.home, policy=self.policy)
        self.profiles = load_profiles(self.home)
        check_registry()
        self.conn = connect(self.home)
        self.now = time.time
        # One clock for every component, so a test (or a replay) can pin it in one place.
        self.fetcher = Fetcher(self.conn, self.policy, self.cfg, self.home, transport=transport, now=self.now)
        self.ingestor = Ingestor(self.conn, self.policy, self.cfg, self.home, now=self.now)
        self.scheduler = Scheduler(self.conn, self.policy, self.cfg, self.home, self.fetcher, self.ingestor, now=self.now,
                                   rng=random.Random())
        self.suggester = Suggester(self.conn, self.policy, self.cfg, self.profiles, now=self.now, home=self.home)
        self.sinks = [IcsSink(self.conn, self.home, str(self.cfg.get("sinks", "ics", "path", default="feeds/timings.ics")),
                              now=self.now)]
        self.feedback = Feedback(self.conn, self.sinks, now=self.now)
        tz_name = str(self.cfg.get("timezone", default="UTC"))
        try:
            self.tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            print(f"warning: time zone {tz_name!r} not found (on Windows: pip install tzdata); using UTC", file=sys.stderr)
            self.tz = ZoneInfo("UTC")
        self.verbose = verbose

    def local(self, ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d %H:%M")

    def day(self, ts: float | None) -> str:
        return "-" if not ts else datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d")


# --- commands --------------------------------------------------------------------------------
def cmd_check(args, app: App) -> int:
    print(f"ninshubur {__version__}")
    print(f"home      {app.home}")
    print(f"config    {app.cfg.path or '(defaults; no config.yaml found)'}")
    print(f"policy    shipped + {'user tightening' if (app.home / 'policy.yaml').is_file() else 'no user policy'}")
    for n in app.policy.notes:
        print(f"  note: {n}")
    print(f"schemas   {len(check_registry())} loaded")
    print(f"adapters  {', '.join(sorted(all_adapters()))}")
    print(f"profiles  {', '.join(app.profiles) or '(none; add profiles.yaml for suggestions)'}")
    now = app.now()
    bad = 0
    for src in app.cfg.sources.values():
        try:
            spec = get_adapter(src.adapter).spec
        except KeyError as e:
            print(f"  source {src.name}: {e}")
            bad += 1
            continue
        due = src.review_due_at()
        flag = "REVIEW OVERDUE" if due is not None and due <= now else f"review due {app.day(due)}"
        if src.schema_id not in spec.schemas and src.schema_id != spec.schema:
            print(f"  source {src.name}: schema {src.schema_id} is not one of {spec.schemas}")
            bad += 1
        print(f"  source {src.name:<20} {src.adapter:<12} tier {src.tier}  {flag}")
    if app.suggester.aliases_error:
        print(f"  aliases: {app.suggester.aliases_error}")
        bad += 1
    print("llm       local endpoint configured; Tier B/C off in this phase")
    print("ok" if bad == 0 else f"{bad} problem(s)")
    return 0 if bad == 0 else 1


def cmd_run(args, app: App) -> int:
    if args.replay:
        from .replay import replay_resolver, replay_transport
        app.fetcher = Fetcher(app.conn, app.policy, app.cfg, app.home, transport=replay_transport(Path(args.replay)),
                              resolve=replay_resolver)
        app.scheduler.fetcher = app.fetcher
    if args.daemon:
        def after_tick() -> None:
            app.suggester.build_all()
            for sink in app.sinks:
                sink.write_all()
            for path in weekly_digests(app.conn, app.suggester, app.home, list(app.profiles), app.now(), app.tz):
                print(f"weekly digest written: {path}")
        app.scheduler.after_tick = after_tick
        print(f"daemon: tick every {app.cfg.get('scheduler', 'tick_s')}s; Ctrl-C to stop")
        try:
            asyncio.run(app.scheduler.daemon())
        except KeyboardInterrupt:
            pass
        return 0
    try:
        reports = asyncio.run(app.scheduler.run_once(force=args.force, only=args.source))
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    if not reports:
        print("nothing due (use --force to run anyway)")
    for r in reports:
        print(f"{r.source:<20} fetched {r.fetched} changed {r.changed} unchanged {r.unchanged} refused {r.refused} errors {r.errors} "
              f"| stored {r.stored} dropped {r.dropped} held {r.held} | health {r.health} | next {app.local(r.next_due_at)}"
              + (f" | {r.note}" if r.note else ""))
    built = app.suggester.build_all()
    for s in app.sinks:
        s.write_all()
    rg = app.suggester.last_regroup
    print(f"suggestions: {len(built)} built; `ninshubur suggest` to review"
          + (f"; {rg['expire']} per-posting hiring suggestion(s) expired, now one per company" if rg.get("expire") else ""))
    return 0


def _badges(s: dict) -> str:
    tier = max((e.get("tier", 3) for e in s["evidence"]), default=3)  # the weakest evidence sets the badge
    b = [f"T{tier}"]
    if tier >= 2:
        b.append("unverified")
    if s["status"] == "held":
        b.append(f"held:{s.get('held_reason')}")
    return " ".join(b)


def _print_suggestion(app: App, s: dict, full: bool = False) -> None:
    print(f"#{s['id']} [{s['kind']}] {s['title']}  ({_badges(s)})")
    print(f"    value {s['est_value']:.0f} USD  cost {s['est_cost']:.0f} USD  roi {s['roi']:.1f}  confidence {s['confidence']:.2f}"
          f"  profile {s.get('profile') or '-'}")
    win = ""
    if s.get("best_window_start"):
        win = f" (best: {app.day(s['best_window_start'])} to {app.day(s['best_window_end'])}, {app.tz.key})"
    print(f"    due {app.day(s.get('deadline'))}{win}  expires {app.day(s['expires_at'])}")
    for r in s["reasons"][: None if full else 2]:
        print(f"    - {r}")
    if full:
        print("    steps:")
        for i, st in enumerate(s["steps"], 1):
            print(f"      {i}. {st}")
        print("    evidence:")
        for e in s["evidence"]:
            vals = ", ".join(f"{k}={v}" for k, v in e.get("values", {}).items())
            print(f"      {e.get('url')}  [{e.get('source')} T{e.get('tier')}] fetched {app.local(e.get('fetched_at'))}  {vals}")
        print(f"    status {s['status']}  created {app.local(s['created_at'])}")
        if s["kind"] == "conference_prospecting" and s["entity_keys"]:
            pr = prospects_for_event(app.conn, s["entity_keys"][0][6:])
            print(f"    prospects ({len(pr)}, unverified; verify role and company before contact):")
            for r in pr:
                flag = " [page held]" if r.get("page_held") else ""
                print(f"      {r['role']:<9} {r['name'][:32]:<32} {(r.get('title') or '')[:28]:<28} {(r.get('company') or r.get('domain') or '')[:28]:<28} "
                      f"{(r.get('handle') or '')[:44]:<44} {r['source_url'][:50]}{flag}")
            print("    full list: ninshubur export prospects --event " + s["entity_keys"][0][6:])


def _minutes(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 60)
    return int(s.rstrip("m"))


def cmd_suggest(args, app: App) -> int:
    if args.regroup_dry_run:
        rg = app.suggester.regroup(dry_run=True)
        total = sum(rg["new_rows"].values())
        print(f"dry run, nothing written: {rg['live_per_posting']} live per-posting hiring suggestion(s)")
        print(f"  {rg['expire']} of them belong to {rg['companies']} company key(s) and expire on the next `ninshubur run`")
        print(f"  {total} per-company suggestion row(s) will be created"
              + (": " + ", ".join(f"{p} {n}" for p, n in rg["new_rows"].items()) if rg["new_rows"] else ""))
        print(f"  {rg['moved']} live per-company suggestion(s) whose postings moved to another key (an alias) expire")
        print(f"  {rg['past_expiry']} are past their own expiry date and expire on the next run anyway")
        print(f"  {rg['stay_live']} cover no posting that still qualifies; they stay live until their own expiry date")
        print(f"  {rg['decided_untouched']} per-posting suggestion(s) a person accepted, dismissed or marked done stay untouched")
        return 0
    status = ("held",) if args.held else ("new",)
    kw = {"status": status, "profile": args.profile, "kind": args.kind}
    items = app.suggester.plan(_minutes(args.plan), **kw) if args.plan else app.suggester.list(top=args.top, **kw)
    if args.json:
        print(json.dumps(items, indent=2, default=str))
        return 0
    if not items:
        print("no suggestions" + (" held" if args.held else "") + "; run `ninshubur run` first")
        return 0
    if args.plan:
        mins = sum(i["cost_parts"].get("human_minutes", 0) for i in items)
        print(f"plan for {_minutes(args.plan)} min: {len(items)} suggestion(s), {mins:.0f} min, "
              f"{sum(i['est_value'] for i in items):.0f} USD expected value")
    for s in items:
        _print_suggestion(app, s)
    return 0


def cmd_show(args, app: App) -> int:
    s = app.suggester.get(args.id)
    if not s:
        print(f"no suggestion {args.id}", file=sys.stderr)
        return 2
    _print_suggestion(app, s, full=True)
    return 0


def cmd_feedback(args, app: App) -> int:
    try:
        if args.cmd == "accept":
            s, pushed = app.feedback.accept(args.id)
            print(f"#{s['id']} accepted" + (f"; pushed to {', '.join(f'{n} ({e})' for n, e in pushed)}" if pushed else ""))
        elif args.cmd == "dismiss":
            s = app.feedback.dismiss(args.id, args.reason)
            print(f"#{s['id']} dismissed")
        else:
            s, _ = app.feedback.done(args.id, args.outcome, args.minutes)
            print(f"#{s['id']} done ({args.outcome})")
    except FeedbackError as e:
        print(e, file=sys.stderr)
        return 2
    for sink in app.sinks:
        sink.write_all()
    return 0


def _resolve_event(app: App, ref: str) -> dict | None:
    """An event item by id, uid or title fragment, or the event behind a #suggestion id."""
    if ref.startswith("#"):
        sg = app.suggester.get(int(ref[1:]))
        if not sg or sg["kind"] != "conference_prospecting":
            return None
        ref = sg["entity_keys"][0][6:]
    r = one(app.conn, "SELECT id, uid, url, title FROM items WHERE kind='event' AND (uid=? OR CAST(id AS TEXT)=?)", (ref, ref))
    if r is None:
        r = one(app.conn, "SELECT id, uid, url, title FROM items WHERE kind='event' AND title LIKE ? ORDER BY published_at DESC LIMIT 1", (f"%{ref}%",))
    return r


def cmd_lookup(args, app: App) -> int:
    srcs = [s for s in app.cfg.sources.values() if s.adapter == "event_sites" and s.enabled]
    if not srcs:
        print("no admitted event_sites source in config.yaml (see config.example.yaml)", file=sys.stderr)
        return 2
    ev = _resolve_event(app, args.event)
    if not ev or not ev["url"]:
        print(f"no event matching {args.event!r} with a URL", file=sys.stderr)
        return 2
    if args.replay:
        from .replay import replay_resolver, replay_transport
        app.fetcher = Fetcher(app.conn, app.policy, app.cfg, app.home, transport=replay_transport(Path(args.replay)),
                              resolve=replay_resolver)
        app.scheduler.fetcher = app.fetcher
    print(f"{'dry run: ' if args.dry_run else ''}{ev['title']} ({ev['url']})")
    report, collected = asyncio.run(app.scheduler.crawl_event(srcs[0].name, dict(ev), dry_run=args.dry_run))
    print(f"fetched {report.fetched} refused {report.refused} errors {report.errors} | stored {report.stored} dropped {report.dropped} held {report.held} | {report.note}".rstrip(" |"))
    for plan, f, parsed in collected:
        it = parsed.items[0] if parsed.items else None
        a = it.attrs if it else {}
        print(f"  {f.final_url or plan.url}")
        print(f"    {a.get('persons', 0)} people, {a.get('orgs', 0)} organisations via {a.get('layer') or 'sections'}; "
              f"{len(parsed.dropped_rows)} hostile rows dropped; contact data seen: {a.get('contact_data', 0)}; "
              f"follow-ups: {len(parsed.followups)}; candidates: {len(parsed.candidates)}"
              + (f"; findings: {', '.join(x.code for x in parsed.findings)}" if parsed.findings else ""))
        if args.dry_run and it:
            for e in it.entities[:40]:
                print(f"      {e.role or e.type:<9} {e.name[:32]:<32} {(e.title or '')[:28]:<28} {(e.company or e.domain or '')[:28]:<28} {e.handle or ''}")
            if len(it.entities) > 40:
                print(f"      ... {len(it.entities) - 40} more")
    if not args.dry_run:
        app.suggester.build_all()
        print("suggestions rebuilt; `ninshubur suggest --kind conference_prospecting`")
    return 0


def cmd_status(args, app: App) -> int:
    app.scheduler.sync_sources()
    s = status_summary(app.conn, app.now())
    print("sources: " + (", ".join(f"{k} {v}" for k, v in sorted(s["by_health"].items())) or "none"))
    for src in s["sources"]:
        due = (src["reviewed_at"] or 0) + (src["review_every_days"] or 0) * 86400
        reason = f"  ({src['health_reason']})" if src["health_reason"] and src["health"] != "ok" else ""
        print(f"  {src['name']:<20} {src['adapter']:<12} T{src['tier']} rep {src['reputation']:.2f} {health_label(src):<20} "
              f"next {app.local(src['next_due_at']):<16} review {app.day(due)}{reason}")
    print(f"items held {s['items_held']}, tainted {s['items_tainted']}; quarantine rows (7 d) {s['quarantine_7d']}; "
          f"new candidates {s['candidates_new']}")
    print("suggestions: " + (", ".join(f"{k} {v}" for k, v in sorted(s["suggestions"].items())) or "none"))
    return 0


def cmd_budget(args, app: App) -> int:
    day = app.now() - 86400
    print(f"bytes today      {ledger_sum(app.conn, 'bytes', day):,.0f} / {app.cfg.get('budgets', 'daily_bytes'):,}")
    print(f"human minutes    {ledger_sum(app.conn, 'human_minutes', day):.0f} / {app.cfg.get('budgets', 'daily_human_minutes')}")
    print(f"llm usd          {ledger_sum(app.conn, 'llm_usd', day):.4f} / {app.cfg.get('llm', 'caps', 'daily_usd')}")
    print(f"llm tokens       {ledger_sum(app.conn, 'llm_tokens', day):.0f} / {app.cfg.get('llm', 'caps', 'daily_tokens')}")
    print("per source (bytes today, parse ok/fail, taint events):")
    for r in rows(app.conn, "SELECT s.source, s.parse_ok, s.parse_fail, s.taint_events, COALESCE((SELECT SUM(amount) FROM budget_ledger b "
                            "WHERE b.kind='bytes' AND b.ref=s.source AND b.ts>=?),0) AS bytes FROM source_stats s ORDER BY s.source", (day,)):
        print(f"  {r['source']:<20} {r['bytes']:>12,.0f}  {r['parse_ok']}/{r['parse_fail']}  {r['taint_events']}")
    return 0


# --- sources ---------------------------------------------------------------------------------
def cmd_sources_list(args, app: App) -> int:
    app.scheduler.sync_sources()
    for src in app.cfg.sources.values():
        row = app.ingestor.source(src.name) or {}
        print(f"{src.name:<20} {src.adapter:<12} T{src.tier} rep {row.get('reputation', 0):.2f} "
              f"{health_label(row) if row else '?':<20} "
              f"owner {src.trust.get('owner')!s:<30} licence {src.trust.get('licence')}  review {app.day(src.review_due_at())}")
    return 0


def _test_source(app: App, src: SourceConfig) -> tuple[list, dict]:
    adapter = get_adapter(src.adapter)
    spec = adapter.spec
    plans = adapter.plan(src, {}, app.now())
    test: dict = {"urls": [p.url for p in plans], "schema_known": True}
    if not plans:
        test.update(status=None, size=None, field_validity=None)
        return app.policy.admission_check(src.trust, spec, test, now=app.now()), test
    plan = plans[0]
    f = asyncio.run(app.fetcher.fetch(plan, src, spec))
    # A probe must not count as a fetch: otherwise the first real run sees the page as unchanged (ETag /
    # content hash) and never parses it. Forget the state the probe recorded.
    app.conn.execute("DELETE FROM fetch_state WHERE key=?", (plan.key,))
    app.conn.commit()
    codes = [x.code for x in f.findings]
    test.update(status=f.status, size=len(f.body), tls_ok="cert_error" not in codes, off_host_redirect="off_host_redirect" in codes,
                content_type=f.headers.get("content-type"),
                robots_allowed=f.error not in ("refused:robots_disallow", "refused:robots_unreachable"),
                unauthenticated=spec.needs_key_env is None, max_bytes=plan.max_bytes, error=f.error, codes=codes)
    robots = app.fetcher.robots.cached(host_of(plan.url))
    if (robots and robots["status"] in (401, 403) and robots["status"] == src.robots_reviewed_status
            and host_of(plan.url) in spec.hosts):
        test["robots_exception"] = robots["status"]
    if f.status == 200 and not f.findings:
        # Parse exactly as a run would (the adapter's own parser, then its schema), so H4 measures the real
        # extraction rather than a generic JSON projection that non-JSON sources cannot pass.
        parsed = adapter.parse(src, f, app.now())
        proj = parsed.projection or run_projection(f, plan.schema or src.schema_id or spec.schema, app.now(), spec=spec)
        test["field_validity"] = round(proj.field_validity, 3)
        test["rows"] = len(proj.rows)
        test["doc_findings"] = [x.code for x in proj.findings]
        if parsed.notes:
            test["notes"] = list(parsed.notes)
    else:
        test["field_validity"] = None
    return app.policy.admission_check(src.trust, spec, test, now=app.now()), test


def cmd_sources_test(args, app: App) -> int:
    src = app.cfg.sources.get(args.name)
    if src is None:
        print(f"unknown source {args.name!r} (add it to config.yaml first, or use `sources add`)", file=sys.stderr)
        return 2
    checks, test = _test_source(app, src)
    for c in checks:
        print(f"  {c.criterion} {'PASS' if c.ok else 'FAIL'}  {c.detail}")
    if app.verbose:
        print(json.dumps(test, indent=2, default=str))
    ok = Policy.admitted(checks)
    print("admitted" if ok else "REFUSED: fix the failing hard criteria before enabling this source")
    return 0 if ok else 1


def cmd_sources_add(args, app: App) -> int:
    params: dict = {}
    for kv in args.param or []:
        k, _, v = kv.partition("=")
        try:
            params[k] = yaml.safe_load(v)
        except yaml.YAMLError:
            params[k] = v
    if args.from_candidate:
        c = one(app.conn, "SELECT * FROM candidates WHERE id=?", (args.from_candidate,))
        if not c:
            print(f"no candidate {args.from_candidate}", file=sys.stderr)
            return 2
        params.setdefault("host", c["host"])
    try:
        spec = get_adapter(args.adapter).spec
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    tier = int(args.tier)
    trust = {"tier": tier, "owner": args.owner, "owner_url": args.owner_url, "terms_url": args.terms_url, "licence": args.licence,
             "legal_basis": args.legal_basis, "rate_limit": args.rate_limit,
             "expected_content_type": args.content_type or spec.expected_content_type,
             "schema": args.schema or spec.schema, "reviewed_by": args.reviewed_by, "reviewed_at": time.strftime("%Y-%m-%d"),
             "review_every_days": args.review_every_days or int(app.policy.tier(tier)["review_every_days"])}
    if args.robots_reviewed_status is not None:
        trust["robots_reviewed_status"] = args.robots_reviewed_status
    try:
        from .config import _source
        src = _source(args.name, {"adapter": args.adapter, "params": params, "trust": trust}, app.policy, [])
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    checks, _ = _test_source(app, src)
    for c in checks:
        print(f"  {c.criterion} {'PASS' if c.ok else 'FAIL'}  {c.detail}")
    if not Policy.admitted(checks):
        print("REFUSED: not written to config", file=sys.stderr)
        return 1
    path = app.cfg.path or (app.home / "config.yaml")
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}) or {}
    data.setdefault("sources", {})[args.name] = {"adapter": args.adapter, "params": params, "trust": trust}
    write_private(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    if args.from_candidate:
        app.conn.execute("UPDATE candidates SET status='admitted' WHERE id=?", (args.from_candidate,))
    print(f"admitted: {args.name} written to {path}")
    return 0


def cmd_sources_review(args, app: App) -> int:
    row = app.ingestor.source(args.name)
    if row is None:
        print(f"unknown source {args.name!r}", file=sys.stderr)
        return 2
    print(f"{args.name}: health {row['health']} ({row['health_reason'] or '-'}), reputation {row['reputation']:.2f}, "
          f"taint events 24h {app.ingestor.taint_events_24h(args.name)}")
    for q in rows(app.conn, "SELECT ts, severity, detector_codes, url_hash, excerpt_hash, action FROM quarantine WHERE source=? "
                            "ORDER BY ts DESC LIMIT 20", (args.name,)):
        print(f"  {app.local(q['ts'])}  {q['severity']:<9} {q['action']:<18} {q['detector_codes']}  "
              f"url#{q['url_hash'] or '-'} excerpt#{q['excerpt_hash'] or '-'}")
    state = uj(row["state"])
    flagged = {uid: st for uid, st in (state.get("per_event") or {}).items() if st.get("flagged") or int(st.get("fails", 0)) >= 3}
    if flagged:
        print(f"  per-event flags ({len(flagged)}):")
        for uid, st in list(flagged.items())[:30]:
            ev = one(app.conn, "SELECT title FROM items WHERE uid=?", (uid,))
            print(f"    {uid}  {(ev or {}).get('title', '?')[:50]:<50} fails {st.get('fails', 0)} {','.join(st.get('flagged', []))}")
    if args.clear_event:
        pe = state.setdefault("per_event", {})
        if args.clear_event in pe:
            pe[args.clear_event] = {"fails": 0, "pages": int(pe[args.clear_event].get("pages", 0))}
            app.conn.execute("UPDATE sources SET state=? WHERE name=?", (j(state), args.name))
            print(f"cleared event {args.clear_event}")
        else:
            print(f"no per-event state for {args.clear_event}", file=sys.stderr)
    if args.clear:
        row = app.ingestor.clear(args.name)
        print(f"cleared: health {row['health']}, reputation {row['reputation']:.2f}")
    return 0


def cmd_sources_candidates(args, app: App) -> int:
    if args.reject or args.reject_host:
        if args.reject:
            n = app.conn.execute("UPDATE candidates SET status='rejected' WHERE id=? AND status='new'", (args.reject,)).rowcount
        else:
            n = app.conn.execute("UPDATE candidates SET status='rejected' WHERE status='new' AND (host=? OR host LIKE ?)",
                                 (args.reject_host.lower(), "%." + args.reject_host.lower())).rowcount
        print(f"rejected {n} candidate(s)")
        return 0
    cs = rows(app.conn, "SELECT * FROM candidates WHERE status='new' ORDER BY count DESC, last_seen DESC LIMIT ?", (args.top,))
    if not cs:
        print("no new candidates")
    for c in cs:
        print(f"#{c['id']:<4} {c['host']:<40} x{c['count']:<4} via {c['found_in_source']:<16} {c['reason']}  e.g. {c['example_url']}")
    print("candidates are never fetched; admit one with `sources add NAME --adapter A --from-candidate ID ...`")
    return 0


def cmd_sources_toggle(args, app: App) -> int:
    path = app.cfg.path
    if not path:
        print("no config.yaml to edit", file=sys.stderr)
        return 2
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if args.name not in (data.get("sources") or {}):
        print(f"unknown source {args.name!r}", file=sys.stderr)
        return 2
    enable = args.cmd == "enable"
    data["sources"][args.name]["enabled"] = enable
    write_private(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    app.ingestor.set_enabled(args.name, enable, f"{args.cmd}d by operator")
    row = app.ingestor.source(args.name)
    held = row and row["health"] not in ("ok", "suspect", "disabled")
    until = "its trust block's reviewed_at is renewed" if held and row["health"] == "review_overdue" \
        else f"`ninshubur sources review {args.name} --clear`"
    print(f"{args.name} {args.cmd}d" + (f"; still {row['health']} ({row['health_reason'] or '-'})" if held else "")
          + (f": it fetches nothing until {until}" if held and enable else ""))
    return 0


def cmd_profiles(args, app: App) -> int:
    if not app.profiles:
        print("no profiles (create profiles.yaml in the data dir; see profiles.example.yaml)")
        return 1
    for p in app.profiles.values():
        print(f"{p.name}: {len(p.keywords)} keywords, {len(p.pains)} pains, cities {', '.join(p.cities) or '-'}, "
              f"home {p.home_base or '-'}/{p.home_country or '-'}, targets {len(p.target_companies)}, tools {len(p.tools_in_use)}, "
              f"thresholds {p.thresholds}")
        if args.pcmd == "check":
            for kind, why in p.builders_available().items():
                print(f"    {kind:<24} {why}")
            q = p.qualify
            print(f"    {'qualify':<24} " + (f"ok: one {q.subject} at a time, answers {'/'.join(q.labels)} (positive: {q.positive}), "
                                             f"{len(q.must)} must, {len(q.must_not)} must_not" if q
                                             else "not set (add a qualify: block with a question to use `ninshubur qualify`)"))
            if p.unknown_keys:
                print(f"    warning: unknown key(s) ignored: {', '.join(p.unknown_keys)} (known: {', '.join(PROFILE_KEYS)})")
            if not p.home_country and p.countries:
                print("    warning: home_country unset; every trip outside the home city is costed as abroad")
            kinds = {get_adapter(src.adapter).spec.kind for src in app.cfg.sources.values() if src.enabled}
            print(f"    admitted source kinds: {', '.join(sorted(kinds)) or 'none'}")
    return 0


def cmd_companies(args, app: App) -> int:
    resolver = app.suggester.refresh_resolver()
    found = company_list(app.conn, resolver)
    if app.suggester.aliases_error:
        print(f"warning: {app.suggester.aliases_error} (the last good aliases are in use)")
    for w in resolver.warnings:
        print(f"note: {w}", file=sys.stderr)
    if args.dupes:
        pairs = company_dupes(found)
        if args.json:
            print(json.dumps([{"a": {"key": d.a.key, "name": d.a.name}, "b": {"key": d.b.key, "name": d.b.name},
                               "distance": d.distance, "why": d.why, "alias_line": d.alias_line, "hint": d.hint}
                              for d in pairs], indent=2, ensure_ascii=False))
            return 0
        if not pairs:
            print(f"no near-miss names among {len(found)} companies")
            return 0
        print(f"{len(pairs)} near-miss pair(s) among {len(found)} companies; nothing is merged until you say so")
        for d in pairs:
            print(f"  {d.a.name} ({d.a.key}, {d.a.postings})  ~  {d.b.name} ({d.b.key}, {d.b.postings})  [{d.why}]")
            print(f"    {d.hint}:" + (f"  {d.alias_line}" if d.alias_line else ""))
        print(f"aliases file: {app.home / ALIASES_FILE}")
        return 0
    if args.json:
        print(json.dumps([c.as_dict() for c in found], indent=2, ensure_ascii=False))
        return 0
    if not found:
        print("no companies yet: they come from hiring items (run `ninshubur run` with a greenhouse or hn source)")
        return 0
    width = min(40, max(len(c.key) for c in found))
    print(f"{'KEY':<{width}}  {'NAME':<28} {'POSTINGS':>8} {'HELD':>4}  {'FIRST':<10} {'LAST':<10}  SOURCES")
    for c in found:
        print(f"{c.key[:width]:<{width}}  {c.name[:28]:<28} {c.postings:>8} {c.held:>4}  {app.day(c.first_at):<10} "
              f"{app.day(c.last_at):<10}  {', '.join(c.sources)}")
    print(f"{len(found)} companies, {sum(c.postings for c in found)} postings; `ninshubur companies --dupes` lists near-miss names")
    return 0


def qualify_subjects(app: App, profile) -> list:
    """The subjects a profile's question is asked about: every company with usable evidence."""
    resolver = app.suggester.refresh_resolver()
    names = {c.key: c.name for c in company_list(app.conn, resolver)}
    return qualifier.build_subjects(evidence_items(app.conn, resolver), names, profile)


def _money(usd: float) -> str:
    return f"${usd:.2f}" if usd >= 0.995 or usd == 0 else f"${usd:.4f}"


def _print_qualification(r: dict) -> None:
    cited = ",".join(str(n) for n in r["cited"]) or "-"
    head = r["label"] if r["status"] == "ok" else f"failed ({r['error']})"
    note = f"  [{r['error']}]" if r["status"] == "ok" and r["error"] else ""
    print(f"  {head:<22} {r['name'][:32]:<32} lines {cited:<10} {r['reason'] or ''}{note}")


def cmd_qualify(args, app: App) -> int:
    profile = app.profiles.get(args.profile)
    if profile is None:
        print(f"unknown profile {args.profile!r}; known: {', '.join(app.profiles) or 'none'}", file=sys.stderr)
        return 2
    if profile.qualify is None:
        print(f"profile {profile.name} has no qualify: block; add one with a question (see profiles.example.yaml)", file=sys.stderr)
        return 2
    if args.show:
        found = qualifier.latest(app.conn, profile.name, args.model)
        if args.json:
            print(json.dumps(found, indent=2, ensure_ascii=False))
            return 0
        if not found:
            print(f"no stored answers for profile {profile.name} yet; `ninshubur qualify --profile {profile.name} --dry-run` shows what a run would ask")
            return 0
        for r in found:
            _print_qualification({**r, "name": f"{r['subject_name']} ({r['model']})"})
        counts: dict[str, int] = {}
        for r in found:
            k = r["label"] if r["status"] == "ok" else "failed"
            counts[k] = counts.get(k, 0) + 1
        print(f"{len(found)} stored answer(s): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0

    subjects = qualify_subjects(app, profile)
    try:
        plan = qualifier.plan(app.conn, app.cfg, profile, subjects, slot_name=args.slot, model=args.model,
                              again=args.again, limit=args.limit)
    except LLMError as e:
        print(f"cannot qualify: {e.message} (slot {args.slot}; models and prices are set under llm: in config.yaml)", file=sys.stderr)
        return 2
    q = profile.qualify
    shown = sum(len(s.lines) for s in subjects)
    print(f"profile {profile.name}: {len(subjects)} subject(s) ({q.subject}) with evidence, {shown} line(s) shown of "
          f"{sum(s.total for s in subjects)}, {sum(s.redactions for s in subjects)} contact detail(s) redacted")
    where = "this machine" if plan.slot.local else f"remote, {plan.slot.base_url}"
    print(f"model {plan.slot.model} on slot {plan.slot.name} ({where}); answers: {'/'.join(q.labels)}")
    print(f"to ask: {len(plan.todo)}; unchanged since their last good answer: {plan.unchanged}"
          + (f"; --limit {args.limit}" if args.limit is not None else ""))
    cost = "no charge (local model)" if plan.slot.local else (
        f"about {_money(plan.usd)}, at most about {_money(plan.worst_usd)} if every answer needs its one retry")
    print(f"estimate: about {plan.input_tokens} input tokens, up to {plan.output_tokens} output tokens; {cost}")
    if args.dry_run:
        print("dry run: nothing was sent, nothing was stored")
        return 0
    if not plan.todo:
        print("nothing to ask (use --again to ask anyway)")
        return 0
    if not plan.slot.local and args.max_usd is None:
        print("a remote model runs only under a cap: add --max-usd AMOUNT (the estimate is above)", file=sys.stderr)
        return 2

    def show(r: dict) -> None:
        _print_qualification({**r, "name": r["subject"].name})

    async def go():
        async with LLMClient(app.conn, app.policy, app.cfg, app.home, now=app.now) as client:
            return await qualifier.run(app.conn, client, profile, plan, max_usd=args.max_usd, now=app.now,
                                       on_result=None if args.json else show)

    try:
        rep = asyncio.run(go())
    except LLMError as e:
        print(f"stopped: {e.message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"run_id": rep.run_id, "status": rep.status, "asked": rep.asked, "answered": rep.answered,
                          "failed": rep.failed, "unchanged": rep.unchanged, "usd": rep.usd, "input_tokens": rep.input_tokens,
                          "output_tokens": rep.output_tokens, "labels": dict(rep.labels), "errors": dict(rep.errors)}, indent=2))
    else:
        labels = ", ".join(f"{k} {v}" for k, v in sorted(rep.labels.items())) or "none"
        failed = f"; failed {rep.failed} ({', '.join(f'{k} {v}' for k, v in sorted(rep.errors.items()))})" if rep.failed else ""
        print(f"run {rep.run_id} {rep.status}: asked {rep.asked} of {len(plan.todo)}, answered {rep.answered} ({labels}){failed}; "
              f"{rep.input_tokens} + {rep.output_tokens} tokens, {_money(rep.usd)}")
        print(f"stored in table qualifications; `ninshubur qualify --profile {profile.name} --show` lists the answers")
    if rep.status != "done":
        print(f"the run stopped early ({rep.status.split(':', 1)[1]}); what was answered is kept, and the next run asks the rest",
              file=sys.stderr)
        return 1
    return 0


def cmd_export(args, app: App) -> int:
    if args.what == "euboulia-facts":
        path = export_provider_facts(app.conn, app.home, app.now(), out=Path(args.output) if args.output else None)
        print(f"written {path}")
        return 0
    statuses = tuple(x.strip() for x in args.status.split(",") if x.strip())
    if args.profile and args.profile not in app.profiles:
        print(f"unknown profile {args.profile!r}; known: {', '.join(app.profiles) or 'none'}", file=sys.stderr)
        return 2
    if args.what == "label-sheet":
        if not args.profile:
            print("label-sheet needs --profile (the sheet is drawn for one profile's question)", file=sys.stderr)
            return 2
        profile = app.profiles[args.profile]
        try:
            path, sheet = write_label_sheet(qualify_subjects(app, profile) if profile.qualify else [], profile, app.home,
                                            sample=args.sample, seed=args.seed, now=app.now(),
                                            out=Path(args.output) if args.output else None, force=args.force)
        except LabelSheetError as e:
            print(e, file=sys.stderr)
            return 2
        q = profile.qualify
        print(f"written {path}")
        print(f"  {len(sheet.rows)} row(s) drawn from {sheet.population} with seed {sheet.seed}, over {len(sheet.strata)} strata "
              f"(counts in {label_meta_path(path).name}); no model output is in the sheet")
        if len(sheet.rows) < sheet.asked:
            print(f"  note: {sheet.asked} were asked for, but only {sheet.population} have evidence")
        print(f"  question: {q.question}")
        print(f"  fill the label column with one of: {', '.join(q.labels)}; note is free text; leave the other columns as they are")
        return 0
    try:
        fmt = "csv" if args.what in ("items", "prospects") and args.format == "md" else args.format
        path = export_files(app.conn, app.suggester, app.home, what=args.what, fmt=fmt, profile=args.profile,
                            statuses=statuses, since=parse_since(args.since, app.now()), now=app.now(), tz=app.tz,
                            out=Path(args.output) if args.output else None, event=args.event)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"written {path}")
    if args.format == "html":
        print("open it in a browser and use Print to PDF for a PDF copy")
    return 0


# --- parser ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ninshubur", description="Policy-first intelligence crawler")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="load config, policy and schemas; report sources").set_defaults(fn=cmd_check)
    r = sub.add_parser("run", help="fetch due sources once (or --daemon) and rebuild suggestions")
    r.add_argument("--once", action="store_true", help="(default)")
    r.add_argument("--daemon", action="store_true")
    r.add_argument("--force", action="store_true", help="ignore schedules")
    r.add_argument("--source", help="only this source")
    r.add_argument("--replay", metavar="DIR", help="serve samples from DIR instead of the network")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("sources", help="trust registry")
    ss = s.add_subparsers(dest="scmd", required=True)
    ss.add_parser("list").set_defaults(fn=cmd_sources_list)
    t = ss.add_parser("test", help="run the admission checklist (H1-H8) against a configured source")
    t.add_argument("name")
    t.set_defaults(fn=cmd_sources_test)
    a = ss.add_parser("add", help="admit a new source: the checklist must pass before it is written to config")
    a.add_argument("name")
    a.add_argument("--adapter", required=True)
    a.add_argument("--from-candidate", type=int)
    a.add_argument("--param", action="append", help="k=v (repeatable)")
    a.add_argument("--tier", required=True, type=int, choices=[0, 1, 2, 3])
    a.add_argument("--owner", required=True)
    a.add_argument("--owner-url", required=True)
    a.add_argument("--terms-url", required=True)
    a.add_argument("--licence", required=True)
    a.add_argument("--legal-basis", default="public_business_info")
    a.add_argument("--rate-limit", required=True, help="documented or measured limit, free text")
    a.add_argument("--content-type")
    a.add_argument("--schema")
    a.add_argument("--reviewed-by", required=True)
    a.add_argument("--review-every-days", type=int)
    a.add_argument("--robots-reviewed-status", type=int, choices=[401, 403],
                   help="the status this host's robots.txt answers, recorded after you have read the host's terms; "
                        "without it a 401 or 403 on robots.txt puts the whole host off limits")
    a.set_defaults(fn=cmd_sources_add)
    rv = ss.add_parser("review", help="show quarantine reasons; --clear restores a quarantined source")
    rv.add_argument("name")
    rv.add_argument("--clear", action="store_true")
    rv.add_argument("--clear-event", metavar="UID", help="reset one event's flags (event_sites)")
    rv.set_defaults(fn=cmd_sources_review)
    c = ss.add_parser("candidates", help="hosts discovered in content (never fetched)")
    c.add_argument("--top", type=int, default=30)
    c.add_argument("--reject", type=int, metavar="ID", help="mark one candidate as rejected")
    c.add_argument("--reject-host", metavar="HOST", help="reject every new candidate on this host (and subdomains)")
    c.set_defaults(fn=cmd_sources_candidates)
    for name, about in (("enable", "resume a paused source (a quarantine stays until `review --clear`)"),
                        ("disable", "pause a source (its health and any quarantine stay on record)")):
        e = ss.add_parser(name, help=about)
        e.add_argument("name")
        e.set_defaults(fn=cmd_sources_toggle, cmd=name)
    pr = sub.add_parser("profiles", help="list profiles")
    pr.add_argument("pcmd", nargs="?", default="list", choices=["list", "check"])
    pr.set_defaults(fn=cmd_profiles)
    sg = sub.add_parser("suggest", help="ranked suggestions")
    sg.add_argument("--profile")
    sg.add_argument("--kind")
    sg.add_argument("--top", type=int, default=20)
    sg.add_argument("--plan", metavar="MINUTES", help="e.g. 45m or 2h: best set within that time")
    sg.add_argument("--held", action="store_true", help="show held suggestions instead")
    sg.add_argument("--json", action="store_true")
    sg.add_argument("--regroup-dry-run", action="store_true",
                    help="count the old one-per-posting hiring suggestions the next run expires in favour of one per "
                         "company; writes nothing")
    sg.set_defaults(fn=cmd_suggest)
    co = sub.add_parser("companies", help="hiring companies, one row per canonical company key")
    co.add_argument("--dupes", action="store_true",
                    help=f"list near-miss names of different companies and the {ALIASES_FILE} line that merges each pair; "
                         "writes nothing")
    co.add_argument("--json", action="store_true")
    co.set_defaults(fn=cmd_companies)
    ql = sub.add_parser("qualify", help="ask a model the profile's qualify question about each company, from stored evidence")
    ql.add_argument("--profile", required=True)
    ql.add_argument("--slot", choices=["local", "router", "cloud"], default="local",
                    help="which llm: slot answers (default: local, the model on this machine)")
    ql.add_argument("--model", help="model name; on the router slot one of llm.router.models (default: the slot's model)")
    ql.add_argument("--dry-run", action="store_true", help="show what would be asked and the cost estimate; send nothing")
    ql.add_argument("--max-usd", type=float, metavar="AMOUNT", help="stop before this much is spent; required for a remote slot")
    ql.add_argument("--limit", type=int, metavar="N", help="ask at most N subjects (in key order)")
    ql.add_argument("--again", action="store_true", help="also ask subjects whose last good answer is still current")
    ql.add_argument("--show", action="store_true", help="list the stored answers and send nothing")
    ql.add_argument("--json", action="store_true")
    ql.set_defaults(fn=cmd_qualify)
    sh = sub.add_parser("show")
    sh.add_argument("id", type=int)
    sh.set_defaults(fn=cmd_show)
    ac = sub.add_parser("accept")
    ac.add_argument("id", type=int)
    ac.set_defaults(fn=cmd_feedback, cmd="accept")
    di = sub.add_parser("dismiss")
    di.add_argument("id", type=int)
    di.add_argument("--reason")
    di.set_defaults(fn=cmd_feedback, cmd="dismiss")
    dn = sub.add_parser("done")
    dn.add_argument("id", type=int)
    dn.add_argument("--outcome", required=True, choices=["won", "lost", "meeting", "reply", "none"])
    dn.add_argument("--minutes", type=int)
    dn.set_defaults(fn=cmd_feedback, cmd="done")
    ex = sub.add_parser("export", help="digest (md/html/csv), suggestions (csv), items (csv), label-sheet (csv) or "
                                       "euboulia-facts (json) to feeds/")
    ex.add_argument("what", choices=["digest", "suggestions", "items", "prospects", "label-sheet", "euboulia-facts"])
    ex.add_argument("--sample", type=int, default=100, metavar="N", help="label-sheet: how many rows to draw (default 100)")
    ex.add_argument("--seed", type=int, default=7, help="label-sheet: the same seed draws the same rows (default 7)")
    ex.add_argument("--force", action="store_true", help="label-sheet: replace an existing sheet (its labels are lost)")
    ex.add_argument("--event", help="prospects: one event (item id or uid)")
    ex.add_argument("--format", choices=["md", "html", "csv"], default="md")
    ex.add_argument("--profile")
    ex.add_argument("--status", default="new,accepted,held", help="comma list; default new,accepted,held")
    ex.add_argument("--since", metavar="7d", help="only suggestions/items updated in the last N d/w/h")
    ex.add_argument("-o", "--output", help="file path (default: feeds/<what>-<week>[-profile].<fmt>)")
    ex.set_defaults(fn=cmd_export)
    lk = sub.add_parser("lookup", help="crawl one event's pages now (event id, uid, title fragment or #suggestion)")
    lk.add_argument("event")
    lk.add_argument("--dry-run", action="store_true", help="fetch and extract, store nothing, print what was found")
    lk.add_argument("--replay", metavar="DIR", help="serve samples from DIR instead of the network")
    lk.set_defaults(fn=cmd_lookup)
    sub.add_parser("status", help="source health, held and quarantined counts").set_defaults(fn=cmd_status)
    sub.add_parser("budget", help="daily budgets and per-source stats").set_defaults(fn=cmd_budget)
    return p


def main(argv: list[str] | None = None) -> int:
    with guarded_output():
        return _main(argv)


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    try:
        app = App(verbose=args.verbose)
    except (PolicyError, ConfigError, ProfileError, SchemaError) as e:
        print(f"refusing to run: {e}", file=sys.stderr)
        return 3
    try:
        return int(args.fn(args, app) or 0)
    finally:
        try:
            asyncio.run(app.fetcher.aclose())
        except Exception:  # noqa: BLE001
            pass
        app.conn.close()


if __name__ == "__main__":
    sys.exit(main())
