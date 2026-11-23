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

import csv
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
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
                  "item_ids": list(s.item_ids), "total": s.total} for i, (s, stratum) in enumerate(sheet.rows, 1)],
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


# --- reading a filled sheet ----------------------------------------------------------------------
# A filled sheet comes back from a spreadsheet program. LibreOffice, for one, saves it again with a byte-order mark
# and may add columns with an empty header; both are accepted. What is not accepted is evidence that differs from
# what was drawn: every row's evidence hash is rebuilt from its cells and compared with the meta file, so the
# labels are known to be about the evidence a model is then asked on.

@dataclass(frozen=True)
class LabelledRow:
    row: int
    subject: Subject
    stratum: str
    label: str | None     # lower-cased; None when the cell is empty
    note: str
    extra: dict = field(default_factory=dict)  # any other named column (a rater sheet's cited, reason, status, ...)


@dataclass(frozen=True)
class FilledSheet:
    path: Path
    sha256: str
    meta: dict
    rows: list[LabelledRow]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self.meta["labels"])

    @property
    def positive(self) -> str:
        return str(self.meta.get("positive") or self.meta["labels"][0])

    def unlabelled(self) -> list[int]:
        return [r.row for r in self.rows if r.label is None]


def _unguard(cell: str) -> str:
    """Undo ``_cell``'s apostrophe, and only that: a leading apostrophe that ``_cell`` would have added."""
    if cell.startswith("'") and _cell(cell[1:]) == cell:
        return cell[1:]
    return cell


def _evidence_lines(cell: str, row: int) -> tuple[str, ...]:
    lines = []
    for n, raw in enumerate(cell.replace("\r\n", "\n").split("\n"), 1):
        prefix = f"[{n}] "
        if not raw.startswith(prefix):
            raise LabelSheetError(f"row {row}: evidence line {n} does not start with {prefix.strip()!r}; "
                                  "the evidence cells must stay as drawn (copy them from the original sheet)")
        lines.append(raw[len(prefix):])
    return tuple(lines)


def read_sheet(path: Path, meta: dict | None = None) -> FilledSheet:
    """Read a filled label sheet and check it against its meta file (or ``meta``, to read a second rater's copy of
    the same sheet). Raises LabelSheetError naming the row and the fix."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise LabelSheetError(f"cannot read {path}: {e.strerror or e}") from None
    if meta is None:
        mp = meta_path(path)
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LabelSheetError(f"{mp.name} is missing; a sheet is read with the meta file written beside it when it was "
                                  "drawn (pass the original sheet's meta for a copy)") from None
        except ValueError as e:
            raise LabelSheetError(f"{mp.name} is not valid JSON ({e})") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LabelSheetError(f"{path.name} is not UTF-8; save it again as CSV with UTF-8 encoding") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h for h in (reader.fieldnames or []) if h]
    missing = [c for c in ("row", "subject_key", "name", "evidence", "label") if c not in header]
    if missing:
        raise LabelSheetError(f"{path.name} lacks column(s) {', '.join(missing)}; keep the header row as drawn")
    labels = [str(x).lower() for x in meta["labels"]]
    want = {int(r["row"]): r for r in meta["rows"]}
    out: list[LabelledRow] = []
    seen: set[int] = set()
    for rec in reader:
        if not any((v or "").strip() for k, v in rec.items() if k):
            continue  # a blank line a spreadsheet left at the end
        try:
            n = int(str(rec["row"]).strip())
        except ValueError:
            raise LabelSheetError(f"{path.name}: row number {rec['row']!r} is not a number; keep the row column as drawn") from None
        m = want.get(n)
        if m is None or n in seen:
            raise LabelSheetError(f"row {n}: {'repeated' if n in seen else 'not in the meta file'}; rows must not be added or copied")
        seen.add(n)
        key = _unguard(rec["subject_key"] or "")
        if key != m["subject_key"]:
            raise LabelSheetError(f"row {n}: subject {key!r} is not {m['subject_key']!r} from the meta file; rows must not be re-sorted "
                                  "in a way that moves cells between rows")
        lines = _evidence_lines(rec["evidence"] or "", n)
        name = _unguard(rec["name"] or "")
        s = Subject(key=key, name=name, lines=lines, item_ids=tuple(int(i) for i in m.get("item_ids", ())),
                    total=int(m.get("total") or len(lines)), sources=())
        if s.evidence_hash != m["evidence_hash"]:
            raise LabelSheetError(f"row {n} ({name}): the name or evidence differs from what was drawn; labels must be about the "
                                  "evidence as drawn, so undo the edit (the original is in the sheet's first copy)")
        label = (rec.get("label") or "").strip().lower() or None
        if label is not None and label not in labels:
            raise LabelSheetError(f"row {n} ({name}): label {label!r} is not one of {', '.join(labels)}")
        extra = {k: v for k, v in rec.items() if k and k not in COLUMNS}
        out.append(LabelledRow(row=n, subject=s, stratum=str(m.get("stratum") or ""), label=label,
                               note=(rec.get("note") or "").strip(), extra=extra))
    lost = sorted(set(want) - seen)
    if lost:
        raise LabelSheetError(f"{path.name}: row(s) {', '.join(map(str, lost[:10]))}{' ...' if len(lost) > 10 else ''} are missing; "
                              "rows must not be deleted")
    out.sort(key=lambda r: r.row)
    return FilledSheet(path=path, sha256=hashlib.sha256(raw).hexdigest(), meta=meta, rows=out)
