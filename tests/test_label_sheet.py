"""Label sheet: blind (no model output), stratified in proportion, the same for the same seed, never overwritten by accident."""
from __future__ import annotations

import csv
import io
import json

import pytest

from ninshubur import labelsheet as L
from ninshubur.analyze import qualify as Q
from ninshubur.profiles import Profile, Qualify
from tests.conftest import NOW, SAMPLES
from tests.test_llm import Server, write_cfg
from tests.test_qualify import cli, reply, run_once  # noqa: F401  (cli is a fixture)

PROFILE = Profile(name="acme", qualify=Qualify(question="Would this organisation take on the offered work?", labels=("fit", "no_fit")))


def subj(i: int, total: int = 1, sources: tuple[str, ...] = ("board",), name: str | None = None) -> Q.Subject:
    return Q.Subject(key=f"company:c{i:03d}", name=name or f"Company {i}", lines=tuple(f"Line {n} of company {i}" for n in range(1, min(total, 3) + 1)),
                     item_ids=tuple(range(i * 10, i * 10 + min(total, 3))), total=total, sources=sources)


def population() -> list[Q.Subject]:
    # 60 from a board (sizes 1..60), 30 from a forum (size 1), 10 seen on both (size 40)
    return ([subj(i, total=i) for i in range(1, 61)] + [subj(100 + i, total=1, sources=("forum",)) for i in range(30)]
            + [subj(200 + i, total=40, sources=("board", "forum")) for i in range(10)])


def parse(text: str) -> list[dict]:
    assert text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(text[1:])))


@pytest.mark.parametrize("sizes, n, want", [
    ({"a": 60, "b": 30, "c": 10}, 10, {"a": 6, "b": 3, "c": 1}),
    ({"a": 97, "b": 2, "c": 1}, 10, {"a": 8, "b": 1, "c": 1}),      # small strata are still seen
    ({"a": 50, "b": 50}, 3, {"a": 2, "b": 1}),                      # a tie goes to the first name
    ({"a": 5, "b": 1}, 100, {"a": 5, "b": 1}),                      # more asked than there is: everything
    ({"a": 5, "b": 5, "c": 5}, 2, {"a": 1, "b": 1, "c": 0}),        # fewer rows than strata
    ({"a": 2, "b": 98}, 50, {"a": 1, "b": 49}),
])
def test_allocation_is_proportional_and_bounded(sizes: dict, n: int, want: dict):
    got = L.allocate(sizes, n)
    assert got == want and sum(got.values()) == min(n, sum(sizes.values())) and all(got[k] <= sizes[k] for k in sizes)


def test_strata_come_from_sources_and_evidence_size_only():
    strata = L.stratify(population())
    assert set(strata.values()) == {"board/few", "board/some", "board/many", "forum/few", "board+forum/many"}
    assert strata["company:c001"] == "board/few" and strata["company:c060"] == "board/many" and strata["company:c100"] == "forum/few"
    assert L.stratify([]) == {}


def test_the_draw_is_the_same_for_a_seed_and_differs_between_seeds():
    a, b, c = L.draw(population(), 30, 7), L.draw(list(reversed(population())), 30, 7), L.draw(population(), 30, 8)
    keys = [s.key for s, _ in a.rows]
    assert keys == [s.key for s, _ in b.rows] and keys != [s.key for s, _ in c.rows]
    assert len(set(keys)) == 30 and a.population == 100
    assert {k: got for k, (_, got) in a.strata.items()} == L.allocate({k: pop for k, (pop, _) in a.strata.items()}, 30)
    assert all(got >= 1 for _, got in a.strata.values())
    by_stratum = [stratum for _, stratum in a.rows]
    assert by_stratum != sorted(by_stratum)  # the row order does not follow the strata


