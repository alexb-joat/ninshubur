from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ninshubur.companies import (AliasesError, Resolver, companies, dupes, load_aliases, normalise_domain, normalise_name,
                                 registrable, rollup, split_name)
from ninshubur.config import load_config
from ninshubur.db import connect, j, one, rows
from ninshubur.profiles import load_profiles
from tests.conftest import DEFAULT_SOURCES, NOW, SAMPLES, write_config, write_profiles

DAY = 86400.0


# --- normalisation -------------------------------------------------------------------------------
@pytest.mark.parametrize("raw, norm", [
    ("Acme Robotics", "acme robotics"),
    ("Acme, Inc.", "acme"),
    ("ACME INC", "acme"),
    ("Acme Co., Ltd.", "acme co"),                    # bare "Co" is never stripped
    ("Smith & Co", "smith co"),
    ("Northwind GmbH & Co. KG", "northwind gmbh co kg"),
    ("Northwind Labs S.A.", "northwind labs"),        # dotted forms are unambiguous
    ("Contoso B.V.", "contoso"),
    ("Tidal A.S.", "tidal"),
    ("Contoso BV", "contoso bv"),                     # bare two-letter tokens stay; dupes lists the pair
    ("Fabrikam AG", "fabrikam ag"),
    ("Fabrikam Pty Ltd", "fabrikam"),
    ("Inc", "inc"),                                   # never emptied
    ("Zeta Inc", "zeta"),
    ("Incorporate Labs", "incorporate labs"),         # a legal form only as a whole trailing token
    ("Ｎｏｒｔｈｗｉｎｄ  Labs", "northwind labs"),     # NFKC: full-width letters
    ("Weiß Straße", "weiss strasse"),                 # casefold
    ("Café Lumière", "café lumière"),                 # accents stay: "Cafe" is listed by dupes, not merged
    ("O'Brien & Sons", "obrien sons"),
    ("  Tidal\tWave--Analytics  ", "tidal wave analytics"),
    ("Mixgrid (YC S11)", "mixgrid"),                  # trailing parenthetical dropped
    ("Pangol (YC S26)", "pangol"),
    ("MONUMENTAL", "monumental"),
])
def test_normalise_name(raw, norm):
    assert normalise_name(raw) == norm


def test_acme_and_acme_labs_are_different_names():
    assert normalise_name("Acme") != normalise_name("Acme Labs")


@pytest.mark.parametrize("raw, domain", [
    ("https://www.Acme.example:8443/jobs?x=1", "acme.example"),
    ("acme.example.", "acme.example"),
    ("WWW.NORTHWIND.EXAMPLE", "northwind.example"),
    ("not a domain", None),
    ("acme", None),
    ("vue.js", None),
    ("", None),
])
def test_normalise_domain(raw, domain):
    assert normalise_domain(raw) == domain


@pytest.mark.parametrize("host, key", [
    ("jobs.acme.example", "acme.example"),
    ("shop.acme.co.uk", "acme.co.uk"),
    ("chronoleaf.pe", "chronoleaf.pe"),
    ("acme.com.pe", "acme.com.pe"),          # generic: com/co/org/net/gov/edu/ac before a country code
    ("www.beta.org.rs", "beta.org.rs"),
    ("acme.github.io", "acme.github.io"),    # shared hosts: the customer subdomain is the company
    ("docs.beta.vercel.app", "beta.vercel.app"),
    ("github.io", None),
    ("jobs.lever.co", None),                 # platforms never name the company
    ("boards.greenhouse.io", None),
    ("github.com", None),
    ("t.me", None),
    ("com.pe", None),                        # a bare suffix
    ("co.uk", None),
])
def test_registrable(host, key):
    assert registrable(host) == key


def test_shared_hosts_and_platforms_never_merge_two_companies(world):
    home, _ = world
    assert key_of(home, "hn", "Acme (acme.github.io)")[0] != key_of(home, "hn", "Beta (beta.github.io)")[0]
    assert key_of(home, "hn", "Zorbit (jobs.lever.co)") == ("company:zorbit", "name")
    assert key_of(home, "hn", "Quokka (https://jobs.lever.co/quokka)") == ("company:quokka", "name")
    assert key_of(home, "hn", "Acme (acme.com.pe)")[0] == "company:acme.com.pe" != key_of(home, "hn", "Beta (beta.com.pe)")[0]


