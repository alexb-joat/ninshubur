#!/usr/bin/env python3
"""Rebuild every number in docs/case-study.md from the evaluation reports.

    python scripts/case_study.py REPORT.json...                              print the generated fragments
    python scripts/case_study.py --check docs/case-study.md REPORT.json...   exit 0 only when the doc matches
    python scripts/case_study.py --update docs/case-study.md REPORT.json...  rewrite the fragments in place

A fragment is the text between `<!-- gen:NAME -->` and `<!-- /gen -->` in the doc. `--check` fails when a fragment
differs from what the reports give, when one is missing or unknown, and when the prose outside the fragments holds a
digit that isn't on a short allowlist (P0-P5, F1, 95%, the model version 5.5, seed 7 and seed 8), so no number in the
doc can be typed by hand. The attack fragment also runs `tests/test_explorer_attacks.py` and uses its result line.

The reports are `ninshubur evaluate --prices ... -o ...` output, one per label sheet (seed). Their per-row table names
the companies, so this script reads only its label columns and prints no company name. It writes nothing unless
`--update` is given.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess  # noqa: S404 - runs the attack tests for their result line; scripts/ is not part of the package
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # this checkout's ninshubur, not whichever one is installed

from ninshubur import stats as S  # noqa: E402

# -- what the doc reports: fixed before this script was written ----------------------------------------------------
# Prompts by name, hash prefix and one generic line. No prompt text and no criteria: the repo is public.
PROMPTS = [
    ("P0", "4a298b36", "the profile's question as the sheets were drawn with it, written before any labelling; pre-registered"),
    ("P1", "6522c05c", "P0 plus rules written after reading the seed-7 labels and disagreements"),
    ("P2", "7c6e8f61", "P1 with one rule removed and two criteria widened, after reading the seed-7 results"),
    ("P3", "60da85db", "P2 plus one exclusion rule fitted on 2 seed-7 rows"),
    ("P4", "6c1595be", "P3 plus one widened criterion; frozen before the seed-8 labels, not run on seed 7"),
    ("P5", "55d0d53c", "P4 without P3's exclusion rule, written after seeing the seed-8 results"),
]
# Confirmatory raters per seed (the pre-registration); everything else in a report is exploratory.
CONFIRMATORY = {7: ["haiku-P0", "sonnet-P0", "opus-P0"], 8: ["opus-P0", "opus-P4"]}
# Pre-registered comparisons: (seed, rater x, rater y or None for "always no"). Gap = agreement(x) - agreement(y).
TESTS = [(7, "haiku-P0", None), (7, "sonnet-P0", None), (7, "opus-P0", None),
         (8, "opus-P0", None), (8, "opus-P4", None), (8, "opus-P4", "opus-P0")]
RULE = ('The pre-registration names these comparisons but fixes no rule for calling one rater better. This doc reads '
        '"better" as: the paired 95% interval of the agreement gap lies wholly above 0, and "worse" as wholly below 0; '
        'an interval that includes 0 is "not shown better". That reading was set on 24 Nov 2026, after the results '
        'were known.')
MODEL_NAMES = {"claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-opus-5-5": "Opus 5.5"}
ATTACKS = ROOT / "tests" / "test_explorer_attacks.py"
GEN = re.compile(r"<!-- gen:([a-z0-9-]+) -->(.*?)<!-- /gen -->", re.S)
ALLOWED = re.compile(r"\bP[0-5]\b|\bF1\b|95%|\b5\.5\b|\b[Ss]eed[ -][78]\b")


class Problem(Exception):
    pass


# -- reading the reports --------------------------------------------------------------------------------------------
def load(paths: list[str]) -> dict[int, dict]:
    reports = {}
    for p in paths:
        try:
            rep = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise Problem(f"cannot read report {p}: {e}") from None
        if "prices" not in rep or not all("positive_f1" in r for r in rep.get("raters", [])):
            raise Problem(f"{p} has no F1 or prices: regenerate it with `ninshubur evaluate ... --prices FILE -o ...`")
        seed = rep["sheet"]["seed"]
        if seed in reports:
            raise Problem(f"two reports for seed {seed}: {p} and an earlier one; give one report per label sheet")
        reports[seed] = rep
    missing = sorted(set(CONFIRMATORY) - set(reports))
    if missing:
        raise Problem(f"no report for seed {', '.join(map(str, missing))}; the doc covers seeds {sorted(CONFIRMATORY)}")
    for seed, rep in reports.items():
        if not rep["sheet"]["prompt_hash"].startswith(PROMPTS[0][1]):
            raise Problem(f"seed {seed}: the sheet's prompt hash {rep['sheet']['prompt_hash'][:8]} is not P0's {PROMPTS[0][1]}")
        for r in rep["raters"]:
            p = prompt_name(r)
            m = re.search(r"-(P[0-9])$", r["rater"])
            if m and m.group(1) != p:
                raise Problem(f"seed {seed}: rater {r['rater']} has prompt hash {r['prompt_hash'][:8]}, which is {p}")
    return reports


def prompt_name(r: dict) -> str | None:
    h = r.get("prompt_hash")
    if not h:
        return None
    for name, prefix, _ in PROMPTS:
        if h.startswith(prefix):
            return name
    raise Problem(f"rater {r['rater']}: prompt hash {h[:8]} is not one of P0-P5; add it to PROMPTS with a generic line")


def rater(rep: dict, name: str) -> dict:
    found = [r for r in rep["raters"] if r["rater"] == name]
    if len(found) != 1:
        raise Problem(f"seed {rep['sheet']['seed']}: {len(found)} raters named {name!r}, expected 1")
    return found[0]


def labels(rep: dict, r: dict | None) -> list[str]:
    """One rater's labels from the per-row table, or "always no" for None. Never reads the subject column."""
    if r is None:
        return [rep["baseline"]["label"]] * len(rep["rows"])
    col = f"{r['rater']} [{r['prompt_hash']}]" if r.get("prompt_hash") else r["rater"]
    return [row[col] for row in rep["rows"]]