def test_the_sheet_is_blind_even_after_a_model_has_answered(home):
    write_cfg(home)
    subs = population()[:6]
    run_once(home, Server(reply("yes", (1,), "MODEL-REASON-TEXT")), subs, profile=Profile(name="acme", qualify=Q.Qualify(
        question="Would this organisation take on the offered work?", labels=("yes", "no"))))
    path, sheet = L.write(subs, PROFILE, home, sample=6, seed=7, now=NOW)
    text = path.read_text(encoding="utf-8")
    rows_ = parse(text)
    assert list(rows_[0]) == L.COLUMNS and len(rows_) == 6
    assert all(r["label"] == "" and r["note"] == "" for r in rows_)
    assert "MODEL-REASON-TEXT" not in text and "haiku" not in text
    assert [r["row"] for r in rows_] == [str(i) for i in range(1, 7)]
    by_key = {s.key: s for s in subs}
    for r in rows_:
        s = by_key[r["subject_key"]]
        assert r["name"] == s.name and r["lines"] == str(len(s.lines))
        assert r["evidence"] == "\n".join(f"[{n}] {line}" for n, line in enumerate(s.lines, 1))


def test_the_meta_file_says_what_was_drawn_and_from_what(home):
    path, sheet = L.write(population(), PROFILE, home, sample=20, seed=7, now=NOW)
    meta = json.loads(L.meta_path(path).read_text(encoding="utf-8"))
    assert path.name == "label-sheet-acme-seed7.csv" and L.meta_path(path).name == "label-sheet-acme-seed7.csv.meta.json"
    assert (meta["profile"], meta["seed"], meta["sample_asked"], meta["sampled"], meta["population"]) == ("acme", 7, 20, 20, 100)
    assert meta["question"] == PROFILE.qualify.question and meta["labels"] == ["fit", "no_fit"] and meta["positive"] == "fit"
    assert meta["prompt_hash"] == Q.prompt_hash(PROFILE.qualify) and meta["created_at"] == "2026-10-12T12:00:00Z"
    assert sum(s["population"] for s in meta["strata"]) == 100 and sum(s["sampled"] for s in meta["strata"]) == 20
    by_key = {s.key: s for s in population()}
    assert [r["row"] for r in meta["rows"]] == list(range(1, 21))
    for r, (s, stratum) in zip(meta["rows"], sheet.rows, strict=True):
        assert r["subject_key"] == s.key and r["stratum"] == stratum
        assert r["evidence_hash"] == by_key[s.key].evidence_hash and r["item_ids"] == list(s.item_ids)


def test_an_existing_sheet_is_not_replaced_without_force(home):
    path, _ = L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW)
    path.write_text("my labels", encoding="utf-8")
    with pytest.raises(L.LabelSheetError, match="exists and may hold labels"):
        L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW)
    assert path.read_text(encoding="utf-8") == "my labels"
    L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW, force=True)
    assert len(parse(path.read_text(encoding="utf-8"))) == 5
    other, _ = L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW, out=home / "feeds" / "second.csv")
    assert other.name == "second.csv" and L.meta_path(other).exists()


def test_refusals_name_the_fix(home):
    with pytest.raises(L.LabelSheetError, match="no qualify: block"):
        L.write(population(), Profile(name="bare"), home, sample=5, seed=7, now=NOW)
    with pytest.raises(L.LabelSheetError, match="no subjects with evidence"):
        L.write([], PROFILE, home, sample=5, seed=7, now=NOW)
    with pytest.raises(L.LabelSheetError, match="at least 1"):
        L.write(population(), PROFILE, home, sample=0, seed=7, now=NOW)


def test_cells_that_look_like_formulas_are_guarded(home):
    s = Q.Subject(key="company:x", name="=HYPERLINK(1)", lines=("=cmd|' /C calc'!A0",), item_ids=(1,), total=1, sources=("board",))
    path, _ = L.write([s], PROFILE, home, sample=1, seed=7, now=NOW)
    row = parse(path.read_text(encoding="utf-8"))[0]
    assert row["name"] == "'=HYPERLINK(1)" and row["evidence"].startswith("[1] =cmd")  # the number comes first, so no formula