def test_only_config_profile_board_and_aliases_are_trusted(world):
    home, _ = world
    (home / "company_aliases.yaml").write_text("Tidal Wave: tidalwave.example\n", encoding="utf-8")
    r = resolver_for(home)

    def res(source, company, board="", **a):
        return r.resolve({"source": source, "attrs": {"company": company, "board": board or company.lower(), **a}})
    assert res("acme-jobs", "Acme Inc", "acme").trusted                      # config board token
    assert res("acme-jobs", "Northwind", "northwind").trusted                # profile board token
    assert res("acme-jobs", "plain", "plain").trusted                        # a board the source is configured to fetch
    assert res("hn", "Tidal Wave").trusted                                   # the aliases file
    evil = res("hn", "Evil Ltd (acme.example)")                              # a crawled post naming a domain
    assert evil.key == "company:acme.example" and not evil.trusted
    assert not res("hn", "Acme Inc").trusted                                 # name match to a configured name
    assert not res("hn", "Nordash.io").trusted and not res("hn", "Quiet Labs").trusted
    assert not res("acme-jobs", "Acme Inc", "acme", domain="acme-own.example").trusted


def test_split_name_reads_domains_in_names():
    assert split_name("Chronoleaf (chronoleaf.pe)") == ("Chronoleaf", "chronoleaf.pe")
    assert split_name("HiQ (hiq.ai)") == ("HiQ", "hiq.ai")
    assert split_name("Mixgrid (YC S11)") == ("Mixgrid", None)
    assert split_name("Nordash.io") == ("Nordash.io", "nordash.io")
    assert split_name("Vue.js Foundation") == ("Vue.js Foundation", None)


# --- fixtures --------------------------------------------------------------------------------------
SOURCES = {
    "acme-jobs": {**DEFAULT_SOURCES["acme-jobs"], "params": {
        "boards": ["acme", "northwind", "plain"],
        "companies": {"acme": {"name": "Acme Inc", "domain": "acme.example"}, "northwind": {"name": "Northwind"}}}},
    "hn": DEFAULT_SOURCES["hn"],
}
PROFILES = {"agency-eu": {
    "keywords": ["outbound"], "titles": ["Head of Growth"],
    "target_companies": [{"name": "Northwind Traders", "domain": "northwind.example", "greenhouse": "northwind"},
                         {"name": "Contoso Ltd", "domain": "contoso.example"}]}}


@pytest.fixture
def world(home):
    write_config(home, SOURCES)
    write_profiles(home, PROFILES)
    conn = connect(home)
    return home, conn


def resolver_for(home: Path) -> Resolver:
    from ninshubur.policy import Policy
    policy = Policy.load(home)
    return Resolver.load(load_config(home, policy=policy), load_profiles(home), home)


_uid = iter(range(1, 10_000))


