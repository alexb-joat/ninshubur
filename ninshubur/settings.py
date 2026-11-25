"""Settings engine (command line only): which settings a person may change by command, and how.

Two classes of key. A **learnable** key changes how suggestions are valued and ranked and nothing else: what a kind of
suggestion is worth, what your hour is worth, what a trip or a task costs. Each one is listed in ``LEARNABLE`` with its
type, bounds, default (read from ``config.DEFAULTS``, never copied) and a one-line meaning. Every other key of the
effective configuration is a **guardrail** key: it changes what is fetched, stored, sent or admitted, so it is shown
but never set here. A key is learnable only if it is listed; nothing is learnable by pattern.

``settings list`` shows the effective value of each key and where it came from (default, ``config.yaml``, or the
overlay ``settings.yaml``); ``settings preview KEY=VALUE`` shows old -> new and writes nothing; ``apply`` (with
``--yes``) and ``undo`` write the overlay and one audit line each in ``logs/settings.jsonl``; ``log`` prints them.
``config.yaml`` and its comments are never rewritten: the overlay is a second file that ``config.load_config`` merges
over it, learnable and valid entries only, when a process starts.
"""
from __future__ import annotations

import contextlib
import copy
import difflib
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULTS, config_path
from .logs import now_iso, read_jsonl

OVERLAY = "settings.yaml"
AUDIT = "logs/settings.jsonl"
MISSING = object()


@dataclass(frozen=True)
class Setting:
    path: tuple[str, ...]
    kind: str            # "usd": a number, int or float; "minutes": a whole number
    minimum: float
    maximum: float
    meaning: str

    @property
    def key(self) -> str:
        return ".".join(self.path)

    @property
    def default(self) -> Any:
        return lookup(DEFAULTS, self.path)

    @property
    def noun(self) -> str:
        return "a whole number" if self.kind == "minutes" else "a number"


def lookup(data: dict, path: tuple[str, ...], default: Any = MISSING) -> Any:
    cur: Any = data
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


USD_MAX = 100_000
_VALUE = "the starting value in USD of a suggestion of kind `{kind}`, before the weight, relevance, confidence, timing, tier and calibration factors"
_MINUTES = "the minutes of your time a suggestion of kind `{kind}` is costed at"

LEARNABLE: tuple[Setting, ...] = (
    Setting(("corroboration_value_usd",), "usd", 0, USD_MAX,
            "the value in USD above which a suggestion resting on a single Tier 2 or 3 source is held as needing corroboration (the policy's own limit still applies)"),
    Setting(("budgets", "daily_human_minutes"), "minutes", 0, 1440,
            "your daily budget of human minutes, shown by `ninshubur budget` against the minutes logged today"),
    Setting(("budgets", "hourly_rate_usd"), "usd", 0, 10_000,
            "what an hour of your time is worth in USD; it turns a suggestion's minutes into its human cost"),
    *(Setting(("base_value_usd", kind), "usd", 0, USD_MAX, _VALUE.format(kind=kind))
      for kind in ("service_request_lead", "hiring_signal_outreach", "outreach_window", "conference_prospecting", "cfp_submit",
                   "local_meeting_suggestion", "pain_content_idea", "tooling_adopt", "tool_deprecation", "tool_price_change",
                   "tool_outage")),
    Setting(("costs", "travel_same_city_usd"), "usd", 0, USD_MAX, "the cost in USD of an event in your own city"),
    Setting(("costs", "travel_same_country_usd"), "usd", 0, USD_MAX, "the cost in USD of an event elsewhere in your country"),
    Setting(("costs", "travel_abroad_usd"), "usd", 0, USD_MAX, "the cost in USD of an event abroad, or where the place is unknown"),
    *(Setting(("costs", "minutes", kind), "minutes", 0, 1440, _MINUTES.format(kind=kind))
      for kind in ("conference_prospecting", "cfp_submit", "hiring_signal_outreach", "service_request_lead", "tool_outage",
                   "outreach_window", "local_meeting_suggestion")),
)
REGISTRY: dict[str, Setting] = {s.key: s for s in LEARNABLE}


