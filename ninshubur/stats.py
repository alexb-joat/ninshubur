"""Agreement statistics for ``ninshubur evaluate``: how often a model gives the label a person gave.

Standard library only. Every interval that resamples is seeded, so a report reproduces on any machine.

- ``wilson``: the 95% interval of a proportion (k of n). Unlike the normal approximation it stays inside
  [0, 1] and is usable at 0 of n and n of n.
- ``cohen_kappa``: agreement beyond what the two label counts would give by chance (1 = perfect, 0 = chance).
- ``f1``: the harmonic mean of precision and recall for one label; ``f1_stat`` is its form for ``bootstrap``.
- ``bootstrap`` and ``paired_bootstrap_diff``: percentile intervals from resampling rows with replacement.
  The paired form resamples the same rows for both raters, so it compares two models on the same subjects.
- ``mcnemar_exact``: whether two paired raters disagree in one direction more than the other (two-sided
  exact binomial test on the discordant counts).
"""
from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Sequence

Z95 = 1.959963984540054  # two-sided 95% normal quantile
REPS = 10_000
SEED = 7

Labels = Sequence[str]
Stat = Callable[[Labels, Labels], float]


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials."""
    if n <= 0:
        raise ValueError("wilson: n must be positive")
    if not 0 <= k <= n:
        raise ValueError(f"wilson: k={k} is outside 0..{n}")
    p = k / n
    d = 1 + z * z / n
    mid = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (mid - half) / d), min(1.0, (mid + half) / d)


def _pairs(a: Labels, b: Labels) -> None:
    if len(a) != len(b):
        raise ValueError(f"the two label lists differ in length ({len(a)} and {len(b)})")
    if not a:
        raise ValueError("no labels to compare")


def agreement(a: Labels, b: Labels) -> float:
    """Share of rows where both gave the same label."""
    _pairs(a, b)
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def cohen_kappa(a: Labels, b: Labels) -> float:
    """Cohen's kappa. NaN when chance agreement is 1 (both raters gave one and the same label to every row)."""
    _pairs(a, b)
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe >= 1:
        return float("nan")
    return (po - pe) / (1 - pe)


def confusion(truth: Labels, pred: Labels, labels: Sequence[str]) -> dict[str, dict[str, int]]:
    """counts[truth_label][pred_label]. Predicted labels outside ``labels`` (such as "failed") get their own column."""
    _pairs(truth, pred)
    cols = list(labels) + sorted({p for p in pred if p not in labels})
    out = {t: {p: 0 for p in cols} for t in labels}
    for t, p in zip(truth, pred, strict=True):
        if t not in out:
            raise ValueError(f"truth label {t!r} is not one of {', '.join(labels)}")
        out[t][p] += 1
    return out


def binary(labels: Labels, positive: str) -> list[str]:
    """The positive label against everything else."""
    return [positive if x == positive else "rest" for x in labels]


def precision_recall(truth: Labels, pred: Labels, positive: str) -> dict:
    """Counts and rates for the positive label. A rate with an empty denominator is None, not 0."""
    _pairs(truth, pred)
    tp = sum(1 for t, p in zip(truth, pred, strict=True) if t == p == positive)
    fp = sum(1 for t, p in zip(truth, pred, strict=True) if p == positive and t != positive)
    fn = sum(1 for t, p in zip(truth, pred, strict=True) if t == positive and p != positive)
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def f1(truth: Labels, pred: Labels, positive: str) -> float | None:
    """F1 for the positive label: 2·precision·recall / (precision + recall), which is 2·tp / (2·tp + fp + fn).

    None when precision or recall is undefined: no row has the positive label in ``truth`` (nothing to recall), or
    no row has it in ``pred`` (a rater that never says it has no precision). 0.0 when both are defined and tp is 0.
    Any label other than ``positive`` (including "failed") counts as not positive.
    """
    pr = precision_recall(truth, pred, positive)
    p, r = pr["precision"], pr["recall"]
    if p is None or r is None:
        return None
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def f1_stat(positive: str) -> Stat:
    """``f1`` as a ``bootstrap`` statistic: NaN where F1 is undefined, so ``bootstrap`` drops that resample."""
    def stat(a: Labels, b: Labels) -> float:
        v = f1(a, b, positive)
        return float("nan") if v is None else v
    return stat


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts b and c."""
    if b < 0 or c < 0:
        raise ValueError("mcnemar_exact: counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _percentile(values: list[float], level: float) -> tuple[float, float]:
    values = sorted(v for v in values if not math.isnan(v))
    if not values:
        return float("nan"), float("nan")
    alpha = (1 - level) / 2
    lo = values[int(alpha * len(values))]
    hi = values[max(0, int((1 - alpha) * len(values)) - 1)]
    return lo, hi


def _draws(n: int, reps: int, seed: int):
    rnd = random.Random(seed)  # noqa: S311 — scheduling jitter / resampling, not security
    for _ in range(reps):
        yield [rnd.randrange(n) for _ in range(n)]


def bootstrap(a: Labels, b: Labels, stat: Stat, *, reps: int = REPS, seed: int = SEED,
              level: float = 0.95) -> tuple[float, float]:
    """Percentile interval of ``stat(a, b)`` over row resamples. Resamples where the statistic is NaN are dropped."""
    _pairs(a, b)
    vals = [stat([a[i] for i in idx], [b[i] for i in idx]) for idx in _draws(len(a), reps, seed)]
    return _percentile(vals, level)


def paired_bootstrap_diff(truth: Labels, x: Labels, y: Labels, stat: Stat, *, reps: int = REPS, seed: int = SEED,
                          level: float = 0.95) -> tuple[float, float, float]:
    """``stat(truth, x) - stat(truth, y)`` and its percentile interval, both raters resampled on the same rows."""
    _pairs(truth, x)
    _pairs(truth, y)
    point = stat(truth, x) - stat(truth, y)
    vals = []
    for idx in _draws(len(truth), reps, seed):
        t = [truth[i] for i in idx]
        vals.append(stat(t, [x[i] for i in idx]) - stat(t, [y[i] for i in idx]))
    lo, hi = _percentile(vals, level)
    return point, lo, hi
