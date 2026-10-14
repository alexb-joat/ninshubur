from __future__ import annotations

from pathlib import Path

import pytest

from ninshubur.parse.htmltree import parse_html
from ninshubur.parse.people import extract, looks_like_name, normalise_handle, split_title_company
from ninshubur.schemas import SCHEMAS, project
from tests.conftest import NOW

S = Path(__file__).parent / "samples" / "event_sites"
M = Path(__file__).parent / "malicious" / "event_sites"


def run(name: str, url: str = "https://event.example/speakers"):
    doc = parse_html((S / name if (S / name).exists() else M / name).read_bytes())
    return extract(doc, url)


def projected(ext):
    pp = project(ext.persons, SCHEMAS["event_person.v1"], NOW)
    po = project(ext.orgs, SCHEMAS["event_org.v1"], NOW)
    return pp, po


@pytest.mark.parametrize("href,handle", [
    ("https://www.linkedin.com/in/janedoe/?originalSubdomain=de", "https://www.linkedin.com/in/janedoe"),
    ("https://linkedin.com/in/john-q-public", "https://www.linkedin.com/in/john-q-public"),
    ("https://de.linkedin.com/in/maxweber-42", "https://www.linkedin.com/in/maxweber-42"),
    ("https://www.linkedin.com/company/acme", None),
    ("https://evil.example/in/x", None),
    ("https://www.linkedin.com/in/<script>", None),
    (None, None),
])
def test_normalise_handle(href, handle):
    assert normalise_handle(href) == handle


@pytest.mark.parametrize("s,ok", [
    ("Jane Doe", True), ("Zoë O'Brien-Müller", True), ("Dr. Max Weber", True), ("John Q. Public", True),
    ("Keynote TBA", True), ("Jane", False), ("jane doe", False), ("Jane Doe 2", False), ("a@b.c Doe", False),
    ("https://x.example Doe", False), ("One Two Three Four Five Six", False), ("Pаul Smith", True),  # homoglyph passes here; taint catches it
])
def test_looks_like_name(s, ok):
    assert looks_like_name(s) is ok


@pytest.mark.parametrize("line,expected", [
    ("Head of Growth at Rocket Labs", ("Head of Growth", "Rocket Labs")),
    ("CEO, Public Ventures", ("CEO", "Public Ventures")),
    ("Sales Director | Alpine SaaS AG", ("Sales Director", "Alpine SaaS AG")),
    ("Alpine SaaS AG | Sales Director", ("Sales Director", "Alpine SaaS AG")),
    ("Professor", ("Professor", None)),
    ("Uni Example", (None, "Uni Example")),
    ("", (None, None)),
])
def test_split_title_company(line, expected):
    assert split_title_company(line) == expected


def test_jsonld_layer():
    ext = run("jsonld.html", "https://expo.example/")
    assert ext.person_layer == "jsonld"
    names = {p["name"]: p for p in ext.persons}
    assert set(names) == {"Ada Lovelace", "Grace Hopper", "Linus B"}
    assert names["Ada Lovelace"]["handle"] == "https://www.linkedin.com/in/ada-lovelace" and names["Ada Lovelace"]["company"] == "Analytical Engines"
    assert names["Linus B"]["handle"] == "https://www.linkedin.com/in/linus-b" and names["Linus B"]["company"] == "Kernel GmbH"
    orgs = {o["name"]: o for o in ext.orgs}
    assert orgs["Acme Inc"]["role"] == "sponsor" and orgs["Acme Inc"]["domain"] == "acme.example"
    assert orgs["Expo Org"]["role"] == "organizer"
    assert ext.contact_data == 2  # email + phone counted
    pp, po = projected(ext)
    assert len(pp.rows) == 3 and len(po.rows) == 3 and pp.violations == 0
    assert "hello@expo.example" not in str(pp.rows) + str(po.rows)


def test_microdata_layer_and_sponsor_section():
    ext = run("microdata.html")
    assert ext.person_layer == "microdata" and [p["name"] for p in ext.persons] == ["Marie Curie", "Alan Turing", "Katherine Johnson"]
    assert ext.persons[0]["company"] == "Radium Labs" and ext.persons[0]["handle"] == "https://www.linkedin.com/in/mcurie"
    orgs = {o["name"]: o for o in ext.orgs}
    assert orgs["Orbit Systems"]["domain"] == "orbit.example" and orgs["Orbit Systems"]["role"] == "sponsor"
    assert orgs["Vector Co"]["domain"] == "vector.example"
    assert ("orbit.example", "https://orbit.example/") in ext.external_hosts