# --- the effective configuration, key by key ------------------------------------------------
def flatten(data: dict, prefix: tuple[str, ...] = ()) -> dict[str, Any]:
    """Every leaf of a configuration as dotted key -> value. The ``sources`` mapping is one leaf (it holds trust blocks)."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        path = (*prefix, str(k))
        if isinstance(v, dict) and v and path != ("sources",):
            out.update(flatten(v, path))
        else:
            out[".".join(path)] = v
    return out


def nest(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for key, v in flat.items():
        cur = out
        *head, last = key.split(".")
        for k in head:
            cur = cur.setdefault(k, {})
        cur[last] = v
    return out


def _read_yaml(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return MISSING   # present but unreadable: reported by the caller, never guessed at


def check_value(setting: Setting, value: Any) -> str | None:
    """Why ``value`` is not acceptable for ``setting``, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"needs {setting.noun}, got {value!r}"
    if isinstance(value, float) and not math.isfinite(value):
        return f"needs {setting.noun}, got {value!r}"
    if setting.kind == "minutes" and not isinstance(value, int):
        return f"needs a whole number of minutes, got {value!r}"
    if not setting.minimum <= value <= setting.maximum:
        return f"{value!r} is outside {setting.minimum:g} to {setting.maximum:g}"
    return None


@dataclass
class Overlay:
    values: dict[str, Any]       # the learnable, valid entries, dotted key -> value
    problems: list[str]          # one line per entry that cannot be used (key names only, never values of other keys)
    text: str | None             # the file as read, or None when there is none


def read_overlay(home: Path) -> Overlay:
    path = home / OVERLAY
    if not path.is_file():
        return Overlay({}, [], None)
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        return Overlay({}, [f"{OVERLAY} cannot be read as YAML"], None)
    if raw is None:
        return Overlay({}, [], text)
    if not isinstance(raw, dict):
        return Overlay({}, [f"{OVERLAY} is not a mapping"], text)
    values: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in flatten(raw).items():
        setting = REGISTRY.get(key)
        if setting is None:
            problems.append(f"{key}: not a learnable key (guardrail and unknown keys are never read from {OVERLAY})")
        elif (why := check_value(setting, value)) is not None:
            problems.append(f"{key}: {why}")
        else:
            values[key] = value
    return Overlay(values, problems, text)


@dataclass
class Layers:
    effective: dict[str, Any]            # dotted key -> value, defaults < config.yaml < overlay
    source: dict[str, str]               # dotted key -> "default" | "config" | "overlay"
    overlay: Overlay
    base: dict[str, Any]                 # the same without the overlay: what a key goes back to when its overlay entry goes
    base_source: dict[str, str]


def read_layers(home: Path) -> Layers:
    config_raw = _read_yaml(config_path(home))
    config_flat = flatten(config_raw) if isinstance(config_raw, dict) else {}
    effective = flatten(DEFAULTS)
    source = {k: "default" for k in effective}
    for k, v in config_flat.items():
        effective[k], source[k] = v, "config"
    base, base_source = dict(effective), dict(source)
    overlay = read_overlay(home)
    for k, v in overlay.values.items():
        effective[k], source[k] = v, "overlay"
    return Layers(effective, source, overlay, base, base_source)


def merge_overlay(data: dict, home: Path) -> tuple[dict, list[str]]:
    """``config.load_config``'s hook: ``data`` with the overlay's learnable, valid entries set, and the warnings for
    every entry that is not (a guardrail, an unknown key, a value out of range), which are ignored and never applied."""
    overlay = read_overlay(home)
    warnings = [f"{OVERLAY}: {p}; ignored" for p in overlay.problems]
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not overlay.values:
        return data, warnings
    data = copy.deepcopy(data)     # DEFAULTS' nested dicts are shared with the caller's; never write into them
    for key, value in overlay.values.items():
        cur = data
        *head, last = key.split(".")
        for k in head:
            cur = cur.setdefault(k, {})
        cur[last] = value
    return data, warnings