def test_cli_writes_the_sheet_and_refuses_what_it_cannot_draw(cli, home):  # noqa: F811
    assert cli("run", "--replay", str(SAMPLES))[0] == 0
    code, out = cli("export", "label-sheet")
    assert code == 2 and "needs --profile" in out
    code, out = cli("export", "label-sheet", "--profile", "bare")
    assert code == 2 and "no qualify: block" in out
    code, out = cli("export", "label-sheet", "--profile", "agency-eu", "--sample", "100", "--seed", "7")
    assert code == 0 and "no model output is in the sheet" in out and "fill the label column with one of: fit, no_fit, unsure" in out
    assert "were asked for, but only" in out and cli.server.requests == []
    path = home / "feeds" / "label-sheet-agency-eu-seed7.csv"
    rows_ = parse(path.read_text(encoding="utf-8"))
    assert rows_ and all(r["label"] == "" for r in rows_) and all(r["evidence"].startswith("[1] ") for r in rows_)
    code, out = cli("export", "label-sheet", "--profile", "agency-eu")
    assert code == 2 and "exists and may hold labels" in out
    code, out = cli("export", "label-sheet", "--profile", "agency-eu", "--force", "--sample", "1")
    assert code == 0 and len(parse(path.read_text(encoding="utf-8"))) == 1
    code, out = cli("export", "label-sheet", "--profile", "agency-eu", "--seed", "8", "--exclude", str(path))
    assert code == 2 and "nothing is left to draw" in out  # the replay has one company, and it is on the seed-7 sheet
    other = home / "other.csv"
    other.write_text("row,subject_key\n1,company:not-in-the-replay\n", encoding="utf-8")
    code, out = cli("export", "label-sheet", "--profile", "agency-eu", "--seed", "8", "--exclude", str(other))
    assert code == 0 and "left out 0 subject(s) already on other.csv" in out, out
    code, out = cli("export", "label-sheet", "--profile", "agency-eu", "--seed", "9", "--exclude", str(home / "nope.csv"))
    assert code == 2 and "cannot read it" in out



