"""Evaluation: a rater is scored only on the evidence the person labelled, missing answers count against it, and the
numbers match ones worked out by hand (the textbook kappa table from test_stats, laid out as a 50-row sheet)."""
from __future__ import annotations

import json
import os

import pytest

from ninshubur import evaluate as E
from ninshubur import labelsheet as L
from ninshubur import stats as S
from ninshubur.db import connect
from tests.conftest import NOW
from tests.test_label_sheet import PROFILE, fill, subj
from tests.test_qualify import cli  # noqa: F401  (a fixture)

N = 50
# Person: fit on rows 1-25, no_fit on 26-50. Rater: fit/fit 20, fit/no_fit 5, no_fit/fit 10, no_fit/no_fit 15.
TRUTH = {i: ("fit" if i <= 25 else "no_fit") for i in range(1, N + 1)}
RATER = {i: ("fit" if i <= 20 or 26 <= i <= 35 else "no_fit") for i in range(1, N + 1)}


@pytest.fixture
def sheet(home):
    path, _ = L.write([subj(i, total=2) for i in range(1, N + 1)], PROFILE, home, sample=N, seed=7, now=NOW)
    fill(path, TRUTH, libreoffice=True)
    return path


def copy_with(home, sheet, labels: dict[int, str], name: str = "rater.csv", **extra) -> object:
    p = home / "feeds" / name
    p.write_text(sheet.read_text(encoding="utf-8-sig"), encoding="utf-8")
    fill(p, labels, edit=(lambda r: r.update(extra)) if extra else None)
    return p


def store(conn, sheet_obj, answers: dict[int, tuple[str, str | None]], *, model="local-3b", slot="local", phash="p0",
          wrong_evidence: set[int] = frozenset()) -> None:
    cur = conn.execute("INSERT INTO llm_runs(task, profile, slot, model, prompt_hash, started_at) VALUES ('qualify','acme',?,?,?,?)",
                       (slot, model, phash, NOW))
    for r in sheet_obj.rows:
        if r.row not in answers:
            continue
        status, label = answers[r.row]
        conn.execute("""INSERT INTO qualifications(run_id, profile, subject_key, subject_name, model, prompt_hash, evidence_hash,
                        status, label, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (cur.lastrowid, "acme", r.subject.key, r.subject.name, model, phash,
                      "0" * 16 if r.row in wrong_evidence else r.subject.evidence_hash, status, label, NOW))


def test_a_rater_copy_scores_the_hand_worked_numbers(home, sheet):
    s = L.read_sheet(sheet)
    rep = E.evaluate(s, [E.sheet_rater(copy_with(home, sheet, RATER), s, "second")], reps=500, now=NOW)
    r = rep["raters"][0]
    assert (r["n"], r["agree"], r["answered"], r["failed"], r["missing"]) == (N, 35, N, 0, 0)
    assert r["kappa"] == pytest.approx(0.4) and r["agreement_ci"] == pytest.approx(S.wilson(35, 50))
    assert r["positive_precision"] == pytest.approx(20 / 30) and r["positive_recall"] == pytest.approx(20 / 25)
    # discordant on "fit": 10 said fit where the person did not, 5 missed: 2 * (C(15,0)+...+C(15,5)) / 2^15
    assert r["mcnemar_p"] == pytest.approx(2 * 4944 / 32768)
    assert r["confusion"] == {"fit": {"fit": 20, "no_fit": 5}, "no_fit": {"fit": 10, "no_fit": 15}}
    assert rep["baseline"]["agree"] == 25 and rep["sheet"]["totals_recorded"] is True
    assert "verdict" not in r  # a sheet copy is not a local model


def test_stored_answers_count_only_on_the_labelled_evidence(home, sheet):
    s = L.read_sheet(sheet)
    conn = connect(home)
    answers = {i: ("ok", TRUTH[i]) for i in range(1, N + 1)}
    answers[2] = ("failed", None)
    answers[3] = ("ok", "fit")  # stored twice below: the newest answer counts
    del answers[4]
    store(conn, s, answers, wrong_evidence={5})
    store(conn, s, {3: ("ok", "no_fit")})
    assert E.stored_pairs(conn, s) == [("local-3b", "p0")]
    r = E.stored_rater(conn, s, "local-3b", "p0")
    assert (r.failed, r.missing, r.local) == (1, 2, True)  # row 2 failed; rows 4 (never asked) and 5 (other evidence) missing
    assert r.labels[1] == r.labels[3] == r.labels[4] == E.FAILED and r.labels[2] == "no_fit"
    assert r.source == "run 1, 2"


def test_local_verdict_against_the_best_remote_rater(home, sheet):
    s = L.read_sheet(sheet)
    conn = connect(home)
    store(conn, s, {i: ("ok", TRUTH[i]) for i in range(1, N + 1)}, model="perfect")
    store(conn, s, {i: ("ok", TRUTH[i]) for i in range(1, N - 6)}, model="gappy")  # 7 rows unanswered
    raters = [E.stored_rater(conn, s, "perfect", "p0"), E.stored_rater(conn, s, "gappy", "p0"),
              E.sheet_rater(copy_with(home, sheet, RATER), s, "remote")]
    rep = E.evaluate(s, raters, reps=300, now=NOW)
    good, gappy, remote = rep["raters"]
    assert good["verdict"]["result"] == "non-inferior" and good["verdict"]["against"] == "remote"
    assert good["verdict"]["difference"] == pytest.approx(1 - 0.7)
    assert gappy["missing"] == 7 and gappy["verdict"]["result"] == "not shown non-inferior"
    md = E.render_markdown(rep)
    assert "| perfect | p0 |" in md and "non-inferior vs remote" in md and "(7 without a usable answer)" in md


def test_refusals(home, sheet):
    s = L.read_sheet(sheet)
    partial = copy_with(home, sheet, {1: "fit"}, name="partial.csv")
    (home / "feeds" / "partial.csv.meta.json").write_text(L.meta_path(sheet).read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(L.LabelSheetError, match="49 row"):
        E.evaluate(L.read_sheet(partial), [E.sheet_rater(sheet, s)], reps=10)
    mixed = home / "feeds" / "mixed.csv"
    mixed.write_text(sheet.read_text(encoding="utf-8-sig"), encoding="utf-8")
    fill(mixed, RATER, edit=lambda r: r.update(prompt_hash="aaa" if int(r["row"]) % 2 else "bbb"))
    with pytest.raises(L.LabelSheetError, match="mixes answers from 2 prompts"):
        E.sheet_rater(mixed, s)


def test_cli_scores_and_writes_a_private_report(cli, home, sheet):  # noqa: F811
    rater = copy_with(home, sheet, RATER)
    code, out = cli("evaluate", "--sheet", str(sheet), "--no-save")
    assert code == 2 and "nothing to score" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--model", "ghost")
    assert code == 2 and "no stored answers on this sheet's evidence for ghost" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--rater", f"{rater}=second", "--reps", "200", "--no-save")
    assert code == 0 and "| second | own | 50/50 | 35 (" in out and "| 0.40 (" in out
    assert not list((home / "feeds").glob("eval-*"))
    code, out = cli("evaluate", "--sheet", str(sheet), "--rater", f"{rater}=second", "--reps", "200", "--json")
    assert code == 0 and "report written" in out
    js, md = sorted((home / "feeds").glob("eval-acme-seed7-*"))
    assert (js.suffix, md.suffix) == (".json", ".md") and md.read_text(encoding="utf-8").startswith("# Evaluation: profile acme")
    for p in (md, js):
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["raters"][0]["kappa"] == pytest.approx(0.4) and len(data["rows"]) == N
