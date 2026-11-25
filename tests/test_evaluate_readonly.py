"""`ninshubur evaluate` reads the database and never writes it: not the file, not its sidecars, not its meta rows, and it
never creates or migrates one. A writer holding the database open (the daemon) is neither blocked nor disturbed."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from ninshubur import labelsheet as L
from ninshubur.db import connect, connect_readonly, db_path
from tests.test_evaluate import N, RATER, TRUTH, copy_with, sheet, store  # noqa: F401  (sheet is a fixture)
from tests.test_qualify import cli  # noqa: F401  (a fixture)

SIDECARS = ("-wal", "-shm", "-journal")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sidecars(path: Path) -> set[str]:
    return {s for s in SIDECARS if Path(str(path) + s).exists()}


def stored_db(home, sheet_path, *, close: bool = True):
    """A database `connect()` wrote, with one local model's answers that match the sheet's truth."""
    conn = connect(home)
    store(conn, L.read_sheet(sheet_path), {i: ("ok", TRUTH[i]) for i in range(1, N + 1)})
    if close:
        conn.close()
        return None
    return conn


def meta_rows(home) -> list[tuple[str, str]]:
    conn = connect_readonly(home)
    try:
        return [tuple(r) for r in conn.execute("SELECT key, value FROM meta ORDER BY key")]
    finally:
        conn.close()


def test_a_closed_database_is_byte_identical_after_evaluate(cli, home, sheet):  # noqa: F811
    stored_db(home, sheet)
    path = db_path(home)
    before, side, meta = sha(path), sidecars(path), meta_rows(home)
    assert side == set()  # `connect()` closed cleanly: nothing beside the file, so nothing may appear
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save", "--reps", "200")
    assert code == 0 and "local-3b" in out  # the stored answers were really read
    assert sha(path) == before and sidecars(path) == side and meta_rows(home) == meta
    assert sorted(p.name for p in home.glob("ninshubur.db*")) == ["ninshubur.db"]


def test_a_database_another_connection_holds_open_is_neither_blocked_nor_changed(cli, home, sheet):  # noqa: F811
    writer = stored_db(home, sheet, close=False)  # the daemon: WAL mode, still open
    path = db_path(home)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO meta(key, value) VALUES ('pending', 'x')")  # a write in flight
    before, side = sha(path), sidecars(path)
    assert {"-wal", "-shm"} <= side
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save", "--reps", "200")
    assert code == 0 and "local-3b" in out
    assert sha(path) == before and sidecars(path) == side
    assert ("pending", "x") not in meta_rows(home)  # the reader sees what was committed, not the write in flight
    writer.execute("COMMIT")  # the evaluation did not hold the writer up
    assert ("pending", "x") in meta_rows(home)
    writer.close()


def test_no_database_still_scores_a_rater_file_and_creates_none(cli, home, sheet):  # noqa: F811
    path = db_path(home)
    assert not path.exists()
    rater = copy_with(home, sheet, RATER)
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save", "--reps", "200", "--rater", f"{rater}=second")
    assert code == 0 and "| second |" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save")
    assert code == 2 and "nothing to score" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--model", "ghost")
    assert code == 2 and "no stored answers on this sheet's evidence for ghost" in out
    assert not path.exists() and not list(home.glob("ninshubur.db*"))


def test_a_database_older_than_the_tables_it_reads_is_refused_not_migrated(cli, home, sheet):  # noqa: F811
    conn = connect(home)
    conn.execute("DROP TABLE qualifications")
    conn.execute("DROP TABLE llm_runs")
    conn.close()
    path = db_path(home)
    before = sha(path)
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save")
    assert code == 2 and "database is older than this build; run `ninshubur status` once to migrate it" in out
    assert "Traceback" not in out
    assert sha(path) == before and sidecars(path) == set()
    names = {r[0] for r in connect_readonly(home).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_runs" not in names and "qualifications" not in names  # still not migrated


@pytest.mark.parametrize("live", [False, True], ids=["file at rest", "held open by a writer"])
def test_a_write_through_the_read_only_connection_raises(home, live):
    writer = connect(home)
    writer.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")
    if not live:
        writer.close()
    path = db_path(home)
    before, side = sha(path), sidecars(path)
    conn = connect_readonly(home)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()["value"] == "v"  # rows by name
        for stmt in ("INSERT INTO meta(key, value) VALUES ('z', '1')", "DELETE FROM meta", "DROP TABLE meta",
                     "CREATE TABLE t(x)", "UPDATE meta SET value='w'"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(stmt)
    finally:
        conn.close()
    assert sha(path) == before and sidecars(path) == side
    check = connect_readonly(home)
    assert [tuple(r) for r in check.execute("SELECT key, value FROM meta WHERE key IN ('k', 'z')")] == [("k", "v")]
    check.close()
    if live:
        writer.close()


def test_it_never_creates_migrates_or_chmods(home):
    path = db_path(home)
    with pytest.raises(FileNotFoundError):
        connect_readonly(home)
    assert not path.exists() and not list(home.glob("ninshubur.db*"))
    raw = sqlite3.connect(str(path))  # a database nobody migrated: one table, no schema_version
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.commit()
    raw.close()
    if os.name == "posix":
        os.chmod(path, 0o644)
    before = sha(path)
    conn = connect_readonly(path=path)
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master")} == {"meta", "sqlite_autoindex_meta_1"}
    conn.close()
    assert sha(path) == before and not sidecars(path)
    if os.name == "posix":
        assert oct(os.stat(path).st_mode & 0o777) == "0o644"  # `connect()` would have made it 0600
