"""Policy: what may be fetched, admitted, stored and sent to a model.

The shipped ``policy.yaml`` is the floor. A user policy at ``$NINSHUBUR_HOME/policy.yaml``
may tighten any setting but never loosen one; keys it is not allowed to touch are ignored
and reported. Loading fails closed: if either file is unreadable or malformed, no fetch
can happen because no ``Policy`` object exists.
"""
from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml

SHIPPED = Path(__file__).with_name("policy.yaml")

SECONDS_PER_DAY = 86400.0
SOURCE_HEALTH_FETCHABLE = frozenset({"ok", "suspect"})
HARD_CRITERIA = ("H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8")


class PolicyError(Exception):
    """Policy could not be loaded or a user policy tried to loosen the floor."""


@dataclass(frozen=True)
class Refusal:
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class Check:
    criterion: str
    ok: bool
    detail: str


# --- merge rules ------------------------------------------------------------------------
# path -> how the user value combines with the shipped value. Anything not listed here is
# read-only for the user policy.
MERGE_RULES: dict[tuple[str, ...], str] = {
    ("fetch", "denied_hosts"): "union",
    ("fetch", "max_hops"): "min",
    ("fetch", "max_body_bytes_default"): "min",
    ("fetch", "max_body_bytes_hard"): "min",
    ("fetch", "decompression_ratio_max"): "min",
    ("fetch", "per_host_rps_max"): "min",
    ("fetch", "robots", "cache_s"): "min",
    ("fetch", "robots", "default_crawl_delay_s"): "max",
    ("fetch", "derived_links", "allowed_adapters"): "intersection",
    ("fetch", "derived_links", "max_per_document"): "min",
    ("fetch", "derived_links", "path_terms"): "intersection",
    ("fetch", "derived_links", "max_query_len"): "min",
    ("admission", "min_field_validity"): "max",
    ("reputation", "suspect_below"): "max",
    ("reputation", "quarantine_below"): "max",
    ("reputation", "taint_events_24h"): "min",
    ("detectors", "schema_violation_ratio"): "min",
    ("detectors", "item_explosion_factor"): "min",
    ("detectors", "max_depth"): "min",
    ("detectors", "base64_run_min"): "min",
    ("detectors", "lookalike_max_distance"): "max",
    ("person_data", "allowed_sources"): "intersection",
    ("person_data", "allowed_fields"): "intersection",
    ("person_data", "forbidden_fields"): "union",
    ("legal_basis", "allowed"): "intersection",
    ("retention", "months"): "min",
    ("retention", "snapshots_days"): "min",
    ("retention", "quarantine_days"): "min",
    ("corroboration", "value_usd"): "min",
}
# Booleans that are "safe" when true: the user may set them true, never false.
TRUE_ONLY: frozenset[tuple[str, ...]] = frozenset({
    ("fetch", "https_only_except_local"),
    ("fetch", "robots", "honour"),
    ("fetch", "refuse_private_addresses"),
    ("fetch", "derived_links", "same_host_only"),
    ("detectors", "medium_holds"),
    ("person_data", "social", "skip_noindex"),
    ("person_data", "social", "skip_undiscoverable"),
    ("person_data", "social", "skip_no_unauthenticated_label"),
    ("person_data", "llm", "require_local"),
})


