"""Agreement statistics against values worked out by hand (formulas written out in each test)."""
from __future__ import annotations

import math

import pytest

from ninshubur import stats as S


def test_wilson_hand_values():
    # p = .71, z^2 = 3.8415: centre (.71 + z^2/200) / (1 + z^2/100) = .70224; half-width .08762
    lo, hi = S.wilson(71, 100)
    assert lo == pytest.approx(0.61461, abs=1e-5) and hi == pytest.approx(0.78985, abs=1e-5)
    # 0 of n: lower bound 0, upper z^2 / (n + z^2); n of n mirrors it
    assert S.wilson(0, 10) == (0.0, pytest.approx(3.841459 / 13.841459, abs=1e-6))
    lo, hi = S.wilson(10, 10)
    assert lo == pytest.approx(10 / 13.841459, abs=1e-6) and hi == pytest.approx(1.0)
    with pytest.raises(ValueError):
        S.wilson(1, 0)
    with pytest.raises(ValueError):
        S.wilson(11, 10)


def test_kappa_textbook_example():
    # 50 rows: yes/yes 20, yes/no 5, no/yes 10, no/no 15. po = .7; pe = (25*30 + 25*20)/2500 = .5; kappa = .4
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert S.agreement(a, b) == pytest.approx(0.7)
    assert S.cohen_kappa(a, b) == pytest.approx(0.4)
    assert S.cohen_kappa(a, a) == pytest.approx(1.0)
    assert math.isnan(S.cohen_kappa(["no"] * 5, ["no"] * 5))  # chance agreement is 1: kappa undefined
    with pytest.raises(ValueError):
        S.cohen_kappa(["yes"], ["yes", "no"])


def test_kappa_counts_an_extra_label_as_disagreement():
    a = ["yes", "no", "no", "yes"]
    b = ["yes", "no", "failed", "yes"]
    # po = .75; pe = (2*2 + 2*1)/16 = .375; kappa = (.75 - .375)/(.625) = .6
    assert S.cohen_kappa(a, b) == pytest.approx(0.6)


def test_mcnemar_exact_hand_value():
    # b = 11, c = 4, n = 15: 2 * (C(15,0)+...+C(15,4)) / 2^15 = 2 * 1941 / 32768
    assert S.mcnemar_exact(11, 4) == pytest.approx(2 * 1941 / 32768)
    assert S.mcnemar_exact(4, 11) == S.mcnemar_exact(11, 4)
    assert S.mcnemar_exact(0, 0) == 1.0
    assert S.mcnemar_exact(3, 3) == 1.0  # capped at 1


def test_confusion_and_precision_recall():
    t = ["yes", "yes", "no", "unsure", "no"]
    p = ["yes", "no", "yes", "failed", "no"]
    m = S.confusion(t, p, ["yes", "unsure", "no"])
    assert m["yes"] == {"yes": 1, "unsure": 0, "no": 1, "failed": 0}
    assert m["unsure"]["failed"] == 1 and m["no"]["yes"] == 1 and m["no"]["no"] == 1
    pr = S.precision_recall(t, p, "yes")
    assert pr == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
    assert S.precision_recall(["no"], ["no"], "yes")["precision"] is None
    assert S.binary(t, "yes") == ["yes", "yes", "rest", "rest", "rest"]
    with pytest.raises(ValueError):
        S.confusion(["maybe"], ["yes"], ["yes", "no"])


def test_bootstrap_is_seeded_and_brackets_the_point():
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    one = S.bootstrap(a, b, S.cohen_kappa, reps=2000, seed=7)
    assert one == S.bootstrap(a, b, S.cohen_kappa, reps=2000, seed=7)
    assert one != S.bootstrap(a, b, S.cohen_kappa, reps=2000, seed=8)
    assert one[0] < 0.4 < one[1]


def test_paired_difference_of_identical_raters_is_zero():
    t = ["yes", "no", "no", "unsure"] * 10
    x = ["yes", "no", "yes", "unsure"] * 10
    point, lo, hi = S.paired_bootstrap_diff(t, x, x, S.agreement, reps=500)
    assert point == lo == hi == 0.0
    point, lo, hi = S.paired_bootstrap_diff(t, t, x, S.agreement, reps=500)
    assert point == pytest.approx(0.25) and lo <= 0.25 <= hi


def test_f1_hand_values():
    # the textbook table above, "yes" as positive: tp 20, fp 10, fn 5. precision 2/3, recall 4/5,
    # F1 = 2 * (2/3 * 4/5) / (2/3 + 4/5) = 2 * 20 / (2 * 20 + 10 + 5) = 40/55 = 8/11
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert S.f1(a, b, "yes") == pytest.approx(8 / 11)
    # "unsure" in truth and "failed" in pred count as not "yes": tp 1 (row 1), fp 1 (row 3), fn 1 (row 2) -> 2/4
    assert S.f1(["yes", "yes", "unsure", "no"], ["yes", "failed", "yes", "no"], "yes") == pytest.approx(0.5)
    # both rates defined but no true positive: precision 0/1, recall 0/1 -> 0.0, not None
    assert S.f1(["yes", "no"], ["no", "yes"], "yes") == 0.0


def test_f1_edge_cases():
    t = ["yes", "no", "unsure", "yes", "no"]
    assert S.f1(t, t, "yes") == 1.0                          # a perfect rater
    assert S.f1(t, ["no"] * 5, "yes") is None                # never says yes: precision undefined
    assert S.f1(["no"] * 5, t, "yes") is None                # no yes in truth: recall undefined
    assert S.f1(["no"] * 5, ["no"] * 5, "yes") is None       # neither has a yes
    assert math.isnan(S.f1_stat("yes")(t, ["no"] * 5)) and S.f1_stat("yes")(t, t) == 1.0


def test_f1_bootstrap_is_seeded_and_brackets_the_point():
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    one = S.bootstrap(a, b, S.f1_stat("yes"), reps=2000, seed=7)
    assert one == S.bootstrap(a, b, S.f1_stat("yes"), reps=2000, seed=7)
    assert one[0] < 8 / 11 < one[1] and 0 <= one[0] and one[1] <= 1
    assert S.bootstrap(a, a, S.f1_stat("yes"), reps=200) == (1.0, 1.0)  # every resample of a perfect rater
    lo, hi = S.bootstrap(a, ["no"] * 50, S.f1_stat("yes"), reps=200)    # undefined in every resample
    assert math.isnan(lo) and math.isnan(hi)
