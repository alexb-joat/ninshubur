"""scripts/case_study.py: every number in the case study comes from the reports, `--check` catches a changed or
hand-typed number, and the paired verdicts follow the stated rule. Synthetic sheets and raters only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from ninshubur import evaluate as E
from ninshubur import labelsheet as L
from ninshubur import stats as S
from ninshubur.profiles import Profile, Qualify
from tests.conftest import NOW
from tests.test_label_sheet import fill, subj

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("case_study", ROOT / "scripts" / "case_study.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

PROFILE = Profile(name="acme", qualify=Qualify(question="Would this organisation take on the offered work?",
                                               labels=("yes", "no", "unsure")))
FACTS = {"$schema": "provider-facts.v1", "generatedAt": "2026-01-02", "providers": {"anthropic": {
    "sources": {"pricing": {"url": "https://example.test/pricing", "fetched": "2026-01-02"}},
    "models": {f"anthropic/claude-{m}-5.5": {"apiId": f"claude-{m}-5-5", "price": {"input": p, "output": 5 * p}}
               for m, p in (("haiku", 1.0), ("sonnet", 3.0), ("opus", 5.0))}}}}
P4 = "44444444aaaaaaaa"


def truth(n: int) -> dict[int, str]:
    """Rows 1-4 yes, 5-7 unsure, the rest no."""
    return {i: "yes" if i <= 4 else "unsure" if i <= 7 else "no" for i in range(1, n + 1)}


def rater(name: str, model: str, phash: str, labels: list[str]) -> E.Rater:
    return E.Rater(name=name, kind="sheet", local=False, prompt_hash=phash, source=f"{name}.csv", labels=labels,
                   model=f"claude-{model}-5-5", usage=[(1000, 100)] * len(labels), latencies=[2000] * len(labels),
                   written_at=NOW + 3600)


@pytest.fixture
def reports(home, monkeypatch):
    facts = home / "facts.json"
    facts.write_text(json.dumps(FACTS), encoding="utf-8")
    prices = E.load_prices(facts)
    out = []
    for seed, n in ((7, 20), (8, 10)):
        path, _ = L.write([subj(i, name=f"Secretco {i}") for i in range(1, n + 1)], PROFILE, home, sample=n, seed=seed,
                          now=NOW)
        t = truth(n)
        fill(path, t)
        sheet = L.read_sheet(path)
        p0 = sheet.meta["prompt_hash"]
        rows = [t[r.row] for r in sheet.rows]
        perfect, never = list(rows), ["no"] * n
        if seed == 7:
            raters = [rater("haiku-P0", "haiku", p0, never), rater("sonnet-P0", "sonnet", p0, ["yes"] * n),
                      rater("opus-P0", "opus", p0, perfect)]
        else:
            raters = [rater("opus-P0", "opus", p0, perfect), rater("opus-P4", "opus", P4, never)]
        rep = json.loads(E.to_json(E.evaluate(sheet, raters, reps=300, seed=7, now=NOW, prices=prices)))
        p = home / f"report-seed{seed}.json"
        p.write_text(json.dumps(rep), encoding="utf-8")
        out.append(p)
    monkeypatch.setattr(cs, "PROMPTS", [("P0", p0[:8], "the question as drawn"), ("P4", P4[:8], "a later prompt")])
    monkeypatch.setitem(cs.FRAGMENTS, "attacks", lambda _r: "**2 passed**")  # the real one runs pytest
    return [str(p) for p in out]


def skeleton(tmp: Path, extra: str = "") -> Path:
    doc = tmp / "case-study.md"
    doc.write_text("# Case study\n\nP0 against always no, F1 and 95% intervals, Haiku 5.5, seed 7 and Seed 8.\n"
                   + extra + "".join(f"\n<!-- gen:{n} -->\n<!-- /gen -->\n" for n in cs.FRAGMENTS if n not in cs.INLINE)
                   + "\nPrices dated <!-- gen:prices-date --><!-- /gen -->.\n", encoding="utf-8")
    return doc


def test_update_then_check_passes_and_prints_no_company_name(tmp_path, reports, capsys):
    doc = skeleton(tmp_path)
    assert cs.main(["--update", str(doc), *reports]) == 0
    assert cs.main(["--check", str(doc), *reports]) == 0
    out = capsys.readouterr().out
    assert "every number in case-study.md matches the regenerated reports" in out
    assert "0 numbers in prose outside fragments" in out
    assert cs.main(reports) == 0
    printed = capsys.readouterr().out + doc.read_text(encoding="utf-8")
    assert "Secretco" not in printed and "company:c0" not in printed
    assert "Prices dated <!-- gen:prices-date -->2 Jan 2026<!-- /gen -->." in printed


def test_a_changed_number_in_a_fragment_fails_the_check(tmp_path, reports, capsys):
    doc = skeleton(tmp_path)
    cs.main(["--update", str(doc), *reports])
    text = doc.read_text(encoding="utf-8")
    assert "| opus-P0 | Opus 5.5 | P0 | 20/20 | 20/20 = 1.00 (" in text
    doc.write_text(text.replace("| 20/20 = 1.00 (", "| 19/20 = 0.95 (", 1), encoding="utf-8")
    assert cs.main(["--check", str(doc), *reports]) == 1
    err = capsys.readouterr().err
    assert "fragment 'results-7' differs" in err and "19/20 = 0.95" in err


def test_a_hand_typed_number_in_prose_fails_the_check(tmp_path, reports, capsys):
    doc = skeleton(tmp_path, extra="Opus agreed on 67 of them.\n")
    cs.main(["--update", str(doc), *reports])
    assert cs.main(["--check", str(doc), *reports]) == 1
    err = capsys.readouterr().err
    assert "number in prose outside a fragment, line 4: Opus agreed on 67 of them." in err
    assert "line 3" not in err  # P0, F1, 95%, 5.5 and seed 7/8 are allowed


def test_a_missing_or_unknown_fragment_fails(tmp_path, reports, capsys):
    doc = skeleton(tmp_path)
    cs.main(["--update", str(doc), *reports])
    text = doc.read_text(encoding="utf-8").replace("<!-- gen:cost -->", "<!-- gen:costs -->")
    doc.write_text(text, encoding="utf-8")
    assert cs.main(["--check", str(doc), *reports]) == 1
    err = capsys.readouterr().err
    assert "fragment 'cost' is missing" in err and "unknown fragment 'costs'" in err


def test_paired_verdicts_follow_the_rule(reports):
    reps = cs.load(reports)
    r7, r8 = reps[7], reps[8]
    t7 = [row["truth"] for row in r7["rows"]]
    # a perfect rater (20 of 20 right) vs always no (13 right: rows 8-20)
    point, lo, hi, verdict = cs.test(r7, cs.rater(r7, "opus-P0"), None)
    assert point == pytest.approx((20 - 13) / 20)
    assert (point, lo, hi) == S.paired_bootstrap_diff(t7, cs.labels(r7, cs.rater(r7, "opus-P0")), ["no"] * 20,
                                                      S.agreement, reps=300, seed=7)
    assert verdict == "better" and lo > 0
    # a rater identical to always no: gap 0, interval [0, 0], not shown better
    assert cs.test(r7, cs.rater(r7, "haiku-P0"), None)[3] == "not shown better"
    # always yes (4 right of 20) vs always no (13 right): worse
    assert cs.test(r7, cs.rater(r7, "sonnet-P0"), None)[3] == "worse"
    # seed 8: opus-P4 (always no, 3 right of 10) vs opus-P0 (perfect)
    assert cs.test(r8, cs.rater(r8, "opus-P4"), cs.rater(r8, "opus-P0"))[:1] == (pytest.approx(-0.7),)
    frag = cs.f_tests(reps)
    assert cs.RULE in frag and "| seed 8 | opus-P4 vs opus-P0 | 3 vs 10 of 10 | -70.0 |" in frag


def test_results_rows_baseline_and_cost(reports):
    reps = cs.load(reports)
    frag = cs.f_results(reps[7])
    # perfect rater: precision and recall 4/4; cost 20 rows x (1000 x $5 + 100 x $25) / 1e6 = $0.15, per true yes / 4
    assert "| 100% (4/4) | 100% (4/4) | $0.0075 | $0.0375 |" in frag
    assert '| always "no" | none | – | 20/20 | 13/20 = 0.65 (' in frag and "| 0% (0/4) |" in frag
    assert "undefined (never says yes)" in frag


def test_reports_without_prices_or_with_an_unknown_prompt_are_refused(tmp_path, reports, capsys):
    data = json.loads(Path(reports[0]).read_text(encoding="utf-8"))
    del data["prices"]
    bad = tmp_path / "old.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    assert cs.main([str(bad), reports[1]]) == 2
    assert "regenerate it with `ninshubur evaluate ... --prices FILE -o ...`" in capsys.readouterr().err
    assert cs.main([reports[1]]) == 2
    assert "no report for seed 7" in capsys.readouterr().err
    data = json.loads(Path(reports[1]).read_text(encoding="utf-8"))
    data["raters"][1]["rater"] = "opus-P3"
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps(data), encoding="utf-8")
    assert cs.main([reports[0], str(odd)]) == 2
    assert "rater opus-P3 has prompt hash 44444444, which is P4" in capsys.readouterr().err
