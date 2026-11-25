"""``ninshubur forget`` and the retention sweep: what is erased, what is listed, what a later crawl may store."""
from __future__ import annotations

import json

import pytest

from ninshubur import erase
from ninshubur.__main__ import main
from ninshubur.analyze.suggest import Suggester
from ninshubur.companies import Resolver
from ninshubur.db import one, rows
from ninshubur.logs import read_jsonl
from ninshubur.profiles import load_profiles
from tests.conftest import NOW, SAMPLE_ROUTES, Env, grep_home, write_config, write_profiles

DAY = 86400.0


@pytest.fixture
def env(home):
    write_config(home)
    write_profiles(home)
    e = Env(home, dict(SAMPLE_ROUTES))
    e.run_once()
    e.profiles = load_profiles(home)
    e.suggester = Suggester(e.conn, e.policy, e.cfg, e.profiles, now=e.clock.now, home=home)
    e.suggester.build_all()
    return e


def _recrawl(env):
    """A crawl that parses every page again, as when the pages changed."""
    env.conn.execute("DELETE FROM fetch_state")
    env.conn.execute("UPDATE sources SET state='{}'")  # event pages are read once per event otherwise
    env.run_once(force=True)


def _cells(conn, needle):
    """(table, column) of every database cell that contains the needle."""
    hits = set()
    for t in [r["name"] for r in rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")]:
        for r in rows(conn, f"SELECT * FROM {t}"):  # noqa: S608 — table and column names come from the test's own literals or sqlite_master
            hits |= {(t, k) for k, v in r.items() if isinstance(v, str) and needle in v}
    return hits


def _names(conn, etype):
    return {r["name"] for r in rows(conn, "SELECT name FROM entities WHERE type=?", (etype,))}


def _note(conn, text):
    """A suggestion text and a feedback note that name someone, as a person working the list would leave them."""
    sid = one(conn, "SELECT id FROM suggestions WHERE kind='cfp_submit' ORDER BY id")["id"]
    conn.execute("UPDATE suggestions SET steps=? WHERE id=?", (json.dumps([f"Ask {text} for an intro", "Send the note"]), sid))
    conn.execute("INSERT INTO feedback(suggestion_id, at, status, reason) VALUES (?,?,?,?)", (sid, NOW, "accepted", f"wrote to {text}, no reply"))
    return sid


# --- a person ------------------------------------------------------------------------------------------
def test_forget_person_removes_the_record_its_links_and_the_name_in_written_texts(env, home):
    built = erase.find(env.conn, "person", "Jane Doe").texts  # suggestions the builder wrote that name her
    sid = _note(env.conn, "Jane Doe")
    jane = one(env.conn, "SELECT id FROM entities WHERE name='Jane Doe'")
    assert jane and one(env.conn, "SELECT COUNT(*) AS n FROM item_entities WHERE entity_id=?", (jane["id"],))["n"] >= 1
    others = _names(env.conn, "person") - {"Jane Doe"}

    t = erase.find(env.conn, "person", "  jane   DOE ")  # case and spacing don't matter
    assert [e["name"] for e in t.entities] == ["Jane Doe"] and t.links >= 1 and t.texts == built + 2 and t.stored
    assert _names(env.conn, "person") == others | {"Jane Doe"}  # find reads only

    done = erase.forget(env.conn, t, NOW, env.ingestor.policy_log)
    assert done["entities"] == 1 and done["links"] >= 1 and done["texts"] == built + 2 and done["listed"] == 1
    assert _names(env.conn, "person") == others
    assert one(env.conn, "SELECT COUNT(*) AS n FROM item_entities WHERE entity_id=?", (jane["id"],))["n"] == 0
    assert json.loads(one(env.conn, "SELECT steps FROM suggestions WHERE id=?", (sid,))["steps"])[0] == "Ask [removed] for an intro"
    assert one(env.conn, "SELECT reason FROM feedback WHERE suggestion_id=?", (sid,))["reason"] == "wrote to [removed], no reply"
    # the log says that it happened, with a hash and counts; the name is in no file under the data home,
    # not in a free database page and not in the write-ahead log
    line = [r for r in read_jsonl(home / "logs" / "policy.jsonl") if r.get("event") == "forget"][-1]
    assert line["type"] == "person" and len(line["mark"]) == 32 and line["entities"] == 1
    assert grep_home(home, "Jane Doe") == [] and grep_home(home, "janedoe") == []


def test_a_forgotten_person_is_not_stored_by_the_next_crawl_until_lifted(env):
    erase.forget(env.conn, erase.find(env.conn, "person", "Jane Doe"), NOW)
    before = _names(env.conn, "person")
    _recrawl(env)
    assert "Jane Doe" not in _names(env.conn, "person") and _names(env.conn, "person") == before
    assert erase.Forgotten(env.conn).has("person", name="JANE DOE") and not erase.Forgotten(env.conn).has("person", name="Ada Lovelace")
    # the list holds no name, only hashes made with this database's salt
    assert all(len(r["mark"]) == 32 and "jane" not in r["mark"] for r in rows(env.conn, "SELECT mark FROM forgotten"))
    assert erase.lift(env.conn, "person", "jane doe") == 1 and erase.lift(env.conn, "person", "jane doe") == 0
    _recrawl(env)
    assert "Jane Doe" in _names(env.conn, "person")


def test_a_name_is_only_replaced_as_a_whole_word_and_short_names_are_left_alone(env):
    sid = _note(env.conn, "Ada Lovelace")
    env.conn.execute("UPDATE suggestions SET title='Adalovelace Summit: Ada Lovelace, ADA LOVELACE and Al' WHERE id=?", (sid,))
    erase.forget(env.conn, erase.find(env.conn, "person", "Ada Lovelace"), NOW)
    assert one(env.conn, "SELECT title FROM suggestions WHERE id=?", (sid,))["title"] == "Adalovelace Summit: [removed], [removed] and Al"
    t = erase.find(env.conn, "person", "Al")
    assert t.texts == 0 and not t.stored  # two letters: listed as forgotten, never searched for in texts


def test_forget_person_counts_crawled_mentions_and_leaves_them(env):
    item = one(env.conn, "SELECT id, title FROM items WHERE kind='hiring' ORDER BY id")
    env.conn.execute("UPDATE items SET title=? WHERE id=?", ("Engineer, reports to Jane Doe", item["id"]))
    t = erase.find(env.conn, "person", "Jane Doe")
    assert t.crawled_mentions == 1
    erase.forget(env.conn, t, NOW)
    assert one(env.conn, "SELECT title FROM items WHERE id=?", (item["id"],))["title"] == "Engineer, reports to Jane Doe"


# --- a company -----------------------------------------------------------------------------------------
def test_forget_company_removes_postings_signals_answers_and_empties_its_suggestion(env, home):
    resolver = Resolver.load(env.cfg, env.profiles, home)
    n_hiring = one(env.conn, "SELECT COUNT(*) AS n FROM items WHERE kind='hiring'")["n"]
    sug = one(env.conn, "SELECT id FROM suggestions WHERE kind='hiring_signal_outreach'")
    other = one(env.conn, "SELECT id, title FROM suggestions WHERE kind='tool_outage'")
    env.conn.execute("INSERT INTO llm_runs(task, profile, slot, model, prompt_hash, started_at) VALUES ('qualify','p','local','m','h',?)", (NOW,))
    env.conn.execute("""INSERT INTO qualifications(run_id, profile, subject_key, subject_name, model, prompt_hash, evidence_hash, status,
                        label, reason, created_at) VALUES (1,'p','company:acme.example','Acme Inc','m','h','e','ok','yes','Acme Inc hires',?)""", (NOW,))
    assert n_hiring >= 1 and sug and one(env.conn, "SELECT COUNT(*) AS n FROM signals WHERE entity_key='company:acme.example'")["n"] >= 1

    for query in ("acme inc", "acme.example", "Acme"):  # a spelling, the domain, the name without its legal form
        t = erase.find(env.conn, "company", query, resolver)
        assert len(t.item_ids) == n_hiring and "company:acme.example" in t.keys and t.suggestions == [sug["id"]], query
        assert t.signals >= 1 and t.qualifications == 1 and t.sources == ["acme-jobs"]
    done = erase.forget(env.conn, t, NOW, env.ingestor.policy_log)
    assert done["items"] == n_hiring and done["suggestions"] == 1 and done["qualifications"] == 1 and done["snapshots"] >= 1

    assert one(env.conn, "SELECT COUNT(*) AS n FROM items WHERE kind='hiring'")["n"] == 0
    for table, col in (("signals", "entity_key"), ("company_aliases", "company_key"), ("qualifications", "subject_key")):
        assert one(env.conn, f"SELECT COUNT(*) AS n FROM {table} WHERE {col}='company:acme.example'")["n"] == 0, table  # noqa: S608 — table and column names come from the test's own literals or sqlite_master
    assert "Acme Inc" not in _names(env.conn, "company") and "Orbit Systems" in _names(env.conn, "company")
    s = one(env.conn, "SELECT * FROM suggestions WHERE id=?", (sug["id"],))
    assert (s["status"], s["title"], s["steps"], s["evidence"], s["entity_keys"], s["item_ids"]) == ("redacted", "[removed]", "[]", "[]", "[]", "[]")
    assert one(env.conn, "SELECT title FROM suggestions WHERE id=?", (other["id"],))["title"] == other["title"]
    # the operator's own config and profile still name the company: found for the preview, never edited
    assert {p.name for p in erase.exported_mentions(home, t.names)} == {"config.yaml", "profiles.yaml"}
    # in the database the name is left in two places, both said in the preview: the mirror of the source's config
    # and the employer field of people who spoke for the company (their records are theirs)
    assert _cells(env.conn, "Acme") == {("sources", "params"), ("entities", "company")} and t.staff_rows >= 1
    assert all(r["type"] == "person" for r in rows(env.conn, "SELECT type FROM entities WHERE company LIKE '%Acme%'"))

    # the board is still in config, so it is fetched; its postings are skipped at storage and the rebuild stays empty
    _recrawl(env)
    env.suggester.build_all()
    assert one(env.conn, "SELECT COUNT(*) AS n FROM items WHERE kind='hiring'")["n"] == 0
    assert one(env.conn, "SELECT COUNT(*) AS n FROM suggestions WHERE kind='hiring_signal_outreach' AND status != 'redacted'")["n"] == 0
    assert "Acme Inc" not in _names(env.conn, "company")
    assert [r for r in read_jsonl(home / "logs" / "policy.jsonl") if r.get("event") == "forgotten_skipped"][-1]["items"] == n_hiring


def test_forget_with_nothing_stored_still_lists_the_name(env):
    t = erase.find(env.conn, "person", "Nobody Here")
    assert not t.stored and not t.already_forgotten
    assert erase.forget(env.conn, t, NOW)["listed"] == 1
    assert erase.find(env.conn, "person", "nobody here").already_forgotten


# --- the sweep -----------------------------------------------------------------------------------------
def test_sweep_removes_people_past_retention_old_snapshots_and_old_quarantine_rows(env, home):
    built = erase.find(env.conn, "person", "Jane Doe").texts
    sid = _note(env.conn, "Jane Doe")
    env.conn.execute("UPDATE entities SET retention_until=? WHERE name='Jane Doe'", (NOW - 1,))
    env.conn.execute("INSERT INTO snapshots(source, taken_at, items_json) VALUES ('gone-source', ?, '[]'), ('gone-source', ?, '[]')",
                     (NOW - 40 * DAY, NOW - 35 * DAY))
    env.conn.execute("INSERT INTO quarantine(ts, source, detector_codes, severity, action) VALUES (?, 's', 'c', 'high', 'dropped_item'), "
                     "(?, 's', 'c', 'high', 'dropped_item')", (NOW - 91 * DAY, NOW - 89 * DAY))
    people = _names(env.conn, "person")
    quarantine = one(env.conn, "SELECT COUNT(*) AS n FROM quarantine")["n"]

    rep = erase.sweep(env.conn, env.policy, NOW, env.ingestor.policy_log)
    assert rep == {"persons": 1, "links": rep["links"], "texts": built + 2, "snapshots": 1, "quarantine": 1} and rep["links"] >= 1
    assert _names(env.conn, "person") == people - {"Jane Doe"}
    assert "Jane Doe" not in one(env.conn, "SELECT steps FROM suggestions WHERE id=?", (sid,))["steps"]
    # the newest snapshot of a source stays, however old: it is the last-good fallback
    assert [r["taken_at"] for r in rows(env.conn, "SELECT taken_at FROM snapshots WHERE source='gone-source'")] == [NOW - 35 * DAY]
    assert one(env.conn, "SELECT COUNT(*) AS n FROM quarantine")["n"] == quarantine - 1
    assert [r for r in read_jsonl(home / "logs" / "policy.jsonl") if r.get("event") == "retention_sweep"][-1]["persons"] == 1
    assert grep_home(home, "Jane Doe") == []
    assert not erase.Forgotten(env.conn).any()  # expiry is not a request: a fresh listing may be stored again
    assert erase.sweep(env.conn, env.policy, NOW) == {"persons": 0, "links": 0, "texts": 0, "snapshots": 0, "quarantine": 0}


def test_the_daemon_and_a_single_run_sweep_once_per_hour(env):
    env.conn.execute("UPDATE entities SET retention_until=? WHERE name='Jane Doe'", (NOW + 1800,))
    assert env.scheduler.retention_sweep() == {"persons": 0, "links": 0, "texts": 0, "snapshots": 0, "quarantine": 0}
    env.clock.advance(3599)
    assert env.scheduler.retention_sweep() is None and "Jane Doe" in _names(env.conn, "person")  # not due yet
    env.clock.advance(1)
    assert env.scheduler.retention_sweep()["persons"] == 1 and "Jane Doe" not in _names(env.conn, "person")
    at, rep = erase.last_sweep(env.conn)
    assert at == env.clock.now() and rep["persons"] == 1


def test_daemon_tick_runs_the_sweep(env):
    import asyncio
    env.conn.execute("UPDATE entities SET retention_until=? WHERE name='Jane Doe'", (NOW - 1,))

    async def one_tick():
        stop = asyncio.Event()
        env.scheduler.after_tick = stop.set
        await env.scheduler.daemon(stop=stop, tick_s=0.01)

    asyncio.run(one_tick())
    assert "Jane Doe" not in _names(env.conn, "person") and erase.last_sweep(env.conn)[0] is not None


# --- the command ---------------------------------------------------------------------------------------
@pytest.fixture
def cli(env, capsys, monkeypatch):
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    env.conn.close()

    def call(*args: str) -> tuple[int, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out + out.err

    return call


def test_cli_previews_then_needs_a_yes(cli, home):
    code, out = cli("forget", "person", "Jane Doe", "--dry-run")
    assert code == 0 and "person record: Jane Doe" in out and "dry run: nothing changed" in out and "namesake" in out
    code, out = cli("forget", "person", "Jane Doe")  # not a terminal, no --yes
    assert code == 2 and "pass --yes" in out
    assert grep_home(home, "Jane Doe") != []
    code, out = cli("forget", "person", "Jane Doe", "--yes")
    assert code == 0 and "erased: 1 record(s)" in out and "no name" in out
    assert grep_home(home, "Jane Doe") == []
    code, out = cli("forget", "person", "Jane Doe", "--dry-run")
    assert code == 0 and "nothing stored matches" in out and "already on the forgotten list" in out
    code, out = cli("status")
    assert code == 0 and "forgotten list: 1 person, 0 company mark(s)" in out
    code, out = cli("forget", "person", "Jane Doe", "--lift")
    assert code == 0 and "off the forgotten list" in out
    assert cli("forget", "person", "Jane Doe", "--lift")[0] == 1


def test_cli_forget_company_names_what_stays(cli, home):
    code, out = cli("forget", "company", "Acme Inc", "--yes")
    assert code == 0 and "hiring posting(s) from acme-jobs" in out and "marked redacted" in out
    assert "config.yaml" in out and "acme-jobs will still be fetched" in out
    assert "Acme" not in (home / "feeds" / "timings.ics").read_text(encoding="utf-8")  # the calendar feed was rebuilt
    assert "person record(s) that name it as their employer" in out
    assert set(grep_home(home, "Acme")) == {"config.yaml", "profiles.yaml", "ninshubur.db"}