def _flatten(d: dict, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for k, v in d.items():
        p = prefix + (str(k),)
        if isinstance(v, dict) and p not in MERGE_RULES:
            out.update(_flatten(v, p))
        else:
            out[p] = v
    return out


def _get(d: dict, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set(d: dict, path: tuple[str, ...], value: Any) -> None:
    cur = d
    for k in path[:-1]:
        cur = cur.setdefault(k, {})
    cur[path[-1]] = value


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    return [v]


def merge_tightening(shipped: dict, user: dict | None) -> tuple[dict, list[str]]:
    """Return (merged, notes). Notes name every user key that was ignored and why."""
    merged = yaml.safe_load(yaml.safe_dump(shipped))  # deep copy via YAML round trip
    notes: list[str] = []
    if not user:
        return merged, notes
    if not isinstance(user, dict):
        raise PolicyError("user policy must be a mapping")
    for path, uval in _flatten(user).items():
        key = ".".join(path)
        sval = _get(shipped, path)
        if path in TRUE_ONLY:
            if uval is True:
                _set(merged, path, True)
            elif sval is True:
                notes.append(f"ignored {key}: cannot be set to false")
            continue
        rule = MERGE_RULES.get(path)
        if rule is None:
            notes.append(f"ignored {key}: not tightenable by the user policy")
            continue
        try:
            if rule == "union":
                _set(merged, path, sorted(set(map(str, _as_list(sval))) | set(map(str, _as_list(uval)))))
            elif rule == "intersection":
                _set(merged, path, [x for x in _as_list(sval) if x in set(map(str, _as_list(uval)))])
            elif rule == "min":
                if sval is None or float(uval) <= float(sval):
                    _set(merged, path, type(sval)(uval) if sval is not None else uval)
                else:
                    notes.append(f"ignored {key}: {uval!r} would loosen {sval!r}")
            elif rule == "max":
                if sval is None or float(uval) >= float(sval):
                    _set(merged, path, type(sval)(uval) if sval is not None else uval)
                else:
                    notes.append(f"ignored {key}: {uval!r} would loosen {sval!r}")
        except (TypeError, ValueError) as e:
            notes.append(f"ignored {key}: bad value ({e})")
    return merged, notes


# --- URL and host checks --------------------------------------------------------------------
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def check_url(url: str, *, allow_http_local: bool = False, allow_punycode: bool = False) -> str | None:
    """Return a refusal code or None. Pure; shared by policy, schemas and detectors."""
    if not isinstance(url, str) or not url:
        return "url_empty"
    if len(url) > 2048:
        return "url_too_long"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url) or any(c.isspace() for c in url):
        return "url_control_chars"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "url_unparseable"
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return "url_scheme"
    if not host:
        return "url_no_host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "url_userinfo"
    if is_ip_literal(host):
        if parts.scheme == "http" and allow_http_local and host in LOCAL_HOSTS:
            return None
        return "url_ip_literal"
    if parts.scheme == "http" and not (allow_http_local and host in LOCAL_HOSTS):
        return "url_not_https"
    if "*" in host:
        return "url_wildcard"
    if not allow_punycode and any(label.startswith("xn--") for label in host.split(".")):
        return "url_punycode"
    if not _HOST_RE.match(host):
        return "url_bad_host"
    return None


def same_site(a: str, b: str) -> bool:
    """True when two hosts are the same site modulo a leading ``www.`` (the one redirect shape that is
    both ubiquitous and harmless). Nothing else counts: a subdomain is a different site here."""
    a, b = a.lower(), b.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return bool(a) and strip(a) == strip(b)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _parse_day(v: Any) -> float | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=v.tzinfo or timezone.utc).timestamp()
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc).timestamp()
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d.replace(tzinfo=d.tzinfo or timezone.utc).timestamp()
    return None


