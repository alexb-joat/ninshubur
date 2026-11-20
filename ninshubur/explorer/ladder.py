"""The trial ladder: how far a site the explorer found has earned its keep.

    candidate --probe--> probed --first clean page--> trial --20 clean pages, or 7 days and 3--> admitted
                                                        ^                                          |
                                                        +---- ownership change, or 30 days stale --+

- **candidate**: the frontier knows a URL on it; one page a day at most.
- **probed**: the random-path probe found no soft-404 (that stops a site). Still one page a day.
- **trial**: a page came back clean (HTTP 200, no transport finding, parsed in the sandbox, no taint). Tier 3, the
  trial cap of pages a day. It graduates after ``trial_clean_fetches`` clean pages, or after ``trial_days`` with at
  least ``trial_min_clean``.
- **admitted**: still the lowest trust (Tier 3, held, "found by explorer"); only Alex promotes a site to a real
  source. Back to trial when the site looks like it changed hands: its address moves to a network it was never seen
  on during the trial (IPv4 /16, IPv6 /32: wide, because CDNs rotate addresses), or a front-page fetch is more than
  ``content_shift_bits`` simhash bits from the one it was admitted with; and when it has had no clean page for
  ``admitted_stale_days`` (trust decays without fresh clean fetches).

What stops a site at any rung (the frontier's ``stop_site``; nothing of it is fetched again): high taint, an
integrity finding (bad certificate, wrong content type, decompression bomb, oversize), a page that broke the parser
(timeout, memory, crash, nonsense output), and the frontier's own stops (soft-404, parked, near-duplicates, no
yield). A certificate-issuer change is not checked yet: the fetcher doesn't expose the peer certificate.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..db import one
from . import traps
from .frontier import Frontier

STAGES = ("candidate", "probed", "trial", "admitted")
INTEGRITY = frozenset({"cert_error", "content_type_mismatch", "decompression_bomb", "body_too_large", "invalid_utf8"})
PARSER_STOPS = frozenset({"parser_timeout", "parser_memory", "parser_killed", "parser_crashed", "parser_output_too_large",
                          "parser_output_invalid"})
DAY = 86400.0


def network(addr: str) -> str | None:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{ip}/{16 if ip.version == 4 else 32}", strict=False))


def is_front_page(url: str) -> bool:
    p = urlsplit(url)
    return p.path in ("", "/") and not p.query


@dataclass
class Step:
    """What one fetch did to its site's rung. ``stopped`` is the stop reason when the site was stopped, ``own`` whether
    the ladder stopped it for what the page sent (taint, an integrity or parser failure)."""
    site: str
    before: str
    after: str
    reason: str = ""
    stopped: str | None = None
    own: bool = False


class Ladder:
    def __init__(self, frontier: Frontier):
        self.fr = frontier
        self.conn = frontier.conn
        self.lim = frontier.limits
        self.log = frontier.log

    def site(self, site: str) -> dict:
        return one(self.conn, "SELECT * FROM explorer_sites WHERE site=?", (site,)) or {}

    def _set(self, site: str, stage: str, reason: str, **cols) -> None:
        now = self.fr.now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.conn:
            self.conn.execute(f"UPDATE explorer_sites SET stage=?, stage_at=?{', ' + sets if sets else ''} WHERE site=?",
                              (stage, now, *cols.values(), site))
        self.log.write({"event": "ladder", "site": site, "stage": stage, "reason": reason[:200]})

    def probed(self, site: str) -> None:
        """The soft-404 probe passed."""
        if self.site(site).get("stage") == "candidate":
            self._set(site, "probed", "random path answered like a missing page")

    def stop(self, site: str, before: str, reason: str) -> Step:
        self.fr.stop_site(site, reason)
        return Step(site, before, before, reason, stopped=reason, own=True)

    def after_fetch(self, site: str, url: str, *, code: str, clean: bool, taint: str | None,
                    fingerprint: int | None = None, addrs: tuple[str, ...] = ()) -> Step:
        """Move ``site`` on the ladder after a fetch of ``url``. ``code`` is the gate's (or the parser's) outcome,
        ``clean`` whether the page counts as a clean fetch, ``fingerprint`` the page's simhash when it was read."""
        s = self.site(site)
        stage = s.get("stage", "candidate")
        # Asked first, also of a site the frontier stopped on this very page (no yield, say): the graver reason is
        # the one that stays on record.
        if taint == "high":
            return self.stop(site, stage, f"taint: a page carried high-severity content ({url[:120]})")
        hit = next((c for c in code.split(",") if c in INTEGRITY), None) or (code if code in PARSER_STOPS else None)
        if hit:
            return self.stop(site, stage, f"{hit}: {url[:120]}")
        if s.get("status", "open") != "open":
            return Step(site, stage, stage, stopped=s.get("status_reason") or "stopped")
        now = self.fr.now()
        nets = json.loads(s.get("nets") or "[]")
        seen = sorted({n for n in map(network, addrs) if n})
        if stage == "admitted":
            moved = [n for n in seen if n not in nets]
            shift = (fingerprint is not None and s.get("fingerprint") and is_front_page(url)
                     and traps.hamming(fingerprint, int(s["fingerprint"], 16)) > self.lim.content_shift_bits)
            stale = s.get("last_clean") and now - float(s["last_clean"]) > self.lim.admitted_stale_days * DAY
            why = (f"address moved to {moved[0]}, a network not seen in its trial" if moved and nets else
                   "front page changed sharply since admission" if shift else
                   f"no clean page in {self.lim.admitted_stale_days} days" if stale else "")
            if why:
                self._set(site, "trial", f"re-trial: {why}", trial_started=now, clean_fetches=int(clean),
                          last_clean=now if clean else s.get("last_clean"), nets=json.dumps(sorted(set(nets) | set(seen))))
                return Step(site, stage, "trial", f"re-trial: {why}")
        if not clean:
            return Step(site, stage, stage)
        n = int(s.get("clean_fetches") or 0) + 1
        fp = s.get("fingerprint")
        if fingerprint is not None and is_front_page(url) and (stage != "admitted" or not fp):
            fp = format(fingerprint, "016x")  # the front page as it looked during the trial
        cols = {"clean_fetches": n, "last_clean": now, "fingerprint": fp}
        if stage in ("candidate", "probed"):
            self._set(site, "trial", "first clean page", trial_started=now, nets=json.dumps(seen), **cols)
            return Step(site, stage, "trial", "first clean page")
        if stage == "trial":
            cols["nets"] = json.dumps(sorted(set(nets) | set(seen)))
            age = now - float(s.get("trial_started") or now)
            if n >= self.lim.trial_clean_fetches or (age >= self.lim.trial_days * DAY and n >= self.lim.trial_min_clean):
                why = f"{n} clean pages" + (f" over {age / DAY:.0f} days" if age >= self.lim.trial_days * DAY else "")
                self._set(site, "admitted", why, **cols)
                return Step(site, stage, "admitted", why)
        with self.conn:
            self.conn.execute(f"UPDATE explorer_sites SET {', '.join(f'{k}=?' for k in cols)} WHERE site=?",
                              (*cols.values(), site))
        return Step(site, stage, stage)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in (*STAGES, "stopped")}
        for r in self.conn.execute("SELECT stage, status, COUNT(*) AS n FROM explorer_sites GROUP BY stage, status"):
            key = r["stage"] if r["status"] == "open" else "stopped"
            out[key] = out.get(key, 0) + r["n"]
        return out
