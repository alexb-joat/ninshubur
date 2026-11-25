"""Evaluation: how often a model gives the label a person gave, on a filled label sheet.

The person's labels are the reference. A rater is anything that labelled the same rows:

- stored answers from ``ninshubur qualify`` (one model, one prompt hash), matched to a sheet row only when the
  answer's evidence hash is the row's, so an answer about other evidence is never scored;
- a second copy of the sheet with its label column filled (another person, or a model run outside Ninshubur),
  read with the first sheet's meta, so its evidence is checked the same way.

A row a rater did not answer, or answered without grounding, gets the label ``failed``: it counts as a
disagreement and is also reported on its own. Nothing here sends anything; the only write is the report: the
database is opened with ``db.connect_readonly`` (never created, migrated, chmodded or written), and a database
without the tables it reads is refused with a message, not migrated.

Numbers (see ``stats.py``): agreement with a Wilson 95% interval; Cohen's kappa with a seeded bootstrap
interval; for the positive label (the first answer), precision, recall, F1 with a bootstrap interval and an exact
McNemar test of whether the rater says it more or less often than the person; the "always the commonest label"
baseline. A local
model is compared with the best remote rater by the paired bootstrap difference in agreement, and called
non-inferior when that interval's lower bound is at least ``-MARGIN`` and it failed on at most
``MAX_FAILED`` rows (rule fixed on 23 Nov 2026, before any model was run).

Tokens: each rater's input and output token totals over the rows it was scored on, as each call reported them
(a sheet copy's ``input_tokens``/``output_tokens`` columns; ``ninshubur qualify``'s stored counts), and the median
latency per row. With a provider-facts file (``ninshubur export euboulia-facts``, read by ``load_prices``), each
rater also gets its list-price cost: input and output priced separately at the file's USD per million tokens, a row
whose input is over the model's ``longContext.threshold`` at the long-context rates. That is what the same calls
would cost at the published prices on the file's date, not what was paid. A model run on this machine costs 0 at
list price; electricity is not counted.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import stats as S
from .db import rows
from .detectors import load_json
from .labelsheet import FilledSheet, LabelSheetError, read_sheet

FAILED = "failed"
MARGIN = 0.10
MAX_FAILED = 5


@dataclass
class Rater:
    name: str
    kind: str                  # "stored" (ninshubur qualify) or "sheet" (a filled copy of the sheet)
    local: bool                # ran on this machine
    prompt_hash: str | None
    source: str                # run ids, or the copy's file name and sha256
    labels: list[str]          # one per sheet row, in row order; FAILED where there is no usable answer
    failed: int = 0            # answered but not grounded
    missing: int = 0           # not answered at all
    model: str | None = None   # the model id the answers name (a sheet's ``model`` column), for pricing
    usage: list[tuple[int, int]] | None = None  # (input, output) tokens per row that reported them; None: none recorded
    latencies: list[int] = field(default_factory=list)  # milliseconds, per row that reported it
    answered_at: tuple[float, float] | None = None  # first and last stored answer (stored raters)
    written_at: float | None = None  # the copy's file modification time (sheet raters)


def stored_pairs(conn: sqlite3.Connection, sheet: FilledSheet) -> list[tuple[str, str]]:
    """(model, prompt_hash) pairs with at least one answer on this sheet's evidence, oldest first."""
    want = {(r.subject.key, r.subject.evidence_hash) for r in sheet.rows}
    seen: dict[tuple[str, str], None] = {}
    for r in rows(conn, "SELECT subject_key, evidence_hash, model, prompt_hash FROM qualifications WHERE profile=? ORDER BY id",
                  (sheet.meta["profile"],)):
        if (r["subject_key"], r["evidence_hash"]) in want:
            seen[(r["model"], r["prompt_hash"])] = None
    return list(seen)