# --- reading a KEY=VALUE ----------------------------------------------------------------------
@dataclass
class Change:
    key: str
    old: Any
    old_source: str
    new: Any
    overlay_old: Any = MISSING     # what the overlay held before (MISSING: the key was not in it)

    @property
    def unchanged(self) -> bool:
        return self.old == self.new and type(self.old) is type(self.new)


def nearest(key: str, known: list[str]) -> str | None:
    close = difflib.get_close_matches(key, known, n=1, cutoff=0.6)
    return close[0] if close else None


def plan(layers: Layers, specs: list[str]) -> tuple[list[Change], list[str]]:
    """The changes ``specs`` ask for, and one refusal line per spec that cannot be honoured."""
    changes: list[Change] = []
    refusals: list[str] = []
    seen: set[str] = set()
    known = sorted(set(layers.effective) | set(REGISTRY))
    for spec in specs:
        key, eq, text = spec.partition("=")
        key = key.strip()
        if not eq or not key:
            refusals.append(f"{spec}: expected KEY=VALUE, for example base_value_usd.cfp_submit=900")
            continue
        if key in seen:
            refusals.append(f"{key}: given twice")
            continue
        seen.add(key)
        setting = REGISTRY.get(key)
        if setting is None:
            if key in layers.effective:
                refusals.append(f"{key}: a guardrail key (it changes what is fetched, stored, sent or admitted), so settings never "
                                "sets it; edit config.yaml by hand, after reading docs/settings.md")
                continue
            children = [k for k in known if k.startswith(key + ".")]
            if children:
                learn = [k for k in children if k in REGISTRY]
                refusals.append(f"{key}: a group, not a setting; " + (f"its settings: {', '.join(learn)}" if learn
                                                                      else "everything in it is a guardrail key"))
                continue
            near = nearest(key, known)
            refusals.append(f"{key}: not a key of the configuration" + (f"; did you mean {near}?" if near else ""))
            continue
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            value = text
        if (why := check_value(setting, value)) is not None:
            refusals.append(f"{key}: {why}")
            continue
        changes.append(Change(key, layers.effective[key], layers.source[key], value,
                              layers.overlay.values.get(key, MISSING)))
    return changes, refusals


# --- output ---------------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def _table(head: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in (head, *rows)]
    return "\n".join(lines)


def render_list(layers: Layers, *, everything: bool = False) -> str:
    rows = [[s.key, _fmt(layers.effective[s.key]), layers.source[s.key], "learnable", f"{s.minimum:g} to {s.maximum:g}", s.meaning]
            for s in sorted(LEARNABLE, key=lambda s: s.key)]
    if everything:
        for key in sorted(k for k in layers.effective if k not in REGISTRY):
            v = layers.effective[key]
            shown = f"{len(v)} configured" if key == "sources" else _fmt(v)
            rows.append([key, shown if len(shown) <= 60 else shown[:57] + "...", layers.source[key], "guardrail", "read-only", ""])
    out = _table(["key", "value", "from", "class", "bounds", "meaning"], rows)
    if layers.overlay.problems:
        out += "\n" + "\n".join(f"warning: {OVERLAY}: {p}; ignored" for p in layers.overlay.problems)
    return out


def render_changes(changes: list[Change]) -> str:
    rows = [[c.key, f"{_fmt(c.old)} ({c.old_source})", "->", _fmt(c.new) + ("  (unchanged)" if c.unchanged else ""),
             f"{REGISTRY[c.key].minimum:g} to {REGISTRY[c.key].maximum:g}"] for c in changes]
    return _table(["key", "now", "", "would be", "bounds"], rows)


