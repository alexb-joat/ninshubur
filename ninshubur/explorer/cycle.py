"""One explorer cycle: probe, fetch, parse in the sandbox, follow links, move sites on the trial ladder.

For each URL the frontier releases (at most one per site, within the site's day cap for its rung):

1. a site never probed gets the random-path soft-404 probe first (one request); no answer uses up the site's page
   for today, and ``probe_tries`` tries without an answer stop it, so a dead site never holds up the queue;
2. the gate fetches the page (STOP file, DNS pin, deadline, caps, robots: all in the gate and the shared fetcher);
3. the body goes to the bwrap parser and its answer is re-checked and taint-scanned (``sandbox.py``); the body stays
   in memory and is never written anywhere;
4. the frontier records the page (near-duplicates, parked, no yield), the ladder moves the site (``ladder.py``);
5. links from an untainted page are offered to the frontier, ``links_per_page`` at most and ``max_hops`` from a
   candidate: text from a page can only *propose* a URL, the frontier decides;
6. records (events, jobs) from sites in trial or admitted go to ``on_records`` (the one-way handoff).

The cycle halts at once when the STOP file appears, the sandbox can't run (fail closed: an unparsed page is never
used) or the 24-hour byte budget is used up. Those are the explorer's own stops, not the page's: the URL stays queued
and nothing is counted against its site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import store, traps
from .frontier import SSRF_CODES
from .gate import ExplorerGate
from .ladder import Ladder
from .sandbox import Page, Sandbox, SandboxError, read_page


@dataclass
class Line:
    url: str
    code: str
    status: int | None = None
    bytes: int = 0
    stage: str = ""
    moved: str = ""          # "trial -> admitted" and the like
    links_offered: int = 0
    links_queued: int = 0
    ssrf_links: int = 0
    hidden_links: int = 0
    records: int = 0
    stopped: str | None = None


@dataclass
class CycleReport:
    lines: list[Line] = field(default_factory=list)
    probes: int = 0
    records: list[dict] = field(default_factory=list)
    halted: str | None = None

    @property
    def requests(self) -> int:
        return self.probes + sum(1 for x in self.lines if x.code not in ("probe_no_answer", "soft_404"))


NO_SANDBOX = "the parser sandbox can't run; see `ninshubur explore check`"
NO_BUDGET = "the byte budget for 24 hours is used up (explorer: daily_bytes in policy.yaml); the queue waits"


def _candidate_status(stage: str, stopped: str | None, by_frontier: bool) -> str:
    """What a candidate's row says after its page. A site the frontier stopped (soft-404, no answer to the probe,
    parked, near-duplicates, no yield) was not worth reading: ``rejected``. One stopped for what it sent or tried
    (taint, an integrity or parser failure, a denied address) is ``quarantined``."""
    if stopped:
        return "rejected" if by_frontier else "quarantined"
    return stage


def _mark(fr, row: dict, status: str, reason: str) -> None:
    if row.get("candidate_id"):
        store.set_status(fr.conn, row["candidate_id"], status, reason, fr.now())


async def run_cycle(gate: ExplorerGate, sandbox: Sandbox, limit: int, *,
                    on_records: Callable[[list[dict]], None] | None = None) -> CycleReport:
    fr = gate.frontier
    ladder = Ladder(fr)
    lim = fr.limits
    rep = CycleReport()
    for row in fr.next_batch(limit):
        if gate.stopped:
            rep.halted = f"the STOP file exists ({gate.stop_path})"
            break
        url, site, host = row["url"], row["site"], row["host"]
        s = ladder.site(site)
        if s.get("stage") == "candidate" and s.get("soft404") is None:
            soft = await gate.probe_soft404(host)
            rep.probes += 1
            if soft is None:
                if gate.stopped:
                    rep.halted = f"the STOP file exists ({gate.stop_path})"
                    break
                if gate.probe_error == "byte_budget":
                    rep.halted = NO_BUDGET
                    break
                gone = fr.note_probe_miss(site, gate.probe_error)
                rep.lines.append(Line(url, "probe_no_answer", stage="candidate", stopped=gone))
                if gone:
                    _mark(fr, row, "rejected", gone)
                continue
            if soft:
                gone = ladder.site(site).get("status_reason") or "soft_404"
                rep.lines.append(Line(url, "soft_404", stage="candidate", stopped=gone))
                _mark(fr, row, "rejected", gone)
                continue
            ladder.probed(site)
        out = await gate.fetch(url, record=False)
        if out.code == "stopped":
            rep.halted = f"the STOP file exists ({gate.stop_path})"
            break
        if out.code == "byte_budget":
            rep.halted = NO_BUDGET
            break
        code, page = out.code, None
        if out.body:
            try:
                page = read_page(sandbox.parse(out.body, out.final_url or url, out.content_type))
            except SandboxError as e:
                code = e.code
        if code == "sandbox_unavailable":  # ours, not the page's: nothing is recorded, so the page is read once it works
            rep.lines.append(Line(url, code, out.status, out.bytes, stage=ladder.site(site).get("stage", "candidate")))
            rep.halted = NO_SANDBOX
            break
        stop = fr.record(url, status=out.status, body=out.body, outcome=code, useful=bool(page and page.useful))
        clean = code == "ok" and page is not None and page.taint is None and not page.limited
        step = ladder.after_fetch(site, url, code=code, clean=clean, taint=page.taint if page else None,
                                  fingerprint=traps.simhash(out.body) if out.body else None, addrs=out.addrs)
        line = Line(url, code if not page or not page.taint else f"{code},taint_{page.taint}", out.status, out.bytes,
                    stage=step.after, moved=f"{step.before} -> {step.after}" if step.before != step.after else "",
                    stopped=step.stopped or stop, hidden_links=page.hidden_links if page else 0)
        # after_fetch also reports a stop fr.record has just made, so "by the frontier" is the frontier's own answer,
        # unless the ladder found a graver reason of its own
        _mark(fr, row, _candidate_status(step.after, line.stopped, by_frontier=bool(stop) and not step.own),
              line.stopped or step.reason or code)
        if page is not None and not line.stopped:
            _follow(fr, page, row, line, lim)
            if step.after in ("trial", "admitted") and page.records:
                recs = [{**r, "site": site, "page": url, "stage": step.after, "taint": list(page.taint_codes)}
                        for r in page.records]
                line.records = len(recs)
                rep.records += recs
                if on_records:
                    on_records(recs)
        rep.lines.append(line)
    return rep


def _follow(fr, page: Page, row: dict, line: Line, lim) -> None:
    """Offer the page's links to the frontier, unless anything on the page tripped a taint detector: text flagged as
    a possible injection, at any severity, doesn't get to steer where the explorer goes next."""
    if page.taint is not None or row["depth"] + 1 > lim.max_hops:
        return
    for link in page.links[: lim.links_per_page]:
        o = fr.offer(link, depth=row["depth"] + 1, found_from=row["url"])
        line.links_offered += 1
        line.links_queued += int(o.queued)
        line.ssrf_links += int(o.code in SSRF_CODES)