def stored_rater(conn: sqlite3.Connection, sheet: FilledSheet, model: str, prompt_hash: str) -> Rater:
    """The newest answer per row from ``ninshubur qualify`` for one model and prompt, on the row's own evidence."""
    newest: dict[tuple[str, str], dict] = {}
    for r in rows(conn, """SELECT q.id, q.run_id, q.subject_key, q.evidence_hash, q.status, q.label, l.slot,
                                  q.input_tokens, q.output_tokens, q.latency_ms, q.created_at
                           FROM qualifications q JOIN llm_runs l ON l.id = q.run_id
                           WHERE q.profile=? AND q.model=? AND q.prompt_hash=? ORDER BY q.id""",
                  (sheet.meta["profile"], model, prompt_hash)):
        newest[(r["subject_key"], r["evidence_hash"])] = r
    labels, failed, missing, runs, local = [], 0, 0, set(), True
    usage, latencies, times = [], [], []
    for row in sheet.rows:
        r = newest.get((row.subject.key, row.subject.evidence_hash))
        if r is None:
            labels.append(FAILED)
            missing += 1
            continue
        runs.add(r["run_id"])
        usage.append((r["input_tokens"] or 0, r["output_tokens"] or 0))
        times.append(r["created_at"])
        if r["latency_ms"] is not None:
            latencies.append(r["latency_ms"])
        local = local and r["slot"] == "local"
        if r["status"] == "ok" and r["label"] in sheet.labels:
            labels.append(r["label"])
        else:
            labels.append(FAILED)
            failed += 1
    return Rater(name=model, kind="stored", local=local and bool(runs), prompt_hash=prompt_hash,
                 source="run " + ", ".join(map(str, sorted(runs))) if runs else "no run", labels=labels, failed=failed,
                 missing=missing, model=model, usage=usage if runs else None, latencies=latencies,
                 answered_at=(min(times), max(times)) if times else None)


def _count(raw, what: str, path: Path, row: int) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        v = -1
    if v < 0:
        raise LabelSheetError(f"{Path(path).name} row {row}: {what} is {raw!r}, not a whole number of 0 or more")
    return v


def sheet_rater(path: Path, sheet: FilledSheet, name: str | None = None, *, local: bool = False) -> Rater:
    """A filled copy of the sheet. Optional columns, read if present: ``status`` (ok/failed), ``prompt_hash``,
    ``model``, and per row ``input_tokens``, ``output_tokens`` and ``latency_ms``."""
    copy = read_sheet(path, meta=sheet.meta)
    labels, failed, missing, hashes, models = [], 0, 0, set(), set()
    usage, latencies, has_tokens = [], [], False
    for r in copy.rows:
        status = (r.extra.get("status") or "").strip().lower()
        if r.extra.get("prompt_hash"):
            hashes.add(r.extra["prompt_hash"].strip())
        if (r.extra.get("model") or "").strip():
            models.add(r.extra["model"].strip())
        i, o = (_count(r.extra.get(c), c, path, r.row) for c in ("input_tokens", "output_tokens"))
        has_tokens = has_tokens or "input_tokens" in r.extra or "output_tokens" in r.extra
        if i is not None or o is not None:
            usage.append((i or 0, o or 0))
        ms = _count(r.extra.get("latency_ms"), "latency_ms", path, r.row)
        if ms is not None:
            latencies.append(ms)
        if r.label is None:
            labels.append(FAILED)
            if status and status != "ok":
                failed += 1
            else:
                missing += 1
        else:
            labels.append(r.label)
    if len(hashes) > 1:
        raise LabelSheetError(f"{Path(path).name} mixes answers from {len(hashes)} prompts; score one prompt per file")
    if len(models) > 1:
        raise LabelSheetError(f"{Path(path).name} mixes answers from {len(models)} models ({', '.join(sorted(models))}); "
                              "score one model per file")
    return Rater(name=name or Path(path).stem, kind="sheet", local=local, prompt_hash=next(iter(hashes), None),
                 source=f"{Path(path).name} (sha256 {copy.sha256[:16]})", labels=labels, failed=failed, missing=missing,
                 model=next(iter(models), None), usage=usage if has_tokens else None, latencies=latencies,
                 written_at=Path(path).stat().st_mtime)