def test_a_second_sheet_leaves_out_every_subject_on_the_first(home):
    prof = PROFILE
    subjects = population()
    first, _ = L.write(subjects, prof, home, sample=10, seed=7, now=NOW)
    seen = {r["subject_key"] for r in parse(first.read_text(encoding="utf-8"))}
    second, sheet = L.write(subjects, prof, home, sample=10, seed=8, now=NOW, exclude=[first])
    keys = {r["subject_key"] for r in parse(second.read_text(encoding="utf-8"))}
    assert keys and not keys & seen and len(keys) == 10
    meta = json.loads(L.meta_path(second).read_text(encoding="utf-8"))
    assert meta["population"] == len(subjects) - len(seen) == sheet.population
    assert meta["excluded"] == [{"sheet": first.name, "sha256": meta["excluded"][0]["sha256"], "subjects": 10, "in_population": 10}]
    assert json.loads(L.meta_path(first).read_text(encoding="utf-8"))["excluded"] == []
    with pytest.raises(L.LabelSheetError, match="--exclude .*cannot read it"):
        L.write(subjects, prof, home, sample=5, seed=9, now=NOW, exclude=[home / "missing.csv"])
    (home / "notes.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(L.LabelSheetError, match="no subject_key column"):
        L.write(subjects, prof, home, sample=5, seed=9, now=NOW, exclude=[home / "notes.csv"])
    everyone, _ = L.write(subjects, prof, home, sample=len(subjects), seed=10, now=NOW)
    with pytest.raises(L.LabelSheetError, match="nothing is left to draw"):
        L.write(subjects, prof, home, sample=5, seed=11, now=NOW, exclude=[everyone])

# --- reading a filled sheet ----------------------------------------------------------------------
def fill(path, labels: dict[int, str], *, libreoffice: bool = False, edit=None) -> None:
    """Fill the label column the way a person would; ``libreoffice`` re-saves it with a BOM, CRLF and an empty column."""
    text = path.read_text(encoding="utf-8-sig")
    recs = list(csv.DictReader(io.StringIO(text)))
    for r in recs:
        r["label"] = labels.get(int(r["row"]), "")
        if edit:
            edit(r)
    buf = io.StringIO()
    cols = list(recs[0].keys()) + ([""] if libreoffice else [])
    w = csv.DictWriter(buf, cols, lineterminator="\r\n" if libreoffice else "\n")
    w.writeheader()
    for r in recs:
        w.writerow({**r, **({"": ""} if libreoffice else {})})
    path.write_text(("﻿" if libreoffice else "") + buf.getvalue(), encoding="utf-8")


def test_a_filled_sheet_reads_back_with_its_subjects(home):
    path, sheet = L.write(population(), PROFILE, home, sample=6, seed=7, now=NOW)
    fill(path, {1: "fit", 2: "NO_FIT ", 3: "fit"}, libreoffice=True)
    got = L.read_sheet(path)
    assert [r.row for r in got.rows] == list(range(1, 7))
    assert [r.label for r in got.rows[:4]] == ["fit", "no_fit", "fit", None] and got.unlabelled() == [4, 5, 6]
    for r, (s, stratum) in zip(got.rows, sheet.rows, strict=True):
        assert r.subject.key == s.key and r.subject.lines == s.lines and r.subject.evidence_hash == s.evidence_hash
        assert r.subject.total == s.total and r.stratum == stratum and r.subject.item_ids == s.item_ids
    assert got.labels == ("fit", "no_fit") and got.positive == "fit" and len(got.sha256) == 64


def test_a_guarded_name_reads_back_as_drawn(home):
    s = Q.Subject(key="company:x", name="=HYPERLINK(1)", lines=("@risk line",), item_ids=(1,), total=1, sources=("board",))
    path, _ = L.write([s], PROFILE, home, sample=1, seed=7, now=NOW)
    fill(path, {1: "fit"})
    assert L.read_sheet(path).rows[0].subject.name == "=HYPERLINK(1)"


@pytest.mark.parametrize("edit, match", [
    (lambda r: r.update(evidence=r["evidence"] + " (checked)") if r["row"] == "2" else None, r"row 2 .*differs from what was drawn"),
    (lambda r: r.update(name="Someone else") if r["row"] == "3" else None, r"row 3 .*differs from what was drawn"),
    (lambda r: r.update(label="maybe") if r["row"] == "1" else None, r"row 1 .*'maybe' is not one of fit, no_fit"),
    (lambda r: r.update(evidence="1. renumbered") if r["row"] == "1" else None, r"row 1: evidence line 1 does not start with '\[1\]'"),
    (lambda r: r.update(subject_key="company:zzz") if r["row"] == "4" else None, r"row 4: subject 'company:zzz'"),
])
def test_an_edited_sheet_is_refused_with_the_row(home, edit, match):
    path, _ = L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW)
    fill(path, {1: "fit"}, edit=edit)
    with pytest.raises(L.LabelSheetError, match=match):
        L.read_sheet(path)


def test_deleted_or_missing_pieces_are_refused(home):
    path, _ = L.write(population(), PROFILE, home, sample=5, seed=7, now=NOW)
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    path.write_text("".join(lines[:1] + lines[2:]), encoding="utf-8")
    with pytest.raises(L.LabelSheetError, match=r"row\(s\) 1 are missing"):
        L.read_sheet(path)
    L.meta_path(path).unlink()
    with pytest.raises(L.LabelSheetError, match="meta.json is missing"):
        L.read_sheet(path)


def test_a_second_raters_copy_is_read_against_the_first_meta(home):
    path, _ = L.write(population(), PROFILE, home, sample=4, seed=7, now=NOW)
    copy = home / "feeds" / "rater.csv"
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    fill(copy, {1: "fit", 2: "fit", 3: "no_fit", 4: "fit"}, edit=lambda r: r.update(reason="because"))
    meta = json.loads(L.meta_path(path).read_text(encoding="utf-8"))
    got = L.read_sheet(copy, meta=meta)
    assert [r.label for r in got.rows] == ["fit", "fit", "no_fit", "fit"] and got.rows[0].extra == {"reason": "because"}