def truth(rep: dict) -> list[str]:
    return [row["truth"] for row in rep["rows"]]


# -- formatting -----------------------------------------------------------------------------------------------------
def num(x: float, d: int = 2) -> str:
    return f"{x:.{d}f}"


def ci(lo_hi, d: int = 2) -> str:
    return f"{num(lo_hi[0], d)} to {num(lo_hi[1], d)}"


def pct(k: int, n: int) -> str:
    return f"{round(100 * k / n)}% ({k}/{n})" if n else "undefined (0/0)"


def usd(x: float | None) -> str:
    return "–" if x is None else f"${x:.4f}"


def model(r: dict) -> str:
    if r.get("local"):
        return f"{r['rater']} (local)"
    u = r.get("usage") or {}
    return MODEL_NAMES.get(u.get("model"), "Opus 5.5, 16 Nov draft" if "draft" in r["rater"] else "?")


def when(ts: str) -> str:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %b %H:%M:%S UTC").lstrip("0")


def secs(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").timestamp()


def gap(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h else f"{m} min {s} s"


def table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


# -- fragments ------------------------------------------------------------------------------------------------------
def score_row(rep: dict, r: dict, note: str | None = None) -> list[str]:
    tp, fp, fn = r["positive_tp"], r["positive_fp"], r["positive_fn"]
    f1 = "undefined" if r["positive_f1"] is None else f"{num(r['positive_f1'])} ({ci(r['positive_f1_ci'])})"
    c = r.get("cost") or {}
    row = [r["rater"], model(r), prompt_name(r) or "own", f"{r['answered']}/{r['n']}",
           f"{r['agree']}/{r['n']} = {num(r['agreement'])} ({ci(r['agreement_ci'])})",
           f"{num(r['kappa'])} ({ci(r['kappa_ci'])})", f1, pct(tp, tp + fp), pct(tp, tp + fn),
           usd(c.get("per_company")), usd(c.get("per_true_yes"))]
    return row + ([note] if note is not None else [])


def baseline_row(rep: dict, note: str | None = None) -> list[str]:
    b, t = rep["baseline"], truth(rep)
    yes = sum(1 for x in t if x == rep["sheet"]["positive"])
    k = S.cohen_kappa(t, labels(rep, None))
    row = [f'always "{b["label"]}"', "none", "–", f"{len(t)}/{len(t)}",
           f"{b['agree']}/{len(t)} = {num(b['agreement'])} ({ci(b['agreement_ci'])})",
           f"{num(k)} (by construction)", "undefined (never says yes)", "undefined (never says yes)",
           pct(0, yes), "–", "–"]
    return row + ([note] if note is not None else [])


HEAD = ["Rater", "Model", "Prompt", "Answered", "Agreement (95%)", "Kappa (95%)", "F1 for yes (95%)",
        "Precision for yes", "Recall for yes", "Cost per company assessed", "Cost per true yes"]


def f_summary(reps: dict[int, dict]) -> str:
    s7, s8 = reps[7], reps[8]
    o7, o8 = rater(s7, "opus-P0"), rater(s8, "opus-P0")
    best = max((rater(s7, n) for n in CONFIRMATORY[7]), key=lambda r: (r["agreement"], r["kappa"]))
    if best is not o7:
        raise Problem(f"the summary names Opus with P0 as the best seed-7 Claude rater, but {best['rater']} is; rewrite it")
    t7 = test(s7, o7, None)
    t8 = test(s8, o8, None)
    t48 = test(s8, rater(s8, "opus-P4"), o8)
    return (f"With the pre-registered prompt P0, the best Claude model (Opus 5.5) agreed with the labeller on "
            f"{o7['agree']} of {o7['n']} companies on seed 7, against {s7['baseline']['agree']} for a rater that always "
            f"says \"no\" (gap {pts(t7[0])} points, 95% interval {pts(t7[1])} to {pts(t7[2])}): {t7[3]}. On seed 8 it "
            f"agreed on {o8['agree']} of {o8['n']} against {s8['baseline']['agree']} ({pts(t8[0])} points, "
            f"{pts(t8[1])} to {pts(t8[2])}): {t8[3]} at this sample size; the pre-registered rival prompt P4 was {t48[3]} "
            f"than P0 ({pts(t48[0])} points, {pts(t48[1])} to {pts(t48[2])}). For the job the qualifier does, finding the "
            f"\"yes\" companies, Opus with P0 found {o7['positive_tp']} of {o7['positive_tp'] + o7['positive_fn']} on seed 7 "
            f"and {o8['positive_tp']} of {o8['positive_tp'] + o8['positive_fn']} on seed 8, at "
            f"{usd(o7['cost']['per_company'])} and {usd(o8['cost']['per_company'])} per company assessed at list price.")


def pts(x: float) -> str:
    return f"{100 * x:+.1f}".replace("+0.0", "0.0").replace("-0.0", "0.0")


def test(rep: dict, x: dict, y: dict | None) -> tuple[float, float, float, str]:
    m = rep["method"]
    point, lo, hi = S.paired_bootstrap_diff(truth(rep), labels(rep, x), labels(rep, y), S.agreement,
                                            reps=m["bootstrap_reps"], seed=m["seed"])
    verdict = "better" if lo > 0 else "worse" if hi < 0 else "not shown better"
    return point, lo, hi, verdict


def f_data(reps: dict[int, dict]) -> str:
    rows = []
    s7 = reps[7]["sheet"]
    for seed, rep in sorted(reps.items()):
        sh = rep["sheet"]
        c = sh["counts"]
        if seed == 7:
            pool = f"{sh['population']} companies collected by {when(sh['drawn_at'])[:6]}"
        elif sh["population"] == s7["population"] - s7["n"]:
            pool = f"the {sh['population']} of those {s7['population']} not drawn for seed 7"
        else:
            pool = f"{sh['population']} companies"
        rows.append([f"seed {seed}", when(sh["drawn_at"])[:6], pool, str(sh["n"]), str(c.get("yes", 0)),
                     str(c.get("no", 0)), str(c.get("unsure", 0))])
    return table(["Sheet", "Drawn", "Drawn from", "n", "yes", "no", "unsure"], rows)


def f_freeze(reps: dict[int, dict]) -> str:
    rows = []
    for seed, rep in sorted(reps.items()):
        sh = rep["sheet"]
        frozen = secs(sh["modified_at"])
        outs = []  # (time, rater, how the time was recorded)
        for r in rep["raters"]:
            if r.get("answered_at"):
                outs.append((r["answered_at"][0], r["rater"], "first stored answer"))
            elif r.get("written_at"):
                outs.append((r["written_at"], r["rater"], "rater file last written"))
        before = sorted(o for o in outs if secs(o[0]) < frozen)
        after = sorted(o for o in outs if secs(o[0]) >= frozen)
        first = after[0] if after else None
        rows.append([f"seed {seed}", f"`{sh['sha256'][:16]}`", when(sh["modified_at"]),
                     f"{first[1]}, {first[2]} {when(first[0])}, {gap(secs(first[0]) - frozen)} after" if first else "–",
                     "; ".join(f"{o[1]} ({o[2]} {when(o[0])[:6]})" for o in before) or "none"])
    return table(["Sheet", "Labels file sha256 (first 16)", "Labels frozen (file time)", "Earliest model output after",
                  "Model output older than the labels"], rows)


def f_method(reps: dict[int, dict]) -> str:
    ms = {json.dumps(r["method"], sort_keys=True) for r in reps.values()}
    if len(ms) != 1:
        raise Problem("the reports were built with different bootstrap settings; regenerate them with the same --reps/--seed")
    m = next(iter(reps.values()))["method"]
    return (f"Agreement intervals are Wilson {m['interval']} intervals. Kappa, F1 and the paired gaps use a percentile "
            f"bootstrap over rows: {m['bootstrap_reps']:,} resamples, seed {m['seed']}, the same draws for every rater, so "
            f"two raters are always compared on the same resampled rows. Resamples where F1 is undefined (no \"yes\" in "
            f"the truth or the answers) are dropped. A local model takes over the bulk qualify role only if its paired "
            f"gap to the best Claude rater has a lower bound of at least -{m['margin']} and it fails at most "
            f"{m['max_failed']} answers.")


def f_prompts(reps: dict[int, dict]) -> str:
    used = {}
    for seed, rep in sorted(reps.items()):
        for r in rep["raters"]:
            p = prompt_name(r)
            if p:
                role = "confirmatory" if r["rater"] in CONFIRMATORY[seed] else "exploratory"
                used.setdefault(p, {}).setdefault(seed, set()).add(role)
    rows = []
    for name, prefix, line in PROMPTS:
        runs = "; ".join(f"seed {s}: {', '.join(sorted(roles))}" for s, roles in sorted(used.get(name, {}).items()))
        rows.append([name, f"`{prefix}`", line, runs or "not run"])
    return table(["Prompt", "Hash (first 8)", "What it is", "Runs"], rows)


def f_results(rep: dict) -> str:
    seed = rep["sheet"]["seed"]
    rows = [score_row(rep, rater(rep, n)) for n in CONFIRMATORY[seed]] + [baseline_row(rep)]
    return table(HEAD, rows)


def f_tests(reps: dict[int, dict]) -> str:
    rows = []
    for seed, x, y in TESTS:
        rep = reps[seed]
        rx, ry = rater(rep, x), (rater(rep, y) if y else None)
        point, lo, hi, verdict = test(rep, rx, ry)
        ay = ry["agree"] if ry else rep["baseline"]["agree"]
        rows.append([f"seed {seed}", f"{x} vs {y or 'always no'}", f"{rx['agree']} vs {ay} of {rx['n']}",
                     f"{pts(point)}", f"{pts(lo)} to {pts(hi)}", verdict])
    return RULE + "\n\n" + table(["Sheet", "Comparison", "Agreed", "Gap (points)", "95% interval", "Verdict"], rows)


def exploratory_note(seed: int, r: dict) -> str:
    if r.get("local"):
        v = r.get("verdict") or {}
        d, lohi = v.get("difference"), v.get("ci")
        rule = (f"adoption rule: {v['result']} (gap to {v['against']} {pts(d)} points, {pts(lohi[0])} to {pts(lohi[1])})"
                if d is not None else f"adoption rule: {v.get('result')}")
        return f"local model, run {r['source'].removeprefix('run ')}; {rule}"
    if not r.get("prompt_hash"):
        return "written 16 Nov, before labelling; no token counts; can't be run again"
    p = prompt_name(r)
    return {"P0": "P0 on a model not pre-registered here", "P5": "prompt written after seeing these results"}.get(
        p, "prompt written after seeing seed-7 results (in-sample)" if seed == 7 else "exploratory")


def f_exploratory(rep: dict) -> str:
    seed = rep["sheet"]["seed"]
    rows = [score_row(rep, r, exploratory_note(seed, r)) for r in rep["raters"] if r["rater"] not in CONFIRMATORY[seed]]
    return table(HEAD + ["Why exploratory"], rows)


def f_cost(reps: dict[int, dict]) -> str:
    pr = next(iter(reps.values()))["prices"]
    if any(r["prices"]["sha256"] != pr["sha256"] for r in reps.values()):
        raise Problem("the reports were priced from different provider-facts files; regenerate them with the same --prices")
    rows, prices = [], {}
    for seed, rep in sorted(reps.items()):
        for r in rep["raters"]:
            u, c = r.get("usage"), r.get("cost") or {}
            if not u:
                rows.append([f"seed {seed}", r["rater"], model(r), "0", "–", "–", "–", c.get("note", "–")])
                continue
            if c.get("price"):
                prices[MODEL_NAMES.get(u["model"], u["model"])] = c["price"]
            lat = r.get("median_latency_ms")
            rows.append([f"seed {seed}", r["rater"], model(r), f"{u['rows']}/{r['n']}", f"{u['input_tokens']:,}",
                         f"{u['output_tokens']:,}", "–" if lat is None else f"{lat / 1000:.1f} s",
                         usd(c.get("usd")) + (f" ({c['long_context_rows']} rows at the long-context rate)"
                                              if c.get("long_context_rows") else "")])
    plist = "; ".join(f"{m} ${p['input']:g} in / ${p['output']:g} out" for m, p in sorted(prices.items()))
    head = (f"Prices: `{pr['file']}` (sha256 `{pr['sha256'][:16]}`), dated {pr['generatedAt']}, taken from "
            f"{', '.join(pr['sources'])}; per million tokens: {plist}. Input and output are priced separately. "
            f"In the reports' words: {pr['note']}.")
    return head + "\n\n" + table(["Sheet", "Rater", "Model", "Rows with tokens", "Input tokens", "Output tokens",
                                  "Median latency per row", "List-price cost"], rows)


def f_prices_date(reps: dict[int, dict]) -> str:
    return datetime.strptime(next(iter(reps.values()))["prices"]["generatedAt"], "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")


def f_attacks(_reps: dict[int, dict]) -> str:
    src = ATTACKS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    sections, current = [], None
    marks = [(i + 1, m.group(1).strip()) for i, ln in enumerate(src.splitlines()) if (m := re.match(r"# -- (.+?) -+$", ln))]
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    py = [sys.executable, "-m", "pytest", str(ATTACKS.relative_to(ROOT)), "-q", "-p", "no:cacheprovider"]
    collected = subprocess.run(py + ["--collect-only"], cwd=ROOT, capture_output=True, text=True).stdout  # noqa: S603
    cases: dict[str, int] = {}
    for line in collected.splitlines():
        if "::" in line:
            name = line.split("::", 1)[1].split("[", 1)[0]
            cases[name] = cases.get(name, 0) + 1
    rows = []
    for f in funcs:
        section = [t for ln, t in marks if ln < f.lineno]
        current = section[-1] if section else "(no heading)"
        if current not in sections:
            sections.append(current)
        body = ast.get_source_segment(src, f) or ""
        logs = bool(re.search(r"\blog\(", body))
        rows.append([current, f.name.removeprefix("test_").replace("_", " "), str(cases.get(f.name, 0)),
                     "yes" if logs else "no"])
    run = subprocess.run(py, cwd=ROOT, capture_output=True, text=True)  # noqa: S603
    last = (run.stdout.strip().splitlines() or ["no output"])[-1]
    result = re.sub(r" in [0-9.]+s.*$", "", last.strip("= "))
    n_log = sum(1 for r in rows if r[3] == "yes")
    return (f"{len(funcs)} test functions in {len(sections)} sections, {sum(cases.values())} cases collected; "
            f"{n_log} of the {len(funcs)} functions also assert what the explorer's log recorded. Result of "
            f"`python -m pytest tests/test_explorer_attacks.py -q` in this build: **{result}**"
            f"{'' if run.returncode == 0 else f' (exit {run.returncode})'}.\n\n"
            + table(["Section", "Attack (test name)", "Cases", "Checks the log"], rows))


FRAGMENTS = {
    "summary": f_summary, "data": f_data, "freeze": f_freeze, "method": f_method, "prompts": f_prompts,
    "results-7": lambda r: f_results(r[7]), "results-8": lambda r: f_results(r[8]), "tests": f_tests,
    "exploratory-7": lambda r: f_exploratory(r[7]), "exploratory-8": lambda r: f_exploratory(r[8]),
    "cost": f_cost, "prices-date": f_prices_date, "attacks": f_attacks,
}
INLINE = {"prices-date"}


def build(reps: dict[int, dict]) -> dict[str, str]:
    return {name: fn(reps) for name, fn in FRAGMENTS.items()}


def wrap(name: str, body: str) -> str:
    return f"<!-- gen:{name} -->{body}<!-- /gen -->" if name in INLINE else f"<!-- gen:{name} -->\n{body}\n<!-- /gen -->"


# -- check and update -----------------------------------------------------------------------------------------------
def stray_numbers(doc: str) -> list[str]:
    prose = GEN.sub(lambda m: "\n" * m.group(0).count("\n"), doc)
    prose = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), prose, flags=re.S)
    out = []
    for i, line in enumerate(prose.splitlines(), 1):
        rest = ALLOWED.sub("", line)
        if re.search(r"\d", rest):
            out.append(f"line {i}: {line.strip()[:100]}")
    return out