# --- list prices -----------------------------------------------------------------------------------
def load_prices(path: Path) -> dict:
    """A provider-facts file's prices, indexed by model. Every model with a ``price`` is kept and found by its key
    (``anthropic/claude-opus-5.5``), the key's last part (``claude-opus-5.5``) or its ``apiId`` (``claude-opus-5-5``)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        facts = load_json(raw)
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read the prices file {path}: {e}") from None
    if not isinstance(facts, dict) or not isinstance(facts.get("providers"), dict):
        raise ValueError(f"{path.name} has no \"providers\" object; pass a file from `ninshubur export euboulia-facts`")
    index: dict[str, dict] = {}
    for prov, pdata in facts["providers"].items():
        pdata = pdata if isinstance(pdata, dict) else {}
        sources = pdata.get("sources") if isinstance(pdata.get("sources"), dict) else {}
        for key, m in (pdata.get("models") or {}).items():
            price = m.get("price") if isinstance(m, dict) else None
            if not isinstance(price, dict):
                continue
            rates = [("input", price.get("input")), ("output", price.get("output"))]
            lc = price.get("longContext")
            if lc is not None:
                rates += [(f"longContext.{k}", lc.get(k) if isinstance(lc, dict) else None)
                          for k in ("threshold", "input", "output")]
            for what, v in rates:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise ValueError(f"{path.name}: models[{key!r}].price.{what} is {v!r}, not a number of 0 or more")
            pricing = sources.get("pricing") if isinstance(sources.get("pricing"), dict) else {}
            entry = {"key": key, "provider": prov, "price": price, "source": pricing.get("url")}
            for alias in {key, key.rsplit("/", 1)[-1], m.get("apiId")} - {None, ""}:
                index[alias] = entry
    return {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "generatedAt": facts.get("generatedAt"),
            "models": index}


def list_cost(rater: Rater, s: dict, prices: dict) -> dict:
    """List-price cost of the calls behind a rater's scored rows, per company assessed (answered rows) and per true
    positive (rows where the rater and the person both gave the positive label; None when there are none)."""
    def per(usd: float) -> dict:
        return {"usd": usd, "per_company": usd / s["answered"] if s["answered"] else None,
                "per_true_yes": usd / s["positive_tp"] if s["positive_tp"] else None}
    none = {"basis": None, "usd": None, "per_company": None, "per_true_yes": None}
    if rater.local:
        return {"basis": "local", **per(0.0), "note": "run on this machine: 0 at list price; electricity isn't counted"}
    if rater.usage is None:
        return {**none, "note": "no token counts recorded"}
    entry = prices["models"].get(rater.model or rater.name)
    if entry is None:
        return {**none, "note": f"model {rater.model or rater.name!r} has no price in {prices['file']}"}
    price, usd, long_rows = entry["price"], 0.0, 0
    lc = price.get("longContext")
    for i, o in rater.usage:
        rate = price
        if lc and i > lc["threshold"]:
            rate, long_rows = lc, long_rows + 1
        usd += i * rate["input"] / 1e6 + o * rate["output"] / 1e6
    return {"basis": "list price", "model": entry["key"], "price": price, "long_context_rows": long_rows, **per(usd)}


def score(truth: list[str], rater: Rater, labels: tuple[str, ...], positive: str, *, reps: int = S.REPS,
          seed: int = S.SEED) -> dict:
    pred = rater.labels
    n = len(truth)
    k = sum(t == p for t, p in zip(truth, pred))
    tb, pb = S.binary(truth, positive), S.binary(pred, positive)
    pr = S.precision_recall(truth, pred, positive)
    usage = rater.usage or []
    return {
        "rater": rater.name, "kind": rater.kind, "local": rater.local, "prompt_hash": rater.prompt_hash, "source": rater.source,
        "n": n, "answered": n - rater.failed - rater.missing, "failed": rater.failed, "missing": rater.missing,
        "counts": dict(Counter(pred)),
        "agree": k, "agreement": k / n, "agreement_ci": S.wilson(k, n),
        "kappa": S.cohen_kappa(truth, pred), "kappa_ci": S.bootstrap(truth, pred, S.cohen_kappa, reps=reps, seed=seed),
        "positive": positive, "positive_kappa": S.cohen_kappa(tb, pb),
        "positive_kappa_ci": S.bootstrap(tb, pb, S.cohen_kappa, reps=reps, seed=seed),
        **{f"positive_{k_}": v for k_, v in pr.items()},
        "positive_f1": S.f1(truth, pred, positive),
        "positive_f1_ci": S.bootstrap(truth, pred, S.f1_stat(positive), reps=reps, seed=seed),
        "mcnemar_p": S.mcnemar_exact(pr["fp"], pr["fn"]),
        "confusion": S.confusion(truth, pred, list(labels)),
        "usage": None if rater.usage is None else {
            "model": rater.model, "rows": len(usage), "input_tokens": sum(i for i, _ in usage),
            "output_tokens": sum(o for _, o in usage)},
        "median_latency_ms": statistics.median(rater.latencies) if rater.latencies else None,
        "answered_at": [_iso(x) for x in rater.answered_at] if rater.answered_at else None,
        "written_at": _iso(rater.written_at) if rater.written_at is not None else None,
    }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def verdicts(truth: list[str], raters: list[Rater], scores: list[dict], *, reps: int = S.REPS, seed: int = S.SEED) -> None:
    """Add a non-inferiority verdict to each local rater's score, against the best remote rater with the same prompt
    (or, when no remote rater used that prompt, the best remote rater overall). Changes ``scores`` in place."""
    remote = [(r, s) for r, s in zip(raters, scores) if not r.local]
    for r, s in zip(raters, scores):
        if not r.local:
            continue
        pool = [x for x in remote if x[0].prompt_hash == r.prompt_hash] or remote
        if not pool:
            s["verdict"] = {"against": None, "result": "no remote rater to compare with"}
            continue
        best_r, best_s = max(pool, key=lambda x: (x[1]["agreement"], x[1]["kappa"]))
        point, lo, hi = S.paired_bootstrap_diff(truth, r.labels, best_r.labels, S.agreement, reps=reps, seed=seed)
        ok = lo >= -MARGIN and (r.failed + r.missing) <= MAX_FAILED
        s["verdict"] = {"against": best_r.name, "against_prompt": best_r.prompt_hash, "difference": point, "ci": (lo, hi),
                        "margin": MARGIN, "max_failed": MAX_FAILED, "result": "non-inferior" if ok else "not shown non-inferior"}


def evaluate(sheet: FilledSheet, raters: list[Rater], *, reps: int = S.REPS, seed: int = S.SEED, now: float | None = None,
             prices: dict | None = None) -> dict:
    """The report. ``prices``, from ``load_prices``, adds each rater's list-price cost and the file it came from."""
    blank = sheet.unlabelled()
    if blank:
        raise LabelSheetError(f"{sheet.path.name}: {len(blank)} row(s) have no label yet ({', '.join(map(str, blank[:10]))}"
                              f"{' ...' if len(blank) > 10 else ''}); label every row before evaluating")
    truth = [r.label for r in sheet.rows]
    commonest, top = Counter(truth).most_common(1)[0]
    scores = [score(truth, r, sheet.labels, sheet.positive, reps=reps, seed=seed) for r in raters]
    verdicts(truth, raters, scores, reps=reps, seed=seed)
    if prices is not None:
        for r, s in zip(raters, scores):
            s["cost"] = list_cost(r, s, prices)
    rep = {
        "created_at": _iso(now if now is not None else time.time()),
        "sheet": {"file": sheet.path.name, "sha256": sheet.sha256, "profile": sheet.meta["profile"], "seed": sheet.meta.get("seed"),
                  "drawn_at": sheet.meta.get("created_at"), "population": sheet.meta.get("population"),
                  "modified_at": _iso(sheet.path.stat().st_mtime),
                  "question": sheet.meta.get("question"), "prompt_hash": sheet.meta.get("prompt_hash"), "labels": list(sheet.labels),
                  "positive": sheet.positive, "n": len(truth), "counts": dict(Counter(truth)),
                  "totals_recorded": all("total" in r for r in sheet.meta["rows"])},
        "method": {"bootstrap_reps": reps, "seed": seed, "interval": "95%", "margin": MARGIN, "max_failed": MAX_FAILED},
        "baseline": {"label": commonest, "agree": top, "agreement": top / len(truth), "agreement_ci": S.wilson(top, len(truth))},
        "raters": scores,
        "rows": [{"row": r.row, "subject": r.subject.name, "truth": r.label, **{x.name + (f" [{x.prompt_hash}]" if x.prompt_hash else ""):
                  x.labels[i] for x in raters}} for i, r in enumerate(sheet.rows)],
    }
    if prices is not None:
        rep["prices"] = {"file": prices["file"], "sha256": prices["sha256"], "generatedAt": prices["generatedAt"],
                         "sources": sorted({e["source"] for e in prices["models"].values() if e["source"]}),
                         "note": "list-price equivalents: what these calls would cost at the published per-token prices "
                                 "on the file's date, not what was paid; local models cost 0, electricity isn't counted"}
    return rep


