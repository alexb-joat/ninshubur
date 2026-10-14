"""Schema registry and projection: the only way payload data becomes stored data.

``project(data, schema, now)`` keeps **only** declared fields, validates and sanitises each
value, taint-scans text, and reports violations. Undeclared fields never leave this function.
"""
from __future__ import annotations

import html
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from . import detectors
from .policy import check_url

FIVE_YEARS = 5 * 365.25 * 86400
THREE_YEARS = 3 * 365.25 * 86400

# ---------------------------------------------------------------------------------------------
# Registry. One entry per adapter version. ``root`` is a dotted path to the list of records
# (omit for a top-level list; a dict at the root is treated as a single record). ``path`` on a
# field reads a nested value. Nothing outside ``fields`` is ever read into a row.
# ---------------------------------------------------------------------------------------------
SCHEMAS: dict[str, dict] = {
    "provider_model.v1": {
        "max_items": 200,
        "required": ["provider", "model_id", "page"],
        "fields": {
            "provider": {"type": "enum", "values": ["anthropic", "openai"]},
            "model_id": {"type": "str", "max": 64, "pattern": r"^[a-z0-9][a-z0-9.-]{1,60}$"},
            "page": {"type": "str", "max": 40, "pattern": r"^[a-z-]+$"},
            "name": {"type": "str", "max": 80, "text": True},
            "input_per_mtok": {"type": "float", "min": 0, "max": 10_000},
            "output_per_mtok": {"type": "float", "min": 0, "max": 10_000},
            "long_context_threshold": {"type": "int", "min": 1_000, "max": 100_000_000},
            "long_context_input": {"type": "float", "min": 0, "max": 10_000},
            "long_context_output": {"type": "float", "min": 0, "max": 10_000},
            "context": {"type": "int", "min": 1_000, "max": 100_000_000},
            "max_output": {"type": "int", "min": 100, "max": 100_000_000},
            "status": {"type": "enum", "values": ["active", "legacy", "deprecated", "retired"]},
            "retires_at": {"type": "str", "max": 60},
            "deprecated_at": {"type": "str", "max": 60},
            "efforts": {"type": "list_str", "max": 12, "max_items": 8},
            "default_effort": {"type": "str", "max": 12, "pattern": r"^[a-z]+$"},
        },
    },
    "confs_tech.v1": {
        "max_items": 500,
        "required": ["name", "url", "startDate"],
        "fields": {
            "name": {"type": "str", "max": 200, "text": True},
            "url": {"type": "url"},
            "startDate": {"type": "date"},
            "endDate": {"type": "date"},
            "city": {"type": "str", "max": 80},
            "country": {"type": "str", "max": 80},
            "cfpUrl": {"type": "url"},
            "cfpEndDate": {"type": "date"},
            "online": {"type": "bool"},
        },
    },
    "hn_story.v1": {
        "root": "hits",
        "max_items": 200,
        "required": ["objectID", "title", "created_at"],
        "fields": {
            "objectID": {"type": "str", "max": 20, "pattern": r"^\d+$"},
            "title": {"type": "str", "max": 300, "text": True},
            "url": {"type": "url"},
            "author": {"type": "str", "max": 40},
            "created_at": {"type": "datetime"},
            "points": {"type": "int", "min": 0, "max": 1_000_000},
            "num_comments": {"type": "int", "min": 0, "max": 1_000_000},
        },
    },
    "hn_comment.v1": {
        "root": "hits",
        "max_items": 200,
        "required": ["objectID", "created_at"],
        "fields": {
            "objectID": {"type": "str", "max": 20, "pattern": r"^\d+$"},
            "story_id": {"type": "int", "min": 1, "max": 10**10},
            "author": {"type": "str", "max": 40},
            "created_at": {"type": "datetime"},
            "comment_text": {"type": "str", "max": 4000, "text": True},
        },
    },
    "greenhouse_jobs.v1": {
        "root": "jobs",
        "max_items": 2000,
        "required": ["id", "title", "absolute_url"],
        "fields": {
            "id": {"type": "int", "min": 1, "max": 10**12},
            "title": {"type": "str", "max": 200, "text": True},
            "updated_at": {"type": "datetime"},
            "first_published": {"type": "datetime"},
            "location": {"type": "str", "max": 120, "path": "location.name"},
            "absolute_url": {"type": "url"},
        },
    },
    "statuspage_summary.v1": {
        "root": "incidents",
        "max_items": 100,
        "required": ["id", "name", "status"],
        "fields": {
            "id": {"type": "str", "max": 40, "pattern": r"^[A-Za-z0-9_-]+$"},
            "name": {"type": "str", "max": 200, "text": True},
            "status": {"type": "enum", "values": ["investigating", "identified", "monitoring", "resolved", "postmortem",
                                                  "scheduled", "in_progress", "verifying", "completed"]},
            "impact": {"type": "enum", "values": ["none", "minor", "major", "critical", "maintenance"]},
            "created_at": {"type": "datetime"},
            "updated_at": {"type": "datetime"},
            "resolved_at": {"type": "datetime"},
            "shortlink": {"type": "url"},
        },
    },
    "statuspage_components.v1": {
        "root": "components",
        "max_items": 200,
        "required": ["id", "name", "status"],
        "fields": {
            "id": {"type": "str", "max": 40, "pattern": r"^[A-Za-z0-9_-]+$"},
            "name": {"type": "str", "max": 120, "text": True},
            "status": {"type": "enum", "values": ["operational", "degraded_performance", "partial_outage",
                                                  "major_outage", "under_maintenance"]},
            "updated_at": {"type": "datetime"},
        },
    },
}