def render_key_table() -> str:
    """The learnable keys as a Markdown table (docs/settings.md carries exactly this between its markers)."""
    lines = ["| key | type | bounds | default | meaning |", "|---|---|---|---:|---|"]
    for s in sorted(LEARNABLE, key=lambda s: s.key):
        lines.append(f"| `{s.key}` | {s.noun.replace('a ', '')} | {s.minimum:g} to {s.maximum:g} | {_fmt(s.default)} | {s.meaning} |")
    return "\n".join(lines)


# --- apply, undo, log ---------------------------------------------------------------------------
MAX_NOTE = 200
OVERLAY_HEADER = ("# Written by `ninshubur settings apply` and `undo`; see docs/settings.md. Only learnable keys are read from this\n"
                  "# file. It is rewritten whole each time, so keep your own comments in config.yaml.\n")


def overlay_text(values: dict[str, Any]) -> str:
    return OVERLAY_HEADER + yaml.safe_dump(nest(dict(sorted(values.items()))), sort_keys=True, default_flow_style=False)


def read_audit(home: Path) -> list[dict]:
    return [r for r in read_jsonl(home / AUDIT) if isinstance(r, dict)]


def history(records: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """The apply and undo lines that took effect, in order, and which apply an undo undid (apply id -> undo id).
    A line a later ``failed`` line withdraws (the overlay could not be replaced after the line was written) is left out."""
    failed = {r.get("id") for r in records if r.get("event") == "failed"}
    acts = [r for r in records if r.get("event") in ("apply", "undo") and type(r.get("id")) is int and r["id"] not in failed]
    return acts, {r["undoes"]: r["id"] for r in acts if r["event"] == "undo" and type(r.get("undoes")) is int}


def _entry(c: Change) -> dict:
    return {"key": c.key, "old": c.old, "old_source": c.old_source, "new": c.new,
            "overlay_before": None if c.overlay_old is MISSING else c.overlay_old}


def _write_tmp(path: Path, text: str) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        f.write(text)
    return tmp


def _append_audit(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")


def commit(home: Path, values: dict[str, Any], record: dict) -> None:
    """Make ``values`` the overlay (no entry left: no file) and log ``record``. The new file is written whole first, the audit
    line goes down next, and only then does the rename replace the old file, so a change never happens without its line.
    The audit line, unlike the run logs, is not best effort: if it cannot be written nothing changes. If the rename fails
    after the line is written, a ``failed`` line withdraws it. Raises OSError."""
    path = home / OVERLAY
    tmp = _write_tmp(path, overlay_text(values)) if values else None
    try:
        _append_audit(home / AUDIT, record)
    except OSError:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise
    try:
        if tmp is not None:
            os.replace(tmp, path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            _append_audit(home / AUDIT, {"id": record["id"], "ts": record["ts"], "event": "failed", "of": record["event"]})
        raise


def _next_id(records: list[dict]) -> int:
    return 1 + max((r["id"] for r in records if type(r.get("id")) is int), default=0)


def _overlay_refusals(layers: Layers) -> list[str]:
    if not layers.overlay.problems:
        return []
    return [f"{OVERLAY} holds entries that cannot be used, so it is not rewritten (that would drop them without your say): "
            f"{p}; remove them from the file, then run this again" for p in layers.overlay.problems]


def _note_refusal(note: str) -> str | None:
    if len(note) > MAX_NOTE:
        return f"--note is {len(note)} characters; the limit is {MAX_NOTE}"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in note):
        return "--note holds a control character or a line break; keep it to one plain line"
    return None


def cmd_apply(args, app) -> int:
    home = app.home
    layers = read_layers(home)
    changes, refusals = plan(layers, args.assignments)
    if (why := _note_refusal(args.note)) is not None:
        refusals.append(why)
    refusals += _overlay_refusals(layers)
    real = [c for c in changes if not c.unchanged]
    print("settings apply" if args.yes else "settings preview (nothing is written)")
    if changes:
        print(render_changes(changes))
    for r in refusals:
        print(f"refused: {r}", file=sys.stderr)
    if refusals:
        return 2
    if not real:
        print("nothing to change: every value is already the one asked for")
        return 0
    if not args.yes:
        print("not applied: nothing was written; add --yes to apply these", file=sys.stderr)
        return 2
    records = read_audit(home)
    rec = {"id": _next_id(records), "ts": now_iso(app.now()), "event": "apply", "note": args.note,
           "keys": [_entry(c) for c in real]}
    values = {**layers.overlay.values, **{c.key: c.new for c in real}}
    try:
        commit(home, values, rec)
    except OSError as e:
        print(f"error: could not write {OVERLAY} or {AUDIT} ({e.strerror or type(e).__name__}); nothing was changed", file=sys.stderr)
        return 1
    print(f"applied as #{rec['id']}; it takes effect at the next process start (the next ninshubur command, or when the daemon "
          f"restarts). Undo: ninshubur settings undo {rec['id']}")
    return 0


def _record_problem(rec: dict) -> str | None:
    """Why an audit line cannot be undone from (a hand-edited or damaged line), or None."""
    keys = rec.get("keys")
    if not isinstance(keys, list) or not keys:
        return "it lists no keys"
    for e in keys:
        setting = REGISTRY.get(e.get("key")) if isinstance(e, dict) else None
        if setting is None:
            return "it names a key that is not learnable"
        for field_name in ("new", "overlay_before"):
            v = e.get(field_name)
            if (field_name == "new" or v is not None) and check_value(setting, v) is not None:
                return f"{setting.key}: its {field_name} value is not valid"
    return None


def cmd_undo(args, app) -> int:
    home = app.home
    layers = read_layers(home)
    acts, undone = history(read_audit(home))
    applies = [r for r in acts if r["event"] == "apply"]
    if args.id is None:
        if not applies:
            print(f"refused: nothing to undo: no apply is logged in {AUDIT}", file=sys.stderr)
            return 2
        target = applies[-1]
    else:
        target = next((r for r in applies if r["id"] == args.id), None)
        if target is None:
            print(f"refused: no apply with id {args.id} in {AUDIT} (ninshubur settings log lists them)", file=sys.stderr)
            return 2
    tid = target["id"]
    if tid in undone:
        hint = "name an earlier one with `ninshubur settings undo ID`" if args.id is None else "it cannot be undone twice"
        print(f"refused: apply #{tid} was already undone by #{undone[tid]}; {hint}", file=sys.stderr)
        return 2
    if (why := _record_problem(target)) is not None:
        print(f"refused: apply #{tid} cannot be undone from {AUDIT}: {why}", file=sys.stderr)
        return 2
    refusals = _overlay_refusals(layers)
    for e in target["keys"]:
        now = layers.overlay.values.get(e["key"], MISSING)
        if now is MISSING or now != e["new"]:
            refusals.append(f"{e['key']} was changed again since apply #{tid} (it is {_fmt(layers.effective[e['key']])} now, not "
                            f"{_fmt(e['new'])}); undo the later change first (ninshubur settings log)")
    for r in refusals:
        print(f"refused: {r}", file=sys.stderr)
    if refusals:
        return 2
    values = dict(layers.overlay.values)
    entries = []
    for e in target["keys"]:
        key, before = e["key"], e.get("overlay_before")
        entries.append({"key": key, "old": layers.effective[key], "old_source": "overlay",
                        "new": layers.base[key] if before is None else before, "overlay_before": values[key]})
        if before is None:
            del values[key]
        else:
            values[key] = before
    rec = {"id": _next_id(read_audit(home)), "ts": now_iso(app.now()), "event": "undo", "undoes": tid, "keys": entries}
    try:
        commit(home, values, rec)
    except OSError as e:
        print(f"error: could not write {OVERLAY} or {AUDIT} ({e.strerror or type(e).__name__}); nothing was changed", file=sys.stderr)
        return 1
    print(f"undid apply #{tid} as #{rec['id']}")
    print(_table(["key", "was", "", "now"], [[x["key"], _fmt(x["old"]), "->", _fmt(x["new"])] for x in entries]))
    print("it takes effect at the next process start")
    return 0


def cmd_log(args, app) -> int:
    records = read_audit(app.home)
    acts, undone = history(records)
    withdrawn = {r.get("id") for r in records if r.get("event") == "failed"}
    shown = [r for r in records if r.get("event") in ("apply", "undo") and type(r.get("id")) is int]
    if not shown:
        print(f"no settings changes logged yet ({AUDIT})")
        return 0
    rows = []
    for r in shown[-args.last:]:
        what = r["event"] + (f" of #{r.get('undoes')}" if r["event"] == "undo" else "")
        if r["id"] in withdrawn:
            what += " (failed: not written)"
        elif r["id"] in undone:
            what += f" (undone by #{undone[r['id']]})"
        keys = r.get("keys") if isinstance(r.get("keys"), list) else []
        changes = "; ".join(f"{e.get('key')} {_fmt(e.get('old'))} -> {_fmt(e.get('new'))}" for e in keys if isinstance(e, dict))
        rows.append([f"#{r['id']}", str(r.get("ts", "")), what, changes, str(r.get("note") or "")])
    print(_table(["id", "time", "what", "changes", "note"], rows))
    return 0


# --- commands ---------------------------------------------------------------------------------
def cmd_list(args, app) -> int:
    print(render_list(read_layers(app.home), everything=args.all))
    return 0


def cmd_preview(args, app) -> int:
    changes, refusals = plan(read_layers(app.home), args.assignments)
    print("settings preview (nothing is written)")
    if changes:
        print(render_changes(changes))
    for r in refusals:
        print(f"refused: {r}", file=sys.stderr)
    return 2 if refusals else 0


def _count(text: str) -> int:
    n = int(text)
    if n < 1:
        raise ValueError(text)
    return n


def add_commands(sub) -> None:
    st = sub.add_parser("settings", help="the settings a person may change by command: list, preview, apply, undo, log")
    ss = st.add_subparsers(dest="settings_cmd", required=True, metavar="{list,preview,apply,undo,log}")
    ls = ss.add_parser("list", help="each setting's effective value, where it came from, its class and bounds")
    ls.add_argument("--all", action="store_true", help="also show the guardrail keys (read-only)")
    ls.set_defaults(fn=cmd_list, main_db=False)
    pv = ss.add_parser("preview", help="old -> new for KEY=VALUE...; checks them and writes nothing (exit 2 on any refusal)")
    pv.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    pv.set_defaults(fn=cmd_preview, main_db=False)
    ap = ss.add_parser("apply", help="write KEY=VALUE... to settings.yaml (needs --yes) and log it; takes effect at the next process start")
    ap.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    ap.add_argument("--note", default="", help=f"why, kept in the log (one line, up to {MAX_NOTE} characters)")
    ap.add_argument("--yes", action="store_true", help="write it; without this apply only previews and exits 2")
    ap.set_defaults(fn=cmd_apply, main_db=False)
    ud = ss.add_parser("undo", help="restore what the last apply (or apply ID) replaced; refused if a key changed again since")
    ud.add_argument("id", nargs="?", type=int, metavar="ID")
    ud.set_defaults(fn=cmd_undo, main_db=False)
    lg = ss.add_parser("log", help="the audit lines of apply and undo, oldest first")
    lg.add_argument("--last", type=_count, default=10, metavar="N", help="show the last N (default 10)")
    lg.set_defaults(fn=cmd_log, main_db=False)