# --- rendering -----------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def _ci(ci) -> str:
    lo, hi = ci
    return f"{lo:.2f}–{hi:.2f}"


def _point_ci(point: float | None, ci) -> str:
    if point is None:
        return "–"
    lo, hi = ci
    return f"{point:.2f}" + ("" if math.isnan(lo) or math.isnan(hi) else f" ({lo:.2f}–{hi:.2f})")


def _usd(x: float | None) -> str:
    return "–" if x is None else f"${x:.4f}"


def _prompt(h: str | None, drawn: str | None) -> str:
    if not h:
        return "own"
    return f"{h[:8]} (as drawn)" if h == drawn else h[:8]


def render_markdown(rep: dict) -> str:
    sh, b = rep["sheet"], rep["baseline"]
    out = [f"# Evaluation: profile {sh['profile']}, sheet seed {sh['seed']}", "",
           f"Created {rep['created_at']} from `{sh['file']}` (sha256 {sh['sha256'][:16]}). Reference: the person's labels, "
           f"n = {sh['n']} ({', '.join(f'{k} {v}' for k, v in sorted(sh['counts'].items()))}).", "",
           f"Question: {sh['question']}", "",
           f"Baseline, \"{b['label']}\" every time: {b['agree']}/{sh['n']} ({_ci(b['agreement_ci'])}).", "",
           "| Rater | Prompt | Answered | Agree (95%) | Kappa (95%) | " + f"\"{sh['positive']}\" F1 (95%) | "
           f"\"{sh['positive']}\" precision / recall | McNemar p | Local verdict |",
           "|---|---|---|---|---|---|---|---|---|"]
    for s in rep["raters"]:
        v = s.get("verdict")
        verdict = "–" if not v else (v["result"] if not v.get("against") else
                                     f"{v['result']} vs {v['against']} ({v['difference']:+.2f}, {_ci(v['ci'])})")
        lost = s["failed"] + s["missing"]
        out.append(f"| {s['rater']} | {_prompt(s['prompt_hash'], sh['prompt_hash'])} | {s['answered']}/{s['n']}"
                   + (f" ({lost} without a usable answer)" if lost else "") + " | "
                   f"{s['agree']} ({_ci(s['agreement_ci'])}) | {s['kappa']:.2f} ({_ci(s['kappa_ci'])}) | "
                   f"{_point_ci(s['positive_f1'], s['positive_f1_ci'])} | {_pct(s['positive_precision'])} / {_pct(s['positive_recall'])} | {s['mcnemar_p']:.3f} | {verdict} |")
    m = rep["method"]
    out += ["", f"Method: Wilson intervals for agreement; Cohen's kappa with a percentile bootstrap ({m['bootstrap_reps']} resamples, "
                f"seed {m['seed']}); McNemar is exact, on \"{sh['positive']}\" against the rest. A row without a usable answer counts "
                f"as a disagreement. A local model is non-inferior when the paired bootstrap interval of its agreement minus the best "
                f"remote rater's has a lower bound of at least -{m['margin']:.2f}, with at most {m['max_failed']} failed rows. "
                f"F1 is for \"{sh['positive']}\" against the rest, with the same bootstrap (resamples where it is undefined "
                f"are dropped); – where the rater never said \"{sh['positive']}\"."]
    out += ["", "## Tokens and cost", "",
            "| Rater | Model | Rows with tokens | Input tokens | Output tokens | Median latency per row | List-price cost | "
            f"Per company assessed | Per true \"{sh['positive']}\" |", "|---|---|---|---|---|---|---|---|---|"]
    for s in rep["raters"]:
        u, c, lat = s.get("usage"), s.get("cost"), s.get("median_latency_ms")
        out.append(f"| {s['rater']} | {(u or {}).get('model') or ('local' if s['local'] else '–')} | "
                   + (f"{u['rows']}/{s['n']} | {u['input_tokens']:,} | {u['output_tokens']:,}" if u else "not recorded | – | –")
                   + f" | {'–' if lat is None else f'{lat / 1000:.1f} s'} | "
                   + (f"{_usd(c['usd'])} | {_usd(c['per_company'])} | {_usd(c['per_true_yes'])} |" if c else "– | – | – |"))
    pr = rep.get("prices")
    if pr:
        out += ["", f"Cost: list-price equivalents from `{pr['file']}` (generatedAt {pr['generatedAt']}, sha256 "
                    f"{pr['sha256'][:16]}), USD per million tokens, input and output priced separately: what these calls would "
                    "cost at the published prices on that date, not what was paid. Per company assessed is the cost over "
                    f"answered rows; per true \"{sh['positive']}\" is over rows where the rater and the person both said "
                    f"\"{sh['positive']}\". Local models: 0 at list price; electricity isn't counted."]
        notes = [f"{s['rater']}: {s['cost']['note']}" for s in rep["raters"] if not s["local"] and s["cost"].get("note")]
        if notes:
            out += ["", "No cost for " + "; ".join(notes) + "."]
    else:
        out += ["", "Cost: not computed; pass `--prices` with a provider-facts file (`ninshubur export euboulia-facts`)."]
    if not sh["totals_recorded"]:
        out += ["", "Note: this sheet's meta file has no per-row line totals (drawn before 23 Nov 2026), so the prompt rebuilt from it "
                    "says \"N of N lines\" where the original prompt may have said more lines existed."]
    for s in rep["raters"]:
        cols = list(next(iter(s["confusion"].values())))
        out += ["", f"## {s['rater']} ({_prompt(s['prompt_hash'], sh['prompt_hash'])}), {s['source']}", "",
                "| person \\ rater | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        out += [f"| {t} | " + " | ".join(str(c[x]) for x in cols) + " |" for t, c in s["confusion"].items()]
    names = [k for k in rep["rows"][0] if k not in ("row", "subject", "truth")] if rep["rows"] else []
    split = [r for r in rep["rows"] if any(r[k] != r["truth"] for k in names)]
    if split:
        out += ["", f"## Rows where a rater differs from the person ({len(split)} of {len(rep['rows'])})", "",
                "| Row | Subject | Person | " + " | ".join(names) + " |", "|---|---|---|" + "---|" * len(names)]
        out += [f"| {r['row']} | {r['subject'].replace('|', '/')} | {r['truth']} | " + " | ".join(
                r[k] if r[k] != r["truth"] else "·" for k in names) + " |" for r in split]
    return "\n".join(out) + "\n"


def report_paths(home: Path, rep: dict) -> tuple[Path, Path]:
    sh = rep["sheet"]
    stem = f"eval-{sh['profile']}-seed{sh['seed']}-{rep['created_at'][:10]}"
    return home / "feeds" / f"{stem}.md", home / "feeds" / f"{stem}.json"


def to_json(rep: dict) -> str:
    return json.dumps(rep, indent=2, ensure_ascii=False, default=list) + "\n"