def test_cards_layer_with_linkedin():
    ext = run("cards_linkedin.html")
    assert ext.person_layer == "cards"
    by = {p["name"]: {k: v for k, v in p.items() if k != "_pretaint"} for p in ext.persons}
    assert by["Jane Doe"] == {"name": "Jane Doe", "title": "Head of Growth", "company": "Rocket Labs",
                              "handle": "https://www.linkedin.com/in/janedoe", "role": "speaker", "layer": "cards"}
    assert by["John Q. Public"]["company"] == "Public Ventures" and by["John Q. Public"]["handle"] == "https://www.linkedin.com/in/john-q-public"
    assert by["Zoë O'Brien-Müller"]["title"] == "Sales Director" and by["Zoë O'Brien-Müller"]["company"] == "Alpine SaaS AG"
    assert by["Dr. Max Weber"]["title"] == "Professor" and by["Dr. Max Weber"]["company"] == "Uni Example"
    assert "Keynote TBA" in by  # a placeholder card still parses; humans dismiss it
    orgs = {o["name"]: o for o in ext.orgs}
    assert orgs["BigCloud"]["domain"] == "bigcloud.example" and orgs["BigCloud"]["role"] == "partner"
    assert orgs["Tiny Tools"]["domain"] == "tiny.example"
    assert orgs["Local Exhibitor"]["domain"] is None
    assert orgs["Should Not Use LI As Domain"]["domain"] is None
    assert not any(h[0].endswith("facebook.com") for h in ext.external_hosts if "event.example" in h[1])
    pp, po = projected(ext)
    assert all("handle" not in r or r["handle"].startswith("https://www.linkedin.com/in/") for r in pp.rows)
    assert ext.contact_data >= 1 and "info@event.example" not in str(pp.rows) + str(po.rows)


def test_wordpress_plugin_cards():
    ext = run("wordpress_plugin.html")
    assert ext.person_layer == "cards"
    by = {p["name"]: p for p in ext.persons}
    assert by["Pat Smith"]["title"] == "VP Marketing" and by["Pat Smith"]["company"] == "Widget Works"
    assert by["Sam Lee"]["company"] == "Lee & Sons"


# --- malicious ---------------------------------------------------------------------------------
def test_injection_in_bio_is_dropped_by_projection():
    ext = run("bio_injection.html")
    pp, _ = projected(ext)
    rows = {r["name"]: r for r in pp.rows}
    assert "Eve Mallory" in rows and rows["Eve Mallory"]["_severity"] == "high" and "prompt_injection" in rows["Eve Mallory"]["_taint"]
    assert rows["Shelly Script"]["_severity"] == "medium" and "shell_fragment" in rows["Shelly Script"]["_taint"]
    assert rows["Honest Person"]["_severity"] is None and rows["Normal Two"]["_severity"] is None
    assert "eve@evil.example" not in str(pp.rows)


def test_five_thousand_cards_are_truncated_and_flagged():
    doc = parse_html((M / "five_thousand_cards.html").read_bytes())
    ext = extract(doc, "https://event.example/")
    assert len(ext.persons) <= 600 and ext.person_layer in (None, "cards")
    pp, _ = projected(ext)
    assert len(pp.rows) <= 300 and (any(f.code == "too_many_items" for f in pp.findings) or len(ext.persons) <= 300)


def test_script_heavy_page_yields_only_visible_people():
    ext = run("script_heavy.html")
    names = {p["name"] for p in ext.persons}
    assert names == {"Real Speaker", "Other Speaker", "Third Speaker"}
    assert "steal" not in str(ext.persons) and "alert" not in str(ext.persons)


def test_contacts_are_counted_not_stored():
    ext = run("contacts_on_cards.html")
    assert ext.contact_data >= 3
    pp, _ = projected(ext)
    blob = str(pp.rows)
    assert "@" not in blob and "555" not in blob and "Main Street" not in blob
    assert {r["name"] for r in pp.rows} == {"Anna Contact", "Ben Reach", "Cara Street"}


def test_homoglyph_and_hidden_unicode_names_are_flagged():
    ext = run("homoglyph_names.html")
    pp, _ = projected(ext)
    rows = {r["name"]: r for r in pp.rows}
    assert any("mixed_script" in r["_taint"] for r in rows.values())
    assert any("hidden_unicode" in r["_taint"] for r in rows.values())
    assert rows["Real Fine"]["_severity"] is None and rows["Also Real"]["_severity"] is None


def test_deep_nesting_is_limited_not_fatal():
    doc = parse_html((M / "deep_nesting.html").read_bytes())
    ext = extract(doc, "https://event.example/")
    assert "structure_limit" in ext.notes and ext.persons == []