def check(doc_path: Path, frags: dict[str, str]) -> int:
    doc = doc_path.read_text(encoding="utf-8")
    found: dict[str, list[str]] = {}
    for m in GEN.finditer(doc):
        found.setdefault(m.group(1), []).append(m.group(2).strip("\n"))
    problems = []
    for name, body in frags.items():
        got = found.get(name, [])
        if not got:
            problems.append(f"fragment {name!r} is missing from the doc")
        for g in got:
            if g != body:
                ga, wa = g.splitlines(), body.splitlines()
                k = next((i for i, (a, b) in enumerate(zip(ga, wa, strict=False)) if a != b), min(len(ga), len(wa)))
                problems.append(f"fragment {name!r} differs at its line {k + 1}:\n  doc:     {(ga[k:k+1] or [''])[0][:160]}"
                                f"\n  reports: {(wa[k:k+1] or [''])[0][:160]}")
    problems += [f"unknown fragment {n!r} (not built by this script)" for n in found if n not in frags]
    problems += [f"number in prose outside a fragment, {s}" for s in stray_numbers(doc)]
    if problems:
        print(f"case study check: {len(problems)} problem(s) in {doc_path}; fix the prose or run --update", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    uses = sum(len(v) for v in found.values())
    numbers = sum(len(re.findall(r"\d+(?:[.,]\d+)*", b)) for b in frags.values())
    print(f"case study check: every number in {doc_path.name} matches the regenerated reports "
          f"({len(frags)} fragments, {uses} placements, {numbers} numbers); 0 numbers in prose outside fragments")
    return 0


def update(doc_path: Path, frags: dict[str, str]) -> int:
    doc = doc_path.read_text(encoding="utf-8")
    seen = set()

    def sub(m: re.Match) -> str:
        seen.add(m.group(1))
        return wrap(m.group(1), frags[m.group(1)]) if m.group(1) in frags else m.group(0)

    new = GEN.sub(sub, doc)
    missing = [n for n in frags if n not in seen]
    if new != doc:
        doc_path.write_text(new, encoding="utf-8")
    print(f"updated {doc_path} ({len(seen)} fragment names placed)" + (f"; not placed yet: {', '.join(missing)}"
                                                                         if missing else ""))
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild every number in docs/case-study.md from evaluation reports.")
    ap.add_argument("reports", nargs="+", metavar="REPORT.json", help="`ninshubur evaluate --prices` reports, one per seed")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", metavar="DOC", help="compare DOC's fragments with the reports; exit 0 only on a full match")
    g.add_argument("--update", metavar="DOC", help="rewrite DOC's fragments from the reports")
    a = ap.parse_args(argv)
    try:
        frags = build(load(a.reports))
    except Problem as e:
        print(f"case_study: {e}", file=sys.stderr)
        return 2
    if a.check:
        return check(Path(a.check), frags)
    if a.update:
        return update(Path(a.update), frags)
    for name, body in frags.items():
        print(wrap(name, body) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
