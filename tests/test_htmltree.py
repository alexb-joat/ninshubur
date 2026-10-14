from __future__ import annotations

from pathlib import Path

from ninshubur.parse.htmltree import card_groups, json_ld, microdata, parse_html

S = Path(__file__).parent / "samples" / "event_sites"
M = Path(__file__).parent / "malicious" / "event_sites"


def test_active_content_never_enters_the_tree():
    doc = parse_html((M / "script_heavy.html").read_bytes())
    tags = {n.tag for n in doc.find_all()}
    assert not tags & {"script", "style", "iframe", "object"}
    assert doc.scripts == 2 and doc.dropped >= 3
    text = doc.text()
    assert "steal()" not in text and "document.cookie" not in text and "display:none" not in text
    assert "onload" not in str([n.attrs for n in doc.find_all()])  # attribute whitelist
    assert "onerror" not in str([n.attrs for n in doc.find_all("img")])
    assert len(doc.ld_blocks) == 1 and json_ld(doc)[0]["name"] == "Only One"
    hrefs = [h for h, _, _ in doc.links()]
    assert "javascript:alert(3)" in hrefs  # kept as text: policy refuses it later; never executed here


def test_caps_make_hostile_pages_bounded():
    deep = parse_html((M / "deep_nesting.html").read_bytes(), max_depth=200)
    assert deep.limited and deep.count <= 202
    many = parse_html((M / "five_thousand_cards.html").read_bytes(), max_nodes=10_000)
    assert many.limited and many.count <= 10_000
    big_attr = parse_html(b'<a href="' + b"x" * 10_000 + b'">y</a>')
    assert len(big_attr.links()[0][0]) == 2048
    big_text = parse_html(b"<p>" + b"z" * 100_000 + b"</p>")
    assert len(big_text.find_all("p")[0].own_text()) <= 4096
    ld = parse_html(b'<script type="application/ld+json">' + b"[" * 100 + b"]" * 100 + b"</script>")
    assert json_ld(ld, max_depth=32) == []  # too deep: skipped, not raised
    bad = parse_html(b'<script type="application/ld+json">{not json</script>')
    assert json_ld(bad) == []


def test_tree_shape_and_helpers():
    doc = parse_html((S / "cards_linkedin.html").read_bytes())
    groups = card_groups(doc)
    assert any(len(g) == 5 and g[0].key == "div.speaker-card" for g in groups)
    card = next(g for g in groups if g[0].key == "div.speaker-card")[0]
    assert card.lines()[:2] == ["Jane Doe", "Head of Growth at Rocket Labs"]
    assert card.find_all("img") and card.find_all("a")[0].attrs["href"].startswith("https://www.linkedin.com/in/janedoe")
    assert "&" not in doc.find_all("h2")[1].text() or "Partners & Exhibitors" == doc.find_all("h2")[1].text()


def test_microdata():
    doc = parse_html((S / "microdata.html").read_bytes())
    md = [m for m in microdata(doc) if m["type"] == "Person"]
    assert [m["props"]["name"] for m in md] == ["Marie Curie", "Alan Turing", "Katherine Johnson"]
    assert md[0]["props"]["worksFor"] == "Radium Labs" and md[0]["props"]["sameAs"].startswith("https://www.linkedin.com/in/mcurie")


def test_garbage_input_does_not_raise():
    for raw in (b"", b"\xff\xfe\x00garbage<<<>>>", b"<" * 50_000, b"<a <b <c", "<p>ok</p>".encode("utf-16")):
        parse_html(raw)
