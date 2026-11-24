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


def copy_with(home, sheet, labels: dict[int, str], name: str = "rater.csv", edit=None, **extra) -> object:
    """A filled copy of the sheet; ``extra`` sets the same columns on every row, ``edit(row)`` changes rows one by one."""
    p = home / "feeds" / name
    p.write_text(sheet.read_text(encoding="utf-8-sig"), encoding="utf-8")
    fill(p, labels, edit=edit or ((lambda r: r.update(extra)) if extra else None))
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
    if os.name == "posix":  # Windows has no owner-only mode bits to read back
        for p in (md, js):
            assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["raters"][0]["kappa"] == pytest.approx(0.4) and len(data["rows"]) == N


# A synthetic provider-facts file: one model with a long-context rate, one without, one with no price.
FACTS = {"$schema": "provider-facts.v1", "generatedAt": "2026-01-02", "providers": {"acmeai": {
    "sources": {"pricing": {"url": "https://example.test/pricing", "fetched": "2026-01-02"}},
    "models": {"acmeai/model-a-1.0": {"apiId": "model-a-1-0", "price": {
                   "input": 2.0, "output": 10.0, "longContext": {"threshold": 2000, "input": 4.0, "output": 15.0}}},
               "acmeai/model-b": {"apiId": "model-b", "price": {"input": 1.0, "output": 5.0}},
               "acmeai/unpriced": {"apiId": "unpriced"}}}}}


@pytest.fixture
def facts(home):
    p = home / "facts.json"
    p.write_text(json.dumps(FACTS), encoding="utf-8")
    return p


def tokens(r):
    """Rows 1-49: 1000 in, 100 out; row 50: 3000 in (over the 2000 long-context threshold), 100 out. Latency 1010..1500."""
    n = int(r["row"])
    r.update(model="model-a-1-0", input_tokens=str(3000 if n == 50 else 1000), output_tokens="100", latency_ms=str(1000 + 10 * n))


def test_f1_tokens_and_list_price_cost_by_hand(home, sheet, facts):
    s = L.read_sheet(sheet)
    rater = E.sheet_rater(copy_with(home, sheet, RATER, edit=tokens), s, "second")
    rep = E.evaluate(s, [rater], reps=500, now=NOW, prices=E.load_prices(facts))
    r = rep["raters"][0]
    # tp 20, fp 10, fn 5: F1 = 2*20 / (2*20 + 10 + 5) = 8/11; its interval is the same seeded bootstrap as kappa's
    assert r["positive_f1"] == pytest.approx(8 / 11)
    truth = [TRUTH[i] for i in range(1, N + 1)]
    assert r["positive_f1_ci"] == S.bootstrap(truth, rater.labels, S.f1_stat("fit"), reps=500)
    assert r["usage"] == {"model": "model-a-1-0", "rows": 50, "input_tokens": 49 * 1000 + 3000, "output_tokens": 5000}
    assert r["median_latency_ms"] == (1250 + 1260) / 2
    # 49 rows at (1000*2 + 100*10)/1e6 = 0.003 each = 0.147; row 50 at the long-context rates (3000*4 + 100*15)/1e6 = 0.0135
    c = r["cost"]
    assert (c["basis"], c["model"], c["long_context_rows"]) == ("list price", "acmeai/model-a-1.0", 1)
    assert c["usd"] == pytest.approx(0.1605) and c["per_company"] == pytest.approx(0.1605 / 50)
    assert c["per_true_yes"] == pytest.approx(0.1605 / 20)
    assert rep["prices"]["generatedAt"] == "2026-01-02" and rep["prices"]["sources"] == ["https://example.test/pricing"]
    assert "not what was paid" in rep["prices"]["note"]
    md = E.render_markdown(rep)
    assert "| 0.40 (" in md and "| 0.73 (" in md  # kappa, then F1 beside it
    assert "| second | model-a-1-0 | 50/50 | 52,000 | 5,000 | 1.3 s | $0.1605 | $0.0032 | $0.0080 |" in md
    assert "generatedAt 2026-01-02" in md and "list-price equivalents" in md and "electricity isn't counted" in md


