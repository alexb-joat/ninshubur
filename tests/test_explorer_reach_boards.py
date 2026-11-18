"""Reach adapter 3: board tokens from links the main database holds, each probed alone; config never written."""
from __future__ import annotations

import uuid

from ninshubur import db
from ninshubur.config import load_config
from ninshubur.explorer import store
from ninshubur.explorer.reach import boards
from ninshubur.explorer.reach.run import reach
from ninshubur.policy import Policy
from ninshubur.profiles import Profile
from tests.conftest import Seen, trust, write_config
from tests.test_explorer_reach import ctx

NO_ROBOTS = {"status": 404, "body": "", "headers": {"Content-Type": "text/plain"}}
GH_JOBS = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://job-boards.greenhouse.io/newco/jobs/1"}]}
ASHBY_EMPTY = {"jobs": []}
LEVER_JOBS = [{"id": str(uuid.UUID(int=1)), "text": "Engineer", "hostedUrl": "https://jobs.lever.co/levco/1"}]


def _main(home, items=(), cands=()):
    conn = db.connect(home)
    for i, (url, text, tainted) in enumerate(items):
        conn.execute("INSERT INTO items(uid, source, kind, title, url, text, first_seen_at, last_seen_at, legal_basis, tainted) "
                     "VALUES (?,?,?,?,?,?,1,1,'public_business_info',?)", (f"u{i}", "hn", "hiring", "t", url, text, tainted))
    for host, ex in cands:
        conn.execute("INSERT INTO candidates(host, example_url, found_in_source, first_seen, last_seen) VALUES (?,?,?,1,1)",
                     (host, ex, "event-sites"))
    conn.close()


def test_tokens_in_links():
    got = boards.tokens_in("Apply: https://job-boards.greenhouse.io/NewCo/jobs/12 or https://jobs.ashbyhq.com/PostHog/abc, "
                           "https://boards.greenhouse.io/embed/job_app?for=x https://jobs.lever.co/levco "
                           "https://jobs.ashbyhq.com/some%20name https://jobs.ashbyhq.com/dot.tech")
    assert [(p, t) for p, t, _ in got] == [("greenhouse", "newco"), ("ashby", "PostHog"), ("lever", "levco")]


def test_known_tokens_from_config_and_profiles(home):
    write_config(home, sources={"job-boards-greenhouse": {"adapter": "greenhouse", "params": {"boards": ["Known"]}, "trust": trust(1, "greenhouse_jobs.v1")}})
    policy = Policy.load(home)
    cfg = load_config(home, policy=policy)
    prof = {"p": Profile(name="p", target_companies=[{"name": "X", "ashby": "Xco"}])}
    assert boards.known_tokens(cfg, prof) == {("greenhouse", "known"), ("ashby", "xco")}


def test_find_tokens_skips_known_tainted_and_counts_spellings(home):
    _main(home, items=[("https://jobs.ashbyhq.com/PostHog/1", "", 0), ("https://jobs.ashbyhq.com/posthog/2", "", 0),
                       ("https://jobs.ashbyhq.com/PostHog/3", "", 0),
                       ("https://news.ycombinator.com/item?id=1", "we hire: https://jobs.lever.co/levco", 0),
                       ("https://news.ycombinator.com/item?id=2", "https://jobs.lever.co/evilco", 1)],
          cands=[("job-boards.greenhouse.io", "https://job-boards.greenhouse.io/known/jobs/1")])
    from ninshubur.explorer.reach.logged import open_main_readonly
    main = open_main_readonly(home)
    got = boards.find_tokens(main, {("greenhouse", "known")})
    assert [(t.platform, t.token, t.seen) for t in got] == [("ashby", "PostHog", 3), ("lever", "levco", 1)]