class SchemaError(Exception):
    pass


def get_schema(schema_id: str) -> dict:
    try:
        s = SCHEMAS[schema_id]
    except KeyError:
        raise SchemaError(f"unknown schema {schema_id!r}") from None
    validate_schema(schema_id, s)
    return s


_TYPES = {"str", "date", "datetime", "int", "float", "bool", "enum", "url", "list_str"}


def validate_schema(schema_id: str, s: dict) -> None:
    if not isinstance(s.get("fields"), dict) or not s["fields"]:
        raise SchemaError(f"{schema_id}: no fields")
    for name, spec in s["fields"].items():
        if spec.get("type") not in _TYPES:
            raise SchemaError(f"{schema_id}.{name}: bad type {spec.get('type')!r}")
        if spec["type"] == "enum" and not spec.get("values"):
            raise SchemaError(f"{schema_id}.{name}: enum without values")
    for r in s.get("required", []):
        if r not in s["fields"]:
            raise SchemaError(f"{schema_id}: required field {r!r} not declared")


def check_registry() -> list[str]:
    """Return the ids of every schema that loads; raise SchemaError on the first bad one."""
    for k, v in SCHEMAS.items():
        validate_schema(k, v)
    return sorted(SCHEMAS)


# --- sanitisers --------------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]{0,500}>")
_WS = re.compile(r"\s+")


def _strip_hidden(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if any(lo <= o <= hi for lo, hi in detectors.HIDDEN_RANGES):
            continue
        out.append(ch)
    return "".join(out)


def sanitize_str(value: Any, max_len: int = 1000) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    value = unicodedata.normalize("NFC", value)
    value = _TAG.sub(" ", value)
    value = html.unescape(value)
    value = _TAG.sub(" ", value)  # entities may have decoded into tags
    value = _strip_hidden(value)
    value = _WS.sub(" ", value).strip()
    if len(value) > max_len:
        value = value[:max_len].rstrip()
    return value


_MONTH_D_Y = re.compile(r"^(?P<m>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})$")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}


