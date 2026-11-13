"""Label sheet: a blind, stratified sample of subjects for a person to label by hand.

Blind. A row holds what a model would be shown (the name and the numbered evidence lines from
``analyze/qualify.py``) and two empty cells, ``label`` and ``note``. Nothing a model said is read, so a
stored answer cannot leak into the sheet.

Stratified. Each subject falls in one stratum, known without any model: the sources its evidence came
from, and whether it has few, some or many evidence lines (thirds of the population). The sample is
shared out over the strata in proportion to their size, every stratum gets at least one row when the
sample allows, and no stratum gives more than it has.

Repeatable. Which subjects are drawn, and the row order, come from SHA-256 of the seed and the subject
key, not from a random generator, so the same seed gives the same sheet on any machine and Python
version, and a subject's place does not depend on the others. Rows are in that hashed order, so the
order says nothing about the stratum.

Beside the sheet goes ``<sheet>.meta.json``: the question, the allowed labels, the seed, the strata
with population and sample counts, and per row the stratum, the evidence hash and the item ids. An
evaluation can check from it that the evidence a person labelled is the evidence a model was asked on.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .analyze.qualify import MAX_LINES, Subject, prompt_hash
from .export import _cell, _writer
from .logs import write_private
from .profiles import Profile

COLUMNS = ["row", "subject_key", "name", "lines", "evidence", "label", "note"]
BANDS = ("few", "some", "many")


class LabelSheetError(ValueError):
    pass


@dataclass(frozen=True)
class Sheet:
    rows: list[tuple[Subject, str]]          # (subject, stratum) in sheet order
    strata: dict[str, tuple[int, int]]       # stratum -> (population, sampled)
    population: int
    seed: int
    asked: int


def _rank(seed: int, what: str, key: str) -> str:
    return hashlib.sha256(f"{seed}:{what}:{key}".encode("utf-8")).hexdigest()


def stratify(subjects: list[Subject]) -> dict[str, str]:
    """Subject key -> stratum name: ``<sources>/<band>``. The bands are thirds of the evidence counts."""
    if not subjects:
        return {}
    totals = sorted(s.total for s in subjects)
    n = len(totals)
    cut1, cut2 = totals[(n - 1) // 3], totals[2 * (n - 1) // 3]
    out = {}
    for s in subjects:
        band = BANDS[0] if s.total <= cut1 else BANDS[1] if s.total <= cut2 else BANDS[2]
        out[s.key] = f"{'+'.join(s.sources) or 'unknown'}/{band}"
    return out


def allocate(sizes: dict[str, int], n: int) -> dict[str, int]:
    """Share ``n`` rows over strata of the given sizes: in proportion, at least one each when ``n`` allows,
    never more than a stratum has. Deterministic (ties go to the stratum whose name sorts first)."""
    total = sum(sizes.values())
    n = max(0, min(n, total))
    if n == total:
        return dict(sizes)
    alloc = {k: (1 if n >= len(sizes) and sizes[k] > 0 else 0) for k in sizes}
    quota = {k: n * sizes[k] / total for k in sizes}
    while sum(alloc.values()) < n:
        k = min((k for k in sizes if alloc[k] < sizes[k]), key=lambda k: (-(quota[k] - alloc[k]), k))
        alloc[k] += 1
    return alloc


def draw(subjects: list[Subject], sample: int, seed: int) -> Sheet:
    if sample < 1:
        raise LabelSheetError("--sample must be at least 1")
    strata = stratify(subjects)
    groups: dict[str, list[Subject]] = {}
    for s in subjects:
        groups.setdefault(strata[s.key], []).append(s)
    alloc = allocate({k: len(v) for k, v in groups.items()}, sample)
    picked: list[tuple[Subject, str]] = []
    for name in sorted(groups):
        ranked = sorted(groups[name], key=lambda s: _rank(seed, "draw", s.key))
        picked.extend((s, name) for s in ranked[:alloc[name]])
    picked.sort(key=lambda r: _rank(seed, "order", r[0].key))
    return Sheet(rows=picked, strata={k: (len(groups[k]), alloc[k]) for k in sorted(groups)}, population=len(subjects),
                 seed=seed, asked=sample)


def render_csv(sheet: Sheet) -> str:
    buf = io.StringIO()
    w = _writer(buf, COLUMNS)
    for i, (s, _stratum) in enumerate(sheet.rows, 1):
        evidence = "\n".join(f"[{n}] {line}" for n, line in enumerate(s.lines, 1))
        w.writerow([_cell(v) for v in (i, s.key, s.name, len(s.lines), evidence, "", "")])
    return "﻿" + buf.getvalue()


def render_meta(sheet: Sheet, profile: Profile, now: float) -> str:
    q = profile.qualify
    assert q is not None
    return json.dumps({
        "profile": profile.name, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "seed": sheet.seed, "sample_asked": sheet.asked, "sampled": len(sheet.rows), "population": sheet.population,
        "question": q.question, "subject": q.subject, "must": list(q.must), "must_not": list(q.must_not),
        "labels": list(q.labels), "positive": q.positive, "prompt_hash": prompt_hash(q), "max_lines": MAX_LINES,
        "columns": COLUMNS,
        "strata": [{"stratum": k, "population": pop, "sampled": got} for k, (pop, got) in sheet.strata.items()],
        "rows": [{"row": i, "subject_key": s.key, "stratum": stratum, "evidence_hash": s.evidence_hash,
                  "item_ids": list(s.item_ids)} for i, (s, stratum) in enumerate(sheet.rows, 1)],
    }, indent=2, ensure_ascii=False) + "\n"


def default_path(home: Path, profile: str, seed: int) -> Path:
    return home / "feeds" / f"label-sheet-{profile}-seed{seed}.csv"


def meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def write(subjects: list[Subject], profile: Profile, home: Path, *, sample: int, seed: int, now: float,
          out: Path | None = None, force: bool = False) -> tuple[Path, Sheet]:
    """Draw the sample and write the sheet and its meta file. An existing sheet is never overwritten
    unless ``force``: it may hold hours of labelling."""
    if profile.qualify is None:
        raise LabelSheetError(f"profile {profile.name} has no qualify: block; the sheet needs its question and labels")
    if not subjects:
        raise LabelSheetError("no subjects with evidence yet; run `ninshubur run` first")
    path = out or default_path(home, profile.name, seed)
    if path.exists() and not force:
        raise LabelSheetError(f"{path} exists and may hold labels; choose another file with -o, or pass --force to replace it")
    sheet = draw(subjects, sample, seed)
    write_private(path, render_csv(sheet))
    write_private(meta_path(path), render_meta(sheet, profile, now))
    return path, sheet