def _routes(seen_paths):
    def gh(req):
        import httpx
        seen_paths.append(req.url.path)
        if "/boards/deadco/" in req.url.path:
            return httpx.Response(404, json={"status": 404})
        return httpx.Response(200, json=GH_JOBS)
    return {("boards-api.greenhouse.io", "/robots.txt"): NO_ROBOTS, ("boards-api.greenhouse.io", "*"): gh,
            ("api.ashbyhq.com", "/robots.txt"): NO_ROBOTS, ("api.ashbyhq.com", "*"): {"body": ASHBY_EMPTY},
            ("api.lever.co", "/robots.txt"): NO_ROBOTS, ("api.lever.co", "*"): {"body": LEVER_JOBS}}


def test_probe_each_token_alone_mark_status_and_preview_yaml(home):
    write_config(home, sources={"job-boards-greenhouse": {"adapter": "greenhouse", "params": {"boards": ["known"]}, "trust": trust(1, "greenhouse_jobs.v1")}})
    cfg_before = (home / "config.yaml").read_bytes()
    _main(home, items=[("https://job-boards.greenhouse.io/newco/jobs/1", "", 0),
                       ("https://job-boards.greenhouse.io/deadco/jobs/1", "", 0),
                       ("https://jobs.ashbyhq.com/emptyco", "", 0), ("https://jobs.lever.co/levco", "", 0)])
    paths: list[str] = []
    seen = Seen()
    rep = reach(ctx(home, routes=_routes(paths), seen=seen), only="boards")
    assert rep.requests == 4 and rep.probes == {"trial": 2, "rejected": 2}
    assert sorted(p for p in paths if "/boards/" in p) == ["/v1/boards/deadco/jobs", "/v1/boards/newco/jobs"]
    conn = store.connect(home)
    st = {r["url"].split("/")[-2 if "greenhouse" in r["url"] else -1].split("?")[0]: (r["status"], r["status_reason"])
          for r in conn.execute("SELECT url, status, status_reason FROM explorer_candidates WHERE kind='board'")}
    assert st == {"newco": ("trial", "1 open job"), "deadco": ("rejected", "no such board (404)"),
                  "emptyco": ("rejected", "board answers but lists no open jobs"), "levco": ("trial", "1 open job")}
    assert rep.yaml[0] == "# add under sources.job-boards-greenhouse.params.boards in config.yaml:"
    assert rep.yaml[1].startswith("      - newco   # 1 open job, probed ")
    assert "no Lever adapter yet: levco" in rep.yaml[2]
    assert (home / "config.yaml").read_bytes() == cfg_before  # never written
    again = reach(ctx(home, routes=_routes(paths)), only="boards")
    assert again.requests == 0  # decided boards are not probed again


def test_transient_failure_leaves_a_candidate_and_cap_defers_the_rest(home):
    import httpx
    _main(home, items=[(f"https://job-boards.greenhouse.io/co{i}/jobs/1", "", 0) for i in range(3)])
    r = {("boards-api.greenhouse.io", "/robots.txt"): NO_ROBOTS,
         ("boards-api.greenhouse.io", "*"): lambda req: httpx.Response(502, text="bad gateway")}
    c = ctx(home, routes=r)
    c.max_probes = 2
    rep = reach(c, only="boards")
    assert rep.requests == 2 and rep.probes == {}
    assert any("1 more token(s) wait for the next run" in n for n in rep.notes)
    assert sum("left for the next run" in n for n in rep.notes) == 2
    conn = store.connect(home)
    assert {r["status"] for r in conn.execute("SELECT status FROM explorer_candidates")} == {"candidate"}


def test_dry_run_plans_probes_without_requests(home):
    _main(home, items=[("https://job-boards.greenhouse.io/newco/jobs/1", "", 0)])
    seen = Seen()
    rep = reach(ctx(home, dry_run=True, seen=seen), only="boards")
    assert rep.planned == [("boards", "GET https://boards-api.greenhouse.io/v1/boards/newco/jobs?content=false  [seen in 1 link(s)]")]
    assert seen.calls == [] and not (home / "explorer").exists()