def test_local_raters_cost_nothing_and_missing_counts_say_so(home, sheet, facts):
    s = L.read_sheet(sheet)
    conn = connect(home)
    store(conn, s, {i: ("ok", "no_fit") for i in range(1, N + 1)})  # a local model that never says fit
    conn.execute("UPDATE qualifications SET input_tokens = 800, output_tokens = 60, latency_ms = 4000 + id")
    raters = [E.stored_rater(conn, s, "local-3b", "p0"),
              E.sheet_rater(copy_with(home, sheet, RATER, name="plain.csv"), s, "plain"),
              E.sheet_rater(copy_with(home, sheet, RATER, name="b.csv", model="model-b", input_tokens="10",
                                      output_tokens="1"), s, "b"),
              E.sheet_rater(copy_with(home, sheet, RATER, name="u.csv", model="unpriced", input_tokens="10"), s, "u")]
    rep = E.evaluate(s, raters, reps=200, now=NOW, prices=E.load_prices(facts))
    local, plain, b, u = rep["raters"]
    assert local["positive_f1"] is None
    assert local["usage"] == {"model": "local-3b", "rows": 50, "input_tokens": 40000, "output_tokens": 3000}
    assert local["median_latency_ms"] == 4025.5 and local["answered_at"] == [E._iso(NOW)] * 2
    assert local["cost"] == {"basis": "local", "usd": 0.0, "per_company": 0.0, "per_true_yes": None,
                             "note": "run on this machine: 0 at list price; electricity isn't counted"}
    assert plain["usage"] is None and plain["cost"]["usd"] is None and plain["cost"]["note"] == "no token counts recorded"
    assert b["cost"]["usd"] == pytest.approx(50 * (10 * 1.0 + 1 * 5.0) / 1e6) and b["cost"]["long_context_rows"] == 0
    assert u["cost"]["usd"] is None and "has no price in facts.json" in u["cost"]["note"]
    md = E.render_markdown(rep)
    assert "| local-3b | p0 | 50/50 | 25 (" in md and "| – | – / 0% |" in md  # never said fit: no F1, no precision
    assert "| plain | – | not recorded | – | – | – | – | – | – |" in md
    assert "No cost for plain: no token counts recorded; u: model 'unpriced' has no price in facts.json." in md


def test_token_and_price_refusals(home, sheet, facts):
    s = L.read_sheet(sheet)
    bad = copy_with(home, sheet, RATER, name="bad.csv", input_tokens="12k")
    with pytest.raises(L.LabelSheetError, match=r"bad.csv row 1: input_tokens is '12k', not a whole number"):
        E.sheet_rater(bad, s)
    mixed = copy_with(home, sheet, RATER, name="mixed.csv", edit=lambda r: r.update(model="m1" if int(r["row"]) % 2 else "m2"))
    with pytest.raises(L.LabelSheetError, match=r"mixes answers from 2 models \(m1, m2\)"):
        E.sheet_rater(mixed, s)
    with pytest.raises(ValueError, match="cannot read the prices file"):
        E.load_prices(home / "absent.json")
    (home / "notfacts.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match='no "providers" object'):
        E.load_prices(home / "notfacts.json")
    broken = json.loads(json.dumps(FACTS))
    broken["providers"]["acmeai"]["models"]["acmeai/model-b"]["price"]["output"] = "5"
    (home / "broken.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match=r"models\['acmeai/model-b'\]\.price\.output is '5', not a number"):
        E.load_prices(home / "broken.json")
    idx = E.load_prices(facts)["models"]
    assert idx["model-a-1-0"] is idx["model-a-1.0"] is idx["acmeai/model-a-1.0"] and "unpriced" not in idx


def test_cli_prices_flag(cli, home, sheet, facts):  # noqa: F811
    rater = copy_with(home, sheet, RATER, edit=tokens)
    code, out = cli("evaluate", "--sheet", str(sheet), "--rater", f"{rater}=second", "--reps", "200", "--no-save")
    assert code == 0 and "| second | model-a-1-0 | 50/50 | 52,000 | 5,000 | 1.3 s | – | – | – |" in out
    assert "Cost: not computed; pass `--prices`" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--rater", f"{rater}=second", "--reps", "200", "--no-save",
                    "--prices", str(facts))
    assert code == 0 and "| $0.1605 | $0.0032 | $0.0080 |" in out and "generatedAt 2026-01-02" in out
    code, out = cli("evaluate", "--sheet", str(sheet), "--rater", f"{rater}=second", "--no-save",
                    "--prices", str(home / "absent.json"))
    assert code == 2 and "cannot evaluate: cannot read the prices file" in out