def parse_when(value: Any) -> float | None:
    """Strict: ISO 8601, RFC 2822, ``Month D, YYYY``. Returns a UTC epoch or None. Never guesses."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v > 1e12:  # milliseconds
            v /= 1000.0
        return v if 0 < v < 4e9 else None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or len(s) > 64:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=d.tzinfo or timezone.utc).timestamp()
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            return d.replace(tzinfo=d.tzinfo or timezone.utc).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    m = _MONTH_D_Y.match(s)
    if m:
        name = m.group("m").lower()
        month = _MONTHS.get(name) or _MONTHS.get(next((k for k in _MONTHS if k.startswith(name[:3])), ""), None)
        if month:
            try:
                return datetime(int(m.group("y")), month, int(m.group("d")), tzinfo=timezone.utc).timestamp()
            except ValueError:
                return None
    return None


def plausible(ts: float, now: float) -> bool:
    return (now - FIVE_YEARS) <= ts <= (now + THREE_YEARS)


# --- projection --------------------------------------------------------------------------------
@dataclass
class Projection:
    rows: list[dict] = field(default_factory=list)
    checked: int = 0
    violations: int = 0
    dropped_rows: int = 0
    findings: list[detectors.Finding] = field(default_factory=list)  # document-level (structure)

    @property
    def field_validity(self) -> float:
        return 1.0 if self.checked == 0 else 1.0 - self.violations / self.checked

    def drift(self, ratio: float) -> bool:
        return self.checked > 0 and (self.violations / self.checked) > ratio


def _walk(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _coerce(value: Any, spec: dict, now: float, *, allowlisted_hosts: tuple[str, ...],
            taint_findings: list[detectors.Finding], det: dict) -> tuple[Any, bool]:
    """Return (clean_value, ok). ``ok`` False means a violation (value is dropped)."""
    t = spec["type"]
    if value is None:
        return None, True
    if t == "str":
        if isinstance(value, (dict, list)):
            return None, False
        if spec.get("text") and isinstance(value, str):
            taint_findings.extend(detectors.scan_text(
                value, allowlisted_hosts=allowlisted_hosts,
                base64_run_min=int(det.get("base64_run_min", 200)),
                lookalike_max_distance=int(det.get("lookalike_max_distance", 2))))
        elif isinstance(value, str) and detectors.has_hidden_unicode(value):
            taint_findings.append(detectors.Finding("high", "hidden_unicode", spec.get("_name", "")))
        clean = sanitize_str(value, int(spec.get("max", 1000)))
        if clean is None:
            return None, False
        if spec.get("pattern") and not re.match(spec["pattern"], clean):
            return None, False
        return clean, True
    if t in ("date", "datetime"):
        ts = parse_when(value)
        if ts is None or not plausible(ts, now):
            return None, False
        return ts, True
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None, False
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return None, False
        if iv < spec.get("min", -2**53) or iv > spec.get("max", 2**53):
            return None, False
        return iv, True
    if t == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None, False
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None, False
        if fv != fv or fv < spec.get("min", -1e18) or fv > spec.get("max", 1e18):
            return None, False
        return fv, True
    if t == "bool":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true", True
        return None, False
    if t == "enum":
        if isinstance(value, str) and value in spec["values"]:
            return value, True
        return None, False
    if t == "url":
        if not isinstance(value, str):
            return None, False
        clean = value.strip()
        if check_url(clean, allow_punycode=bool(spec.get("allow_punycode"))):
            return None, False
        return clean, True
    if t == "list_str":
        if not isinstance(value, list):
            return None, False
        out = []
        for v in value[: int(spec.get("max_items", 20))]:
            c = sanitize_str(v, int(spec.get("max", 100)))
            if c:
                out.append(c)
        return out, True
    return None, False


def project(data: Any, schema: dict, now: float | None = None, *, detectors_cfg: dict | None = None,
            allowlisted_hosts: tuple[str, ...] = ()) -> Projection:
    """Keep only declared fields of each record under ``schema``.

    Every row gets ``_taint`` (tuple of codes) and ``_severity`` (None|medium|high) from the
    content detectors run over its text fields. Rows missing a required field are dropped and
    counted; document-level structure findings go to ``Projection.findings``.
    """
    now = time.time() if now is None else now
    det = detectors_cfg or {}
    p = Projection()
    max_depth = int(det.get("max_depth", 32))
    p.findings.extend(detectors.check_structure(data, max_depth=max_depth))
    if any(f.code == "depth_exceeded" for f in p.findings):
        return p
    records = _walk(data, schema.get("root"))
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        p.findings.append(detectors.Finding("high", "root_shape", type(records).__name__))
        return p
    max_items = int(schema.get("max_items", 1000))
    if len(records) > max_items:
        p.findings.append(detectors.Finding("high", "too_many_items", str(len(records))))
        records = records[:max_items]
    fields: dict = schema["fields"]
    required = list(schema.get("required", []))
    for rec in records:
        if not isinstance(rec, dict):
            p.dropped_rows += 1
            p.violations += 1
            p.checked += 1
            continue
        row: dict = {}
        taint: list[detectors.Finding] = []
        bad_required = False
        for name, spec in fields.items():
            raw = _walk(rec, spec.get("path")) if spec.get("path") else rec.get(name)
            p.checked += 1
            spec = {**spec, "_name": name}
            clean, ok = _coerce(raw, spec, now, allowlisted_hosts=allowlisted_hosts, taint_findings=taint, det=det)
            if not ok:
                p.violations += 1
                if name in required:
                    bad_required = True
                continue
            if clean is None:
                if name in required:
                    bad_required = True
                continue
            row[name] = clean
        if bad_required:
            p.dropped_rows += 1
            continue
        row["_taint"] = detectors.codes(taint)
        row["_severity"] = detectors.worst(taint)
        p.rows.append(row)
    ratio = float(det.get("schema_violation_ratio", 0.20))
    if p.drift(ratio):
        p.findings.append(detectors.Finding("high", "schema_drift", f"{p.violations}/{p.checked}"))
    return p