class Policy:
    def __init__(self, data: dict, notes: Iterable[str] = ()):
        self.data = data
        self.notes = list(notes)
        self._validate()
        self.denied_hosts: frozenset[str] = frozenset(h.lower() for h in _as_list(_get(data, ("fetch", "denied_hosts"))))
        self.allowed_person_fields: frozenset[str] = frozenset(_as_list(_get(data, ("person_data", "allowed_fields"))))
        self.forbidden_person_fields: frozenset[str] = frozenset(_as_list(_get(data, ("person_data", "forbidden_fields"))))
        self.allowed_person_sources: frozenset[str] = frozenset(_as_list(_get(data, ("person_data", "allowed_sources"))))
        self.legal_bases: frozenset[str] = frozenset(_as_list(_get(data, ("legal_basis", "allowed"))))

    # -- loading ------------------------------------------------------------------------------
    @classmethod
    def load(cls, home: Path | None = None, shipped: Path = SHIPPED) -> "Policy":
        try:
            base = yaml.safe_load(shipped.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise PolicyError(f"cannot load shipped policy {shipped}: {e}") from e
        if not isinstance(base, dict):
            raise PolicyError("shipped policy is not a mapping")
        user: dict | None = None
        if home is not None:
            up = Path(home) / "policy.yaml"
            if up.is_file():
                try:
                    user = yaml.safe_load(up.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError) as e:
                    raise PolicyError(f"cannot load user policy {up}: {e}") from e
        merged, notes = merge_tightening(base, user)
        return cls(merged, notes)

    def _validate(self) -> None:
        for path in (("fetch", "denied_hosts"), ("admission", "tiers"), ("person_data", "allowed_fields"),
                     ("reputation", "suspect_below"), ("retention", "months"), ("legal_basis", "allowed")):
            if _get(self.data, path) is None:
                raise PolicyError(f"policy missing {'.'.join(path)}")
        tiers = _get(self.data, ("admission", "tiers"))
        if not isinstance(tiers, dict) or not all(int(k) in (0, 1, 2, 3) for k in tiers):
            raise PolicyError("policy admission.tiers must define tiers 0-3")

    def get(self, *path: str, default: Any = None) -> Any:
        v = _get(self.data, path)
        return default if v is None else v

    def tier(self, n: int) -> dict:
        tiers = _get(self.data, ("admission", "tiers"))
        t = tiers.get(n, tiers.get(str(n)))
        if t is None:
            raise PolicyError(f"unknown tier {n}")
        return t

    # -- fetching -----------------------------------------------------------------------------
    def host_denied(self, host: str) -> bool:
        host = host.lower()
        return host in self.denied_hosts or any(host.endswith("." + d) for d in self.denied_hosts)

    def allow_fetch(self, url: str, *, spec_hosts: Iterable[str], method: str = "GET",
                    allowed_methods: Iterable[str] = ("GET",), source_health: str = "ok",
                    review_due_at: float | None = None, now: float | None = None,
                    extra_hosts: Iterable[str] = ()) -> Refusal | None:
        """The single admission gate for a network request. None means allowed.

        ``extra_hosts`` is the per-run allowlist a dynamic-host adapter receives from the scheduler
        (hosts of event URLs taken from an admitted Tier 1 dataset); it is never derived from the page
        being fetched."""
        now = time.time() if now is None else now
        code = check_url(url, allow_http_local=False)
        if code:
            return Refusal(code, f"url rejected: {url[:120]}")
        host = host_of(url)
        if self.host_denied(host):
            return Refusal("host_denied", f"{host} is in denied_hosts")
        hosts = {h.lower() for h in spec_hosts} | {h.lower() for h in extra_hosts}
        if host not in hosts:
            return Refusal("host_not_in_spec", f"{host} is not one of the adapter's hosts")
        if method.upper() not in {m.upper() for m in allowed_methods}:
            return Refusal("method_not_allowed", f"{method} not allowed for this adapter")
        if source_health not in SOURCE_HEALTH_FETCHABLE:
            return Refusal("source_health", f"source health is {source_health}")
        if review_due_at is not None and review_due_at <= now:
            return Refusal("review_overdue", "source review is overdue (H8)")
        return None

    def allow_derived_link(self, url: str, *, base_url: str, adapter: str, anchor_text: str = "") -> Refusal | None:
        """May ``adapter`` follow ``url`` found on the page ``base_url``? The only relaxation of the
        never-fetch-content-URLs rule, and a narrow one."""
        dl = self.get("fetch", "derived_links", default={}) or {}
        if adapter not in set(_as_list(dl.get("allowed_adapters"))):
            return Refusal("derived_links_not_allowed", f"adapter {adapter} may not follow links")
        code = check_url(url)
        if code:
            return Refusal(code, f"derived url rejected: {url[:120]}")
        host, base_host = host_of(url), host_of(base_url)
        if self.host_denied(host):
            return Refusal("host_denied", f"{host} is in denied_hosts")
        if dl.get("same_host_only", True) and not same_site(host, base_host):
            return Refusal("derived_off_host", f"{host} is not {base_host}")
        parts = urlsplit(url)
        if parts.fragment:
            return Refusal("derived_fragment", "fragment links are the same page")
        if len(parts.query) > int(dl.get("max_query_len", 200)):
            return Refusal("derived_query_too_long", "query string over the limit")
        terms = [str(t).lower() for t in _as_list(dl.get("path_terms"))]
        hay = (parts.path + " " + parts.query + " " + (anchor_text or "")).lower()
        if terms and not any(t in hay for t in terms):
            return Refusal("derived_path_term", "link path/text matches none of the allowed terms")
        return None

    # -- admission ----------------------------------------------------------------------------
    def admission_check(self, trust: dict, spec: Any, test: dict | None = None,
                        now: float | None = None) -> list[Check]:
        """Run H1-H8. ``spec`` is an AdapterSpec-like object; ``test`` holds the results of a
        test fetch: status, final_host, off_host_redirect, content_type, size, field_validity,
        tls_ok, robots_allowed, unauthenticated."""
        now = time.time() if now is None else now
        t = test or {}
        checks: list[Check] = []
        trust = trust or {}
        hosts = {h.lower() for h in getattr(spec, "hosts", ())}

        owner, owner_url = trust.get("owner"), trust.get("owner_url")
        ok = bool(owner) and bool(owner_url) and check_url(str(owner_url or "")) is None
        checks.append(Check("H1", ok, "owner and owner_url recorded" if ok else "owner / owner_url missing or invalid"))

        basis = trust.get("legal_basis")
        ok = bool(trust.get("terms_url")) and bool(trust.get("licence")) and basis in self.legal_bases
        robots_ok = t.get("robots_allowed", True)
        checks.append(Check("H2", ok and bool(robots_ok),
                            "terms, licence, legal basis recorded; robots allows" if ok and robots_ok
                            else ("robots.txt disallows the planned path" if ok else "terms_url / licence / legal_basis missing or not allowed")))

        url_codes = [check_url(u) for u in t.get("urls", [])]
        bad = [c for c in url_codes if c]
        tls_ok = t.get("tls_ok", True)
        off_host = t.get("off_host_redirect", False)
        ok = not bad and bool(tls_ok) and not off_host and all(host_of(u) in hosts for u in t.get("urls", []))
        checks.append(Check("H3", ok, "https, fixed hostnames, no off-host redirect" if ok
                            else f"transport integrity failed: {bad or ('tls' if not tls_ok else 'off-host redirect or host not in spec')}"))

        validity = t.get("field_validity")
        min_validity = float(self.get("admission", "min_field_validity", default=0.95))
        schema_known = bool(t.get("schema_known", trust.get("schema") is not None))
        ok = schema_known and validity is not None and float(validity) >= min_validity
        checks.append(Check("H4", ok, f"schema {trust.get('schema')} validity {validity}" if ok
                            else f"schema missing or field validity {validity} < {min_validity}"))

        needs_key = getattr(spec, "needs_key_env", None)
        status = t.get("status")
        ok = status == 200 and (needs_key is not None or bool(t.get("unauthenticated", True)))
        checks.append(Check("H5", ok, "public or licensed read works" if ok
                            else f"test fetch status {status}; login/paywall/CAPTCHA are refused"))

        size = t.get("size")
        max_bytes = int(t.get("max_bytes", self.get("fetch", "max_body_bytes_default", default=2_000_000)))
        ok = bool(trust.get("rate_limit")) and size is not None and int(size) <= max_bytes
        checks.append(Check("H6", ok, f"rate limit recorded, size {size} <= {max_bytes}" if ok
                            else "rate_limit missing or response over the size cap"))

        questions = tuple(getattr(spec, "questions", ()) or ())
        ok = len(questions) > 0 and bool(trust.get("schema"))
        checks.append(Check("H7", ok, f"{len(questions)} business question(s), schema declared" if ok
                            else "adapter declares no business question or no schema"))

        tier = trust.get("tier")
        reviewed_at = _parse_day(trust.get("reviewed_at"))
        every = trust.get("review_every_days")
        try:
            tier_default = int(self.tier(int(tier))["review_every_days"]) if tier is not None else None
        except (PolicyError, ValueError, TypeError):
            tier_default = None
        ok = (bool(trust.get("reviewed_by")) and reviewed_at is not None and tier_default is not None
              and every is not None and 0 < int(every) <= tier_default
              and reviewed_at + int(every) * SECONDS_PER_DAY > now)
        checks.append(Check("H8", ok, f"reviewed by {trust.get('reviewed_by')}, every {every} d" if ok
                            else "reviewer/date missing, interval above the tier maximum, or review overdue"))
        return checks

    @staticmethod
    def admitted(checks: Iterable[Check]) -> bool:
        return all(c.ok for c in checks if c.criterion in HARD_CRITERIA)

    def review_due_at(self, trust: dict) -> float | None:
        at = _parse_day(trust.get("reviewed_at"))
        every = trust.get("review_every_days")
        if at is None or every is None:
            return None
        return at + int(every) * SECONDS_PER_DAY

    # -- storing ------------------------------------------------------------------------------
    def allow_store(self, fields: dict, *, source_adapter: str) -> tuple[dict, list[str]]:
        """Project person fields through the whitelist. Returns (kept, dropped_keys)."""
        if source_adapter not in self.allowed_person_sources:
            return {}, sorted(fields)
        kept: dict = {}
        dropped: list[str] = []
        for k, v in fields.items():
            if k in self.allowed_person_fields and k not in self.forbidden_person_fields and v not in (None, ""):
                kept[k] = v
            else:
                dropped.append(k)
        return kept, sorted(dropped)

    def allow_llm(self, *, person_data: bool, local: bool) -> Refusal | None:
        if person_data and not local and self.get("person_data", "llm", "require_local", default=True):
            return Refusal("person_data_remote_llm", "person data may only go to a local model")
        return None

    def retention_until(self, fetched_at: float) -> float:
        months = int(self.get("retention", "months", default=12))
        return fetched_at + months * 30.4375 * SECONDS_PER_DAY