def add_item(conn: sqlite3.Connection, source: str, company: str, *, board: str = "", title: str = "Account Executive",
             at: float = NOW - DAY, first_seen: float | None = None, held: int = 0, tainted: int = 0, person_data: int = 0,
             **attrs) -> int:
    n = next(_uid)
    cur = conn.execute(
        """INSERT INTO items(uid, source, kind, title, url, published_at, first_seen_at, last_seen_at, attrs, legal_basis,
           person_data, tainted, held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"t{n}", source, "hiring", title, f"https://jobs.example/{n}", at, first_seen if first_seen is not None else at + n,
         NOW, j({"company": company, "board": board or company.lower(), "job_id": n, **attrs}), "public_business_info",
         person_data, tainted, held))
    return int(cur.lastrowid)


def key_of(home: Path, source: str, company: str, board: str = "", **attrs) -> tuple[str, str]:
    r = resolver_for(home).resolve({"source": source, "attrs": {"company": company, "board": board or company.lower(), **attrs}})
    assert r is not None
    return r.key, r.origin


# --- merge order -----------------------------------------------------------------------------------
def test_step1_own_domain_beats_config(world):
    home, _ = world
    assert key_of(home, "acme-jobs", "Acme Inc", "acme", domain="https://acme-own.example/") == ("company:acme-own.example", "domain")
    assert key_of(home, "hn", "Chronoleaf (chronoleaf.pe)") == ("company:chronoleaf.pe", "domain")
    assert key_of(home, "hn", "HiQ (hiq.ai)") == ("company:hiq.ai", "domain")
    assert key_of(home, "hn", "Nordash.io") == ("company:nordash.io", "domain")


def test_step2_config_then_profile_beat_the_aliases_file(world):
    home, _ = world
    (home / "company_aliases.yaml").write_text('"Acme Inc": other.example\n"Contoso": other.example\n', encoding="utf-8")
    assert key_of(home, "acme-jobs", "Acme Inc", "acme") == ("company:acme.example", "config")  # board token
    assert key_of(home, "hn", "ACME, Inc.") == ("company:acme.example", "config")               # exact normalised name
    assert key_of(home, "acme-jobs", "Northwind", "northwind") == ("company:northwind.example", "profile")  # board, config has no domain
    assert key_of(home, "hn", "Contoso") == ("company:contoso.example", "profile")             # name: "Contoso Ltd" -> "contoso"
    # the board token only counts for the adapter it belongs to: an HN item with board "acme" is not that board
    assert key_of(home, "hn", "Acme Robotics", "acme") == ("company:acme-robotics", "name")


def test_step3_aliases_file_beats_the_name(world):
    home, _ = world
    (home / "company_aliases.yaml").write_text("Tidal Wave: tidalwave.example\nNorthwnd Labs: Northwind Labs\n", encoding="utf-8")
    assert key_of(home, "hn", "Tidal Wave Inc.") == ("company:tidalwave.example", "manual")
    assert key_of(home, "hn", "Northwnd Labs") == ("company:northwind-labs", "manual")
    assert key_of(home, "hn", "Northwind Labs") == ("company:northwind-labs", "name")


def test_step4_normalised_name(world):
    home, _ = world
    assert key_of(home, "hn", "Quiet Labs") == ("company:quiet-labs", "name")
    assert key_of(home, "hn", "MONUMENTAL") == key_of(home, "hn", "Monumental") == ("company:monumental", "name")
    assert key_of(home, "hn", "Mixgrid (YC S11)") == ("company:mixgrid", "name")
    assert key_of(home, "acme-jobs", "plain", "plain") == ("company:plain", "name")


def test_profile_target_keys_match_item_keys(world):
    home, _ = world
    r = resolver_for(home)
    targets = load_profiles(home)["agency-eu"].target_companies
    assert [r.key_for_target(t) for t in targets] == ["company:northwind.example", "company:contoso.example"]


def test_conflicting_domains_for_one_name_are_not_merged_by_name(home):
    write_config(home, {"acme-jobs": {**DEFAULT_SOURCES["acme-jobs"], "params": {"boards": ["a", "b"], "companies": {
        "a": {"name": "Acme Inc", "domain": "acme.example"}, "b": {"name": "Acme", "domain": "acme-eu.example"}}}},
        "hn": DEFAULT_SOURCES["hn"]})
    r = resolver_for(home)
    assert r.resolve({"source": "hn", "attrs": {"company": "Acme"}}).key == "company:acme"
    assert any("2 domains" in w for w in r.warnings)


# --- no fuzzy merge, dupes -------------------------------------------------------------------------
def test_near_miss_names_stay_separate_and_are_listed(world):
    home, conn = world
    add_item(conn, "hn", "Northwind Labs")
    add_item(conn, "hn", "Northwind Labs")
    add_item(conn, "hn", "Northwnd Labs")
    add_item(conn, "hn", "Quiet")
    add_item(conn, "hn", "Quiet Labs")
    add_item(conn, "hn", "Nordash.io")
    add_item(conn, "hn", "Nordash")
    add_item(conn, "hn", "Abco")
    add_item(conn, "hn", "Abcd")
    add_item(conn, "hn", "Fabrikam")
    add_item(conn, "hn", "Fabrikam AG")
    found = companies(conn, resolver_for(home))
    keys = {c.key for c in found}
    assert {"company:northwind-labs", "company:northwnd-labs", "company:quiet", "company:quiet-labs",
            "company:nordash.io", "company:nordash", "company:abco", "company:abcd"} <= keys
    pairs = {(d.a.key, d.b.key): d for d in dupes(found)}
    typo = pairs[("company:northwnd-labs", "company:northwind-labs")]  # fewer postings is the alias side
    assert typo.distance == 1 and typo.alias_line == "Northwnd Labs: Northwind Labs"
    quiet = pairs.get(("company:quiet", "company:quiet-labs")) or pairs[("company:quiet-labs", "company:quiet")]
    assert quiet.why == "'labs' added" and quiet.alias_line == "Quiet Labs: Quiet"  # a tie: the later key is the alias
    nordash = pairs[("company:nordash", "company:nordash.io")]
    assert nordash.distance == 0 and nordash.alias_line == "Nordash: nordash.io"
    assert not any({"company:abco", "company:abcd"} == {a, b} for a, b in pairs)
    ag = pairs.get(("company:fabrikam-ag", "company:fabrikam")) or pairs[("company:fabrikam", "company:fabrikam-ag")]
    assert ag.why == "'ag' added" and ag.alias_line in ("Fabrikam AG: Fabrikam", "Fabrikam: Fabrikam AG")


def test_dupes_prefilter_matches_every_pair_comparison():
    import random
    from ninshubur.companies import CompanySummary, _limit, _names
    from ninshubur.detectors import edit_distance
    rnd = random.Random(7)
    words = ["acme", "acne", "northwind", "northwnd", "quiet", "quite", "labs", "ag", "zeta", "zetta", "tidal", "tidl"]
    found = []
    for i in range(120):
        name = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 2)))
        found.append(CompanySummary(key=f"company:k{i}", name=name, domain=None, origin="name", postings=1, spellings={name: 1}))
    got = {(d.a.key, d.b.key) if d.a.key < d.b.key else (d.b.key, d.a.key): d.distance for d in dupes(found)}
    want = {}
    for i, a in enumerate(found):
        for b in found[i + 1:]:
            for x in _names(a):
                for y in _names(b):
                    s_, l_ = sorted((x, y), key=len)
                    if x == y:
                        d = 0
                    elif len(s_) >= 3 and l_.startswith(s_ + " ") and l_[len(s_) + 1:] in ("labs", "ag"):
                        d = 1
                    else:
                        lim = _limit(min(len(x), len(y)))
                        d = edit_distance(x, y, lim) if lim else 99
                        d = d if d <= lim else 99
                    if d < 99:
                        k = (a.key, b.key) if a.key < b.key else (b.key, a.key)
                        want[k] = min(want.get(k, 99), d)
    assert got == want  # 4 letters: only identical names pair


def test_alias_line_from_dupes_merges_the_pair(world):
    home, conn = world
    add_item(conn, "hn", "Nordash.io")
    add_item(conn, "hn", "Nordash")
    d = next(d for d in dupes(companies(conn, resolver_for(home))) if d.a.key == "company:nordash")
    (home / "company_aliases.yaml").write_text(d.alias_line + "\n", encoding="utf-8")
    assert [c.key for c in companies(conn, resolver_for(home))] == ["company:nordash.io"]


def test_dupes_writes_nothing(world):
    home, conn = world
    add_item(conn, "hn", "Northwind Labs")
    add_item(conn, "hn", "Northwnd Labs")
    before = conn.total_changes
    listing = sorted(p.name for p in home.rglob("*"))
    assert dupes(companies(conn, resolver_for(home)))
    assert conn.total_changes == before and sorted(p.name for p in home.rglob("*")) == listing


def test_display_name_is_deterministic_and_stable(world):
    home, conn = world
    add_item(conn, "hn", "PANGOL", first_seen=NOW - 9 * DAY)
    add_item(conn, "hn", "Pangol (YC S26)", first_seen=NOW - 5 * DAY)
    add_item(conn, "hn", "Pangol", first_seen=NOW - 4 * DAY)
    add_item(conn, "hn", "Zeta Works", first_seen=NOW - 3 * DAY)  # a tie: the earliest seen spelling wins
    add_item(conn, "hn", "ZETA WORKS", first_seen=NOW - 2 * DAY)
    add_item(conn, "acme-jobs", "acme", board="acme")             # config name first, whatever the spelling
    runs = [{c.key: c.name for c in companies(conn, resolver_for(home))} for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0]["company:pangol"] == "Pangol" and runs[0]["company:zeta-works"] == "Zeta Works"
    assert runs[0]["company:acme.example"] == "Acme Inc"


# --- rollup ------------------------------------------------------------------------------------------
def _no_entities(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_READ and arg1 == "entities":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def test_rollup_is_idempotent_and_leaves_out_tainted_held_and_person_rows(world, policy):
    home, conn = world
    ok1 = add_item(conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - 2 * DAY)
    ok2 = add_item(conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - 3 * DAY)
    hn = add_item(conn, "hn", "Quiet Labs")
    tainted = add_item(conn, "hn", "Evil Corp", tainted=1)
    held = add_item(conn, "hn", "Held Labs", held=1)
    person = add_item(conn, "hn", "Person Labs", person_data=1)
    conn.execute("""INSERT INTO entities(type, key, name, source, legal_basis, fetched_at) VALUES
                    ('person', 'person:x', 'Jane Example', 'hn', 'public_business_info', ?)""", (NOW,))
    r = resolver_for(home)
    conn.set_authorizer(_no_entities)  # rollup must not even read the entities table
    try:
        rep = rollup(conn, r, policy, NOW)
        assert (rep.signals, rep.written, rep.removed) == (3, 3, 0)
        again = rollup(conn, r, policy, NOW)
        assert (again.signals, again.written, again.removed) == (3, 0, 0)
    finally:
        conn.set_authorizer(None)
    sig = rows(conn, "SELECT * FROM signals ORDER BY item_id")
    assert [s["item_id"] for s in sig] == [ok1, ok2, hn]
    assert {s["entity_key"] for s in sig} == {"company:acme.example", "company:quiet-labs"}
    assert all(s["kind"] == "hiring" and 0 < s["confidence"] <= 1 and s["window_start"] > s["at"] for s in sig)
    assert not {tainted, held, person} & {s["item_id"] for s in sig}
    aliases = {a["alias"]: (a["company_key"], a["origin"]) for a in rows(conn, "SELECT * FROM company_aliases")}
    assert aliases["board:greenhouse:acme"] == ("company:acme.example", "config")
    assert aliases["name:quiet-labs"] == ("company:quiet-labs", "name")
    assert "name:person-labs" not in aliases and "name:evil-corp" not in aliases
    created = one(conn, "SELECT created_at FROM company_aliases WHERE alias='name:quiet-labs'")["created_at"]
    # an item that became tainted, and one that is gone, lose their rows on the next rollup
    conn.execute("UPDATE items SET tainted=1 WHERE id=?", (ok2,))
    conn.execute("DELETE FROM items WHERE id=?", (hn,))
    rep = rollup(conn, r, policy, NOW + DAY)
    assert rep.removed == 2 and [s["item_id"] for s in rows(conn, "SELECT item_id FROM signals")] == [ok1]
    assert one(conn, "SELECT COUNT(*) AS n FROM company_aliases WHERE alias='name:quiet-labs'")["n"] == 0
    add_item(conn, "hn", "Quiet Labs")
    rollup(conn, r, policy, NOW + 2 * DAY)
    assert one(conn, "SELECT created_at FROM company_aliases WHERE alias='name:quiet-labs'")["created_at"] == NOW + 2 * DAY
    assert created == NOW


def test_same_second_postings_share_one_signal(world, policy):
    home, conn = world
    a = add_item(conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY)
    add_item(conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY)
    rollup(conn, resolver_for(home), policy, NOW)
    assert [s["item_id"] for s in rows(conn, "SELECT item_id FROM signals")] == [a]


# --- aliases file ------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, line, words", [
    ("Acme Labs:\n  - acme.example\n", 1, "maps to a sequence"),
    ("Acme Labs: acme.example\nQuiet:\n", 2, "maps to nothing"),
    ("Acme Labs: acme.example\n  bad: [\n", 2, "not valid YAML"),
    ("- Acme Labs\n", 1, "must be a mapping"),
    ("Acme Labs: acme.example\nACME LABS, Inc.: other.example\n", 2, "same name as line 1"),
    ("'...': acme.example\n", 1, "empty once punctuation"),
])
def test_malformed_aliases_file_names_file_line_and_fix(tmp_path, text, line, words):
    p = tmp_path / "company_aliases.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(AliasesError) as e:
        load_aliases(p)
    msg = str(e.value)
    assert msg.startswith(f"{p}:") and words in msg and (f":{line}:" in msg)
    assert "acme.example" in msg or "keep one" in msg or "write" in msg  # the fix


def test_absent_or_empty_aliases_file_is_fine(tmp_path):
    assert load_aliases(tmp_path / "company_aliases.yaml") == {}
    (tmp_path / "company_aliases.yaml").write_text("# none yet\n", encoding="utf-8")
    assert load_aliases(tmp_path / "company_aliases.yaml") == {}


# --- CLI -------------------------------------------------------------------------------------------------
@pytest.fixture
def cli(home, capsys, monkeypatch):
    from ninshubur.__main__ import main
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home)
    write_profiles(home)

    def call(*args: str) -> tuple[int, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out + out.err

    return call


def test_cli_companies_and_dupes(cli, home):
    code, out = cli("companies")
    assert code == 0 and out.startswith("no companies yet")
    code, out = cli("run", "--replay", str(SAMPLES))
    assert code == 0
    code, out = cli("companies")
    lines = out.splitlines()
    assert code == 0 and lines[0].split() == ["KEY", "NAME", "POSTINGS", "HELD", "FIRST", "LAST", "SOURCES"]
    assert lines[1].split() == ["company:acme.example", "Acme", "Inc", "3", "0", "2026-09-20", "2026-10-01", "acme-jobs"]
    code, out = cli("companies", "--json")
    data = json.loads(out)
    assert code == 0 and data[0]["key"] == "company:acme.example" and data[0]["origin"] == "config"
    code, out = cli("companies", "--dupes")
    assert code == 0 and out.startswith("no near-miss names among 1 companies")
    conn = connect(home)
    add_item(conn, "hn", "Acme Robotics")  # not merged: listed, with the line that would merge it
    add_item(conn, "hn", "Nordash.io")
    add_item(conn, "hn", "Nordash")
    conn.close()
    code, out = cli("companies", "--dupes")
    assert code == 0 and "2 near-miss pair(s) among 4 companies" in out
    assert "Nordash (company:nordash, 1)  ~  Nordash.io (company:nordash.io, 1)  [same name]" in out
    assert "add to company_aliases.yaml if they are one company:  Nordash: nordash.io" in out
    assert "Acme Robotics: acme.example" in out and str(home / "company_aliases.yaml") in out
    code, out = cli("companies", "--dupes", "--json")
    assert code == 0 and {d["alias_line"] for d in json.loads(out)} == {"Nordash: nordash.io", "Acme Robotics: acme.example"}


def test_malformed_aliases_file_warns_keeps_working_and_fails_check(cli, home):
    (home / "company_aliases.yaml").write_text("Acme Labs: [a, b]\n", encoding="utf-8")
    code, out = cli("companies")
    assert code == 0 and "company_aliases.yaml:1:" in out and "maps to a sequence" in out
    assert out.count("maps to a sequence") == 2  # once on stderr at start-up, once above the table
    code, out = cli("run", "--replay", str(SAMPLES))
    assert code == 0 and "suggestions:" in out
    code, out = cli("check")
    assert code == 1 and "aliases: " in out and "maps to a sequence" in out
    log = (home / "logs" / "policy.jsonl").read_text(encoding="utf-8")
    assert '"aliases_error"' in log


def test_a_broken_aliases_file_keeps_the_last_good_aliases(world, policy):
    from ninshubur.analyze.suggest import Suggester
    home, conn = world
    p = home / "company_aliases.yaml"
    p.write_text("Tidal Wave: tidalwave.example\n", encoding="utf-8")
    sug = Suggester(conn, policy, load_config(home, policy=policy), load_profiles(home), now=lambda: NOW, home=home)
    item = {"source": "hn", "attrs": {"company": "Tidal Wave"}}
    assert sug.resolver.resolve(item).key == "company:tidalwave.example"
    import os
    p.write_text("Tidal Wave: [x]\n", encoding="utf-8")
    os.utime(p, (NOW + 5, NOW + 5))
    assert sug.refresh_resolver().resolve(item).key == "company:tidalwave.example" and "sequence" in sug.aliases_error
    p.write_text("Tidal Wave: other.example\n", encoding="utf-8")
    os.utime(p, (NOW + 10, NOW + 10))
    assert sug.refresh_resolver().resolve(item).key == "company:other.example" and sug.aliases_error is None
