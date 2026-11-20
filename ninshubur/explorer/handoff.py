"""The one-way handoff from the explorer to main Ninshubur: capped JSONL files, and nothing else.

The explorer *writes* records (events and jobs from sites in trial or admitted) to
``~/.ninshubur/explorer/out/YYYY-MM-DD.jsonl``. Main Ninshubur *reads* them: it never opens the explorer's database,
and the explorer never opens main's. Killing the explorer (``kill -9``, OOM, a full disk) can leave at worst a torn
last line, which the reader skips; main keeps running either way.

Caps (writer and reader both enforce them): a line is at most ``MAX_LINE`` bytes, a file at most ``MAX_FILE`` bytes,
``KEEP_DAYS`` files are kept. The reader re-validates every line against the schema below, scans its text for taint
again, and stores what passes as a Tier 3 item of the source ``explorer``, always **held** and marked "found by
explorer"; the explorer's own taint codes travel with it. A line that fails any check is counted and skipped, never
stored. One flag stops reading: ``explorer: {read_inbox: false}`` in config.yaml.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import detectors
from ..adapters.base import AdapterSpec
from ..config import Config, SourceConfig
from ..db import one
from ..ingest import Ingestor
from ..items import DateRef, Item, item_uid
from ..logs import JsonlLog
from ..policy import Policy, check_url, host_of
from ..schemas import parse_when, sanitize_str

VERSION = 1
MAX_LINE = 4096
MAX_FILE = 2_000_000
KEEP_DAYS = 14
READ_PER_CALL = 4_000_000
FILE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
KEYS = frozenset({"v", "id", "type", "name", "url", "start", "end", "place", "org", "site", "page", "stage", "taint",
                  "found_at"})
CAPS = {"id": 24, "name": 200, "url": 2048, "start": 40, "end": 40, "place": 160, "org": 160, "site": 253, "page": 2048}
SOURCE = SourceConfig(name="explorer", adapter="explorer", trust={"tier": 3, "owner": "Ninshubur explorer",
                                                                  "legal_basis": "public_business_info"})
SPEC = AdapterSpec(name="explorer", kind="discovery", hosts=frozenset(), schema="", expected_content_type="",
                   base_interval_s=86400, max_interval_s=86400 * 7, default_tier=3, legal_basis="public_business_info")
INBOX_SCHEMA = """CREATE TABLE IF NOT EXISTS explorer_inbox (
    file TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,
    stored INTEGER NOT NULL DEFAULT 0, bad INTEGER NOT NULL DEFAULT 0, at REAL)"""


def out_dir(home: Path) -> Path:
    return home / "explorer" / "out"


def record_id(r: dict) -> str:
    key = "|".join(str(r.get(k, "")) for k in ("site", "type", "name", "start", "url"))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# -- the explorer's side --------------------------------------------------------------------------------------------
class Writer:
    """Appends records to today's file. Never raises into the explorer's cycle: a full disk or a capped file stops
    writing and says so in the explorer log."""

    def __init__(self, home: Path, log: JsonlLog, now: Callable[[], float] = time.time):
        self.dir = out_dir(home)
        self.log = log
        self.now = now
        self.written = 0
        self.refused = 0
        self.failed: str | None = None

    def _path(self) -> Path:
        return self.dir / (time.strftime("%Y-%m-%d", time.gmtime(self.now())) + ".jsonl")

    def write(self, records: list[dict]) -> None:
        if self.failed or not records:
            self.refused += len(records) if self.failed else 0
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            path = self._path()
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":  # a line torn by a kill: end it, so the next stands alone
                    os.write(fd, b"\n")
                    size += 1
                for r in records:
                    line = (json.dumps({"v": VERSION, "id": record_id(r), **{k: r[k] for k in KEYS - {"v", "id", "found_at"}},
                                        "found_at": round(self.now(), 3)}, ensure_ascii=False) + "\n").encode()
                    if len(line) > MAX_LINE or size + len(line) > MAX_FILE:
                        self.refused += 1
                        continue
                    os.write(fd, line)
                    size += len(line)
                    self.written += 1
            finally:
                os.close(fd)
            self._prune()
        except OSError as e:
            self.failed = f"{type(e).__name__}: {e.strerror or e}"
            self.refused += len(records)
            self.log.write({"event": "handoff_write_failed", "detail": self.failed[:200]})

    def _prune(self) -> None:
        files = sorted(p for p in self.dir.iterdir() if FILE_NAME.match(p.name))
        for p in files[:-KEEP_DAYS]:
            p.unlink(missing_ok=True)


# -- main Ninshubur's side ------------------------------------------------------------------------------------------
@dataclass
class InboxReport:
    enabled: bool = True
    files: int = 0
    lines: int = 0
    stored: int = 0
    updated: int = 0
    tainted: int = 0
    dropped_taint: int = 0
    bad: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)

    def refuse(self, why: str) -> None:
        self.bad += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1


def _str(v, cap: int) -> bool:
    return isinstance(v, str) and len(v) <= cap


def check_line(raw: bytes) -> tuple[dict | None, str]:
    """A line from the explorer, or None and why not. Every key, type and size is checked; nothing is coerced."""
    if len(raw) > MAX_LINE:
        return None, "line_too_long"
    try:
        r = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "not_json"
    if not isinstance(r, dict) or set(r) != KEYS:
        return None, "wrong_keys"
    if r["v"] != VERSION or r["type"] not in ("event", "hiring") or r["stage"] not in ("trial", "admitted"):
        return None, "wrong_version_type_or_stage"
    if not all(_str(r[k], cap) for k, cap in CAPS.items()) or not r["name"].strip():
        return None, "bad_field"
    if not isinstance(r["taint"], list) or len(r["taint"]) > 20 or not all(_str(c, 40) for c in r["taint"]):
        return None, "bad_taint"
    if type(r["found_at"]) not in (int, float):
        return None, "bad_time"
    if check_url(r["page"]) or (r["url"] and check_url(r["url"])):
        return None, "bad_url"
    host = host_of(r["page"]) or ""
    if not (host == r["site"] or host.endswith("." + r["site"])):
        return None, "site_mismatch"
    return r, ""


def _item(r: dict, codes: tuple[str, ...]) -> Item:
    name = sanitize_str(r["name"], 200) or ""
    at = parse_when(r["start"]) if r["start"] else None
    end = parse_when(r["end"]) if r["end"] else None
    dates = []
    if at is not None:
        dates.append(DateRef("event_start" if r["type"] == "event" else "posted", at, end if r["type"] == "event" else None))
    return Item(uid=item_uid("explorer", r["id"]), source="explorer", kind=r["type"], title=name, url=r["url"] or r["page"],
                text="", published_at=at if r["type"] == "hiring" else None, dates=dates, tier=3,
                attrs={"found_by": "explorer", "site": r["site"], "page": r["page"], "stage": r["stage"],
                       "place": sanitize_str(r["place"], 160) or "", "org": sanitize_str(r["org"], 160) or ""},
                tainted=bool(codes), taint_codes=codes, taint_severity="medium" if codes else None)


class Inbox:
    """Main Ninshubur's reader, kept for the daemon's life. The table and the offsets are set up once, and a tick
    with nothing new costs one directory listing: no file is opened, nothing is queried, nothing is built. A file is
    opened only when it is longer than what was read of it, and its offset is asked of the database again then, so
    a second reader (``explore inbox`` beside the daemon) can't make this one read lines twice."""

    def __init__(self, conn: sqlite3.Connection, policy: Policy, home: Path, *, ingestor: Ingestor | None = None,
                 now: Callable[[], float] = time.time):
        self.conn, self.policy, self.home, self.now = conn, policy, home, now
        self.ingestor = ingestor
        self.log = JsonlLog(home / "logs" / "policy.jsonl")
        self.offsets: dict[str, int] | None = None  # file name -> bytes read, as of this reader's last look

    def _files(self) -> list[tuple[str, os.stat_result | None]]:
        d = out_dir(self.home)
        if not d.is_dir() or d.is_symlink():
            return []
        out = []
        with os.scandir(d) as it:
            for e in it:
                if FILE_NAME.match(e.name):
                    try:
                        out.append((e.name, e.stat(follow_symlinks=False)))
                    except OSError:
                        out.append((e.name, None))
        return sorted(out, key=lambda x: x[0])

    def read(self, cfg: Config, *, dry_run: bool = False) -> InboxReport:
        """Read what the explorer wrote since last time into main's database (``dry_run``: count, store nothing)."""
        rep = InboxReport(enabled=cfg.get("explorer", "read_inbox", default=True) is not False)
        if not rep.enabled:
            return rep
        try:
            files = self._files()
        except OSError:
            return rep
        if not files:
            return rep
        if self.offsets is None:
            self.conn.execute(INBOX_SCHEMA)
            self.offsets = {r["file"]: int(r["offset"]) for r in self.conn.execute("SELECT file, offset FROM explorer_inbox")}
        ing = self.ingestor
        source_ready = False
        budget = READ_PER_CALL
        d = out_dir(self.home)
        for name, lst in files:
            if lst is None:
                rep.skipped_files.append(f"{name}: can't open")
                continue
            if not stat.S_ISREG(lst.st_mode):  # told from the listing: a FIFO is never opened (it would block the tick)
                rep.skipped_files.append(f"{name}: " + ("a symlink, never followed" if stat.S_ISLNK(lst.st_mode)
                                                        else "not a regular file"))
                continue
            if lst.st_size > MAX_FILE:
                rep.skipped_files.append(f"{name}: over {MAX_FILE:,} bytes, read up to the cap")
            if min(lst.st_size, MAX_FILE) <= self.offsets.get(name, 0) or budget <= 0:
                continue
            try:
                fd = os.open(d / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                rep.skipped_files.append(f"{name}: can't open")
                continue
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):  # swapped since the listing
                    rep.skipped_files.append(f"{name}: not a regular file")
                    continue
                pos = one(self.conn, "SELECT offset FROM explorer_inbox WHERE file=?", (name,))
                start = self.offsets[name] = int(pos["offset"]) if pos else 0
                end = min(st.st_size, MAX_FILE)
                if start >= end:
                    continue
                chunk = os.pread(fd, min(end - start, budget), start)
            finally:
                os.close(fd)
            cut = chunk.rfind(b"\n") + 1  # only whole lines; a torn tail waits (or is ended by the writer's next line)
            if cut == 0:
                continue
            rep.files += 1
            budget -= cut
            bad_before, n_stored = rep.bad, 0
            for raw in chunk[:cut].split(b"\n")[:-1]:
                if not raw.strip():
                    continue
                rep.lines += 1
                r, why = check_line(raw)
                if r is None:
                    rep.refuse(why)
                    continue
                findings = [f for k in ("name", "place", "org") for f in detectors.scan_text(r[k])]
                if detectors.worst(findings) == "high":
                    rep.dropped_taint += 1
                    continue
                codes = tuple(dict.fromkeys([*r["taint"], *detectors.codes(findings)]))
                rep.tainted += int(bool(codes))
                if dry_run:
                    n_stored += 1
                    continue
                if not source_ready:
                    ing = ing or Ingestor(self.conn, self.policy, cfg, self.home, now=self.now)
                    ing.ensure_source(SOURCE, SPEC)
                    source_ready = True
                created = ing._store_item(_item(r, codes), SOURCE, float(r["found_at"]), held=True)
                rep.stored += int(created)
                rep.updated += int(not created)
                n_stored += 1
            if not dry_run:
                with self.conn:
                    self.conn.execute("""INSERT INTO explorer_inbox(file, offset, lines, stored, bad, at) VALUES (?,?,?,?,?,?)
                                         ON CONFLICT(file) DO UPDATE SET offset=excluded.offset, lines=lines+excluded.lines,
                                           stored=stored+excluded.stored, bad=bad+excluded.bad, at=excluded.at""",
                                      (name, start + cut, chunk[:cut].count(b"\n"), n_stored, rep.bad - bad_before, self.now()))
                self.offsets[name] = start + cut
        if rep.lines and not dry_run:  # once per batch the explorer handed off, not per tick: what main took and what it refused
            self.log.write({"event": "explorer_inbox", "files": rep.files, "lines": rep.lines, "stored": rep.stored,
                            "updated": rep.updated, "tainted": rep.tainted, "bad": rep.bad,
                            "dropped_taint": rep.dropped_taint, "reasons": rep.reasons})
        return rep


def read_inbox(conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path, *,
               now: Callable[[], float] = time.time, dry_run: bool = False) -> InboxReport:
    """One read with a reader of its own (the ``explore inbox`` command); the daemon keeps an ``Inbox``."""
    return Inbox(conn, policy, home, now=now).read(cfg, dry_run=dry_run)