def test_cli_asks_on_the_sheet_as_drawn_and_on_a_second_prompt(cli, home):  # noqa: F811
    from ninshubur.db import connect, rows
    assert cli("run", "--replay", str(SAMPLES))[0] == 0
    assert cli("export", "label-sheet", "--profile", "agency-eu", "--sample", "3", "--seed", "7")[0] == 0
    path = home / "feeds" / "label-sheet-agency-eu-seed7.csv"
    fill(path, {1: "fit", 2: "no_fit", 3: "unsure"}, libreoffice=True)
    meta = json.loads(L.meta_path(path).read_text(encoding="utf-8"))
    n = len(meta["rows"])
    assert n >= 1
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(path))
    assert code == 0 and "evidence exactly as drawn" in out and "is the one the sheet was drawn with" in out
    assert len(cli.server.requests) == n
    sent = json.loads(cli.server.requests[0].content)["messages"][1]["content"]
    first = L.read_sheet(path).rows[0].subject
    assert all(line in sent or any(line in json.loads(r.content)["messages"][1]["content"] for r in cli.server.requests)
               for line in first.lines)
    conn = connect(home)
    got = {(r["subject_key"], r["evidence_hash"]) for r in rows(conn, "SELECT subject_key, evidence_hash FROM qualifications")}
    assert got == {(r["subject_key"], r["evidence_hash"]) for r in meta["rows"]}

    second = home / "p1.yaml"
    second.write_text("qualify:\n  question: Would this company buy the service within a month?\n  labels: [fit, no_fit, unsure]\n",
                      encoding="utf-8")
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(path), "--qualify-file", str(second), "--dry-run")
    assert code == 0 and "differs from the sheet's" in out and f"to ask: {n}" in out and len(cli.server.requests) == n

    second.write_text("question: Would this company buy?\nlabels: [yes, no]\n", encoding="utf-8")
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(path), "--qualify-file", str(second))
    assert code == 2 and "must keep the same answers" in out
    second.write_text("question: Would this company buy?\nlabel: [fit]\n", encoding="utf-8")
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(path), "--qualify-file", str(second))
    assert code == 2 and "unknown key(s) label" in out
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(home / "nope.csv"))
    assert code == 2 and "cannot read" in out
    meta["profile"] = "other"
    other = home / "other-meta.json"
    other.write_text(json.dumps(meta), encoding="utf-8")
    code, out = cli("qualify", "--profile", "agency-eu", "--from-sheet", str(path), "--meta", str(other))
    assert code == 2 and "drawn for profile 'other'" in out
    assert len(cli.server.requests) == n


def test_the_sheet_links_each_line_to_its_page_and_only_to_web_pages(home):
    groups = {"company:x": [
        {"id": 1, "kind": "hiring", "source": "board", "title": "Python engineer", "text": "", "attrs": "{}", "published_at": NOW,
         "first_seen_at": NOW, "url": "https://jobs.example/1", "tainted": 0, "held": 0, "person_data": 0},
        {"id": 2, "kind": "hiring", "source": "board", "title": "Data engineer", "text": "", "attrs": "{}", "published_at": NOW - 1,
         "first_seen_at": NOW - 1, "url": "javascript:alert(1)", "tainted": 0, "held": 0, "person_data": 0},
        {"id": 3, "kind": "hiring", "source": "board", "title": "ML engineer", "text": "", "attrs": "{}", "published_at": NOW - 2,
         "first_seen_at": NOW - 2, "url": "=HYPERLINK(\"https://x\")", "tainted": 0, "held": 0, "person_data": 0}]}
    (s,) = Q.build_subjects(groups, {"company:x": "X"}, PROFILE)
    assert s.urls == ("https://jobs.example/1", "", "")
    before = Q.Subject(key=s.key, name=s.name, lines=s.lines, item_ids=s.item_ids, total=s.total)
    assert s.evidence_hash == before.evidence_hash  # links are not evidence
    path, _ = L.write([s], PROFILE, home, sample=1, seed=7, now=NOW)
    row = parse(path.read_text(encoding="utf-8"))[0]
    assert row["links"] == "[1] https://jobs.example/1"
    fill(path, {1: "fit"})
    assert L.read_sheet(path).rows[0].label == "fit"
