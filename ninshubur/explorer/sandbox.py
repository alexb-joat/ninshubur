"""The explorer's parser runs in a bubblewrap (``bwrap``) sandbox, so a parser bug a hostile page can reach gets a
process with nothing to take:

- no network (``--unshare-all`` gives it a fresh network namespace with no interfaces up);
- no ``$HOME``: the root is read-only ``/usr`` (plus the ``/bin``, ``/lib`` links) and the ``ninshubur`` package
  directory, bound read-only at ``/opt/ninshubur``; ``~/.ninshubur``, ``~/.ssh`` and the rest of ``/home`` don't exist
  inside; ``/tmp`` is an empty tmpfs;
- no secrets: ``--clearenv`` (API keys never enter), Python in isolated mode (``-I``: no ``PYTHON*`` variables, no
  user site), ``--cap-drop ALL``;
- no escape from the terminal or the parent: ``--new-session`` (no TIOCSTI), ``--die-with-parent``, its own PID
  namespace;
- bounded cost: rlimits on address space, CPU seconds, open files and output size (stdout is an in-memory file
  capped by ``RLIMIT_FSIZE``), and a wall-clock timeout that kills the whole process group.

Nothing here trusts the sandbox's answer: ``check_output`` re-checks its shape and every size, and ``read_page``
scans the text for taint before anything is used. Without a working ``bwrap`` the explorer does not parse at all
(fail closed); ``ninshubur explore check`` says why and proves the fences with a control.

Linux only (bubblewrap is a Linux tool). No network call is made here.
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .. import detectors
from ..schemas import sanitize_str

INSIDE = "/opt/ninshubur"
BOOT = (f"import sys; sys.path.insert(0, {INSIDE!r}); "
        "from ninshubur.explorer.extract import sandbox_main; sandbox_main()")
# Set by bwrap (PWD) and by Python's locale coercion (LC_CTYPE, PEP 538); everything else is cleared.
HARMLESS_ENV = frozenset({"PWD", "LC_CTYPE"})
RECORD_KEYS = frozenset({"type", "name", "url", "start", "end", "place", "org"})
RECORD_CAPS = {"name": 200, "url": 2048, "start": 40, "end": 40, "place": 160, "org": 160}
TEXT_CAPS = {"title": 300, "description": 500, "lang": 20}


class SandboxError(Exception):
    """The sandbox refused, failed or answered nonsense. ``code`` names which, for the log and the frontier."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SandboxLimits:
    memory_mb: int = 1024
    cpu_s: int = 10
    timeout_s: float = 20.0
    max_output: int = 2_000_000
    max_files: int = 32


def limits_from(lim) -> SandboxLimits:
    """The sandbox's limits from the frontier's ``Limits`` (policy.yaml ``explorer:``)."""
    return SandboxLimits(memory_mb=lim.parser_memory_mb, cpu_s=lim.parser_cpu_s, timeout_s=lim.parser_timeout_s,
                         max_output=max(2_000_000, 2 * lim.max_body_bytes))


def _base_python() -> str:
    """The real interpreter behind a virtualenv: the venv itself lives under ``$HOME``, which the sandbox lacks."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


class Sandbox:
    def __init__(self, limits: SandboxLimits | None = None, *, bwrap: str | None = None, python: str | None = None):
        self.limits = limits or SandboxLimits()
        self.bwrap = bwrap if bwrap is not None else shutil.which("bwrap")
        self.python = python or _base_python()
        self.package = Path(__file__).resolve().parents[1]  # .../ninshubur

    def unavailable(self) -> str | None:
        """Why the sandbox can't run here, with the fix; None when it can."""
        if not sys.platform.startswith("linux"):
            return f"the explorer's parser sandbox needs Linux (bubblewrap); this is {sys.platform}"
        if not self.bwrap:
            return "bwrap is not installed: install bubblewrap (apt install bubblewrap, dnf install bubblewrap)"
        return None

    def argv(self, code: str, args: tuple[str, ...] = ()) -> list[str]:
        a = [self.bwrap or "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--cap-drop", "ALL",
             "--ro-bind", "/usr", "/usr"]
        for p in ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32"):
            if os.path.islink(p):
                a += ["--symlink", os.readlink(p), p]
            elif os.path.isdir(p):
                a += ["--ro-bind", p, p]
        prefix = os.path.realpath(sys.base_prefix)
        if not prefix.startswith("/usr"):  # an interpreter installed outside /usr (pyenv, uv): its own tree only
            a += ["--ro-bind", prefix, prefix]
        a += ["--ro-bind", str(self.package), f"{INSIDE}/ninshubur", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/tmp",
              "--", self.python, "-I", "-B", "-X", "utf8", "-c", code, *args]
        return a

    def _limits(self) -> None:  # runs in the child between fork and exec
        lim = self.limits
        mem = lim.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_s, lim.cpu_s + 1))  # SIGXCPU at the soft limit, SIGKILL after
        resource.setrlimit(resource.RLIMIT_NOFILE, (lim.max_files, lim.max_files))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim.max_output, lim.max_output))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def run(self, code: str, stdin: bytes = b"", args: tuple[str, ...] = ()) -> bytes:
        """Run ``code`` with Python inside the sandbox; return its stdout (at most ``max_output`` bytes)."""
        why = self.unavailable()
        if why:
            raise SandboxError("sandbox_unavailable", why)
        out_fd, err_fd = os.memfd_create("explorer-out"), os.memfd_create("explorer-err")
        try:
            try:
                proc = subprocess.Popen(self.argv(code, args), stdin=subprocess.PIPE, stdout=out_fd, stderr=err_fd,
                                        preexec_fn=self._limits, start_new_session=True, close_fds=True, env={})
            except OSError as e:
                raise SandboxError("sandbox_unavailable", f"{self.bwrap} did not start: {e}") from e
            try:
                proc.communicate(stdin, timeout=self.limits.timeout_s)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise SandboxError("parser_timeout", f"no answer in {self.limits.timeout_s:g} s") from None
            out, err = _read(out_fd, self.limits.max_output), _read(err_fd, 4096)
        finally:
            os.close(out_fd)
            os.close(err_fd)
        rc = proc.returncode
        if rc == 0:
            return out
        sig = -rc if rc < 0 else rc - 128 if rc > 128 else 0  # bwrap passes a killed child on as 128 + signal
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        if sig == signal.SIGXCPU:
            raise SandboxError("parser_timeout", f"over {self.limits.cpu_s} CPU seconds")
        if sig == signal.SIGXFSZ or "[Errno 27]" in tail[0]:  # Python ignores SIGXFSZ: the write fails with EFBIG
            raise SandboxError("parser_output_too_large", f"over {self.limits.max_output:,} bytes")
        if sig == signal.SIGKILL:
            raise SandboxError("parser_killed", "killed (CPU hard limit or out of memory)")
        if "MemoryError" in tail[0]:
            raise SandboxError("parser_memory", f"over {self.limits.memory_mb} MB")
        if b"bwrap:" in err[:200]:
            raise SandboxError("sandbox_unavailable", tail[0][:200] + " (unprivileged user namespaces may be off)")
        raise SandboxError("parser_crashed", f"exit {rc}: {tail[0][:200]}")

    def parse(self, body: bytes, url: str, content_type: str) -> dict:
        header = json.dumps({"url": url, "content_type": content_type, "max_bytes": len(body)}).encode()
        return check_output(self.run(BOOT, header + b"\n" + body), self.limits.max_output)

    def self_test(self, paths: list[Path]) -> dict:
        """Prove the fences, with a control: the same probe runs inside the sandbox and as a plain child process.
        It reports whether each path can be read, whether a TCP listener this process opens on 127.0.0.1 can be
        reached (counted only if its greeting byte comes back), whether any route out exists (connect to 192.0.2.1, a
        documentation address: an isolated namespace fails at once with "network unreachable"; outside, a connect
        that "succeeds" proves nothing, since a middlebox may answer), and which environment variables are visible."""
        srv = socket.create_server(("127.0.0.1", 0))
        srv.settimeout(5)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def greet():
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except OSError:
                    continue
                with c:
                    c.sendall(b"!")

        t = threading.Thread(target=greet, daemon=True)
        t.start()
        spec = json.dumps({"paths": [str(p) for p in paths], "port": port})
        try:
            inside = json.loads(self.run(PROBE, args=(spec,)))
            plain = subprocess.run([self.python, "-I", "-c", PROBE, spec], capture_output=True, timeout=30,
                                   env={"NINSHUBUR_PROBE": "control"})
            control = json.loads(plain.stdout or b"{}")
        finally:
            stop.set()
            srv.close()
            t.join(timeout=6)
        fenced = (not any(v == "readable" for v in inside["paths"].values()) and inside["loopback"] != "reached"
                  and inside["internet"] == "unreachable" and not set(inside["env"]) - HARMLESS_ENV)
        works = control.get("loopback") == "reached" and "NINSHUBUR_PROBE" in control.get("env", [])
        return {"inside": inside, "control": control, "fenced": fenced, "control_ok": works}


PROBE = r"""
import json, os, socket, sys
spec = json.loads(sys.argv[1])
res = {"paths": {}, "env": sorted(os.environ)}
for p in spec["paths"]:
    try:
        (os.listdir(p) if os.path.isdir(p) else open(p, "rb").close())
        res["paths"][p] = "readable"
    except FileNotFoundError:
        res["paths"][p] = "absent"
    except OSError as e:
        res["paths"][p] = "denied: " + type(e).__name__
def reach(addr, port, want_byte):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect((addr, port))
        if not want_byte:
            return "connected (not proof: a middlebox may answer)"
        return "reached" if s.recv(1) == b"!" else "connected, no greeting"
    except OSError as e:
        return "unreachable" if getattr(e, "errno", None) in (101, 113) else type(e).__name__
    finally:
        s.close()
res["loopback"] = reach("127.0.0.1", spec["port"], True)
res["internet"] = reach("192.0.2.1", 443, False)
print(json.dumps(res))
"""


def _read(fd: int, cap: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, n = [], 0
    while n <= cap:
        b = os.read(fd, min(65536, cap + 1 - n))
        if not b:
            break
        chunks.append(b)
        n += len(b)
    return b"".join(chunks)


def _str(v, cap: int, where: str) -> str:
    if not isinstance(v, str) or len(v) > cap:
        raise SandboxError("parser_output_invalid", f"{where} is not a string of at most {cap} characters")
    return v


def check_output(raw: bytes, max_output: int) -> dict:
    """The sandbox's answer, re-checked field by field: exact keys, types and caps. Anything else is refused."""
    if len(raw) > max_output:
        raise SandboxError("parser_output_too_large", f"{len(raw):,} bytes")
    try:
        o = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise SandboxError("parser_output_invalid", f"not JSON: {type(e).__name__}") from None
    want = {"v", "title", "description", "lang", "links", "hidden_links", "records", "attr_texts", "limited", "nodes"}
    if not isinstance(o, dict) or set(o) != want or o["v"] != 1:
        raise SandboxError("parser_output_invalid", "unexpected keys or version")
    for k, cap in TEXT_CAPS.items():
        _str(o[k], cap, k)
    if not isinstance(o["links"], list) or len(o["links"]) > 200:
        raise SandboxError("parser_output_invalid", "links is not a list of at most 200")
    for u in o["links"]:
        _str(u, 2048, "a link")
    if not isinstance(o["attr_texts"], list) or len(o["attr_texts"]) > 40:
        raise SandboxError("parser_output_invalid", "attr_texts is not a list of at most 40")
    for t in o["attr_texts"]:
        _str(t, 300, "an attribute text")
    if not isinstance(o["records"], list) or len(o["records"]) > 50:
        raise SandboxError("parser_output_invalid", "records is not a list of at most 50")
    for r in o["records"]:
        if not isinstance(r, dict) or set(r) != RECORD_KEYS or r["type"] not in ("event", "hiring"):
            raise SandboxError("parser_output_invalid", "a record has unexpected keys or type")
        for k, cap in RECORD_CAPS.items():
            _str(r[k], cap, f"record {k}")
    for k in ("hidden_links", "nodes"):
        if type(o[k]) is not int or o[k] < 0:
            raise SandboxError("parser_output_invalid", f"{k} is not a count")
    if type(o["limited"]) is not bool:
        raise SandboxError("parser_output_invalid", "limited is not true/false")
    return o


@dataclass
class Page:
    """A parsed page after the parent's own checks. ``taint`` is None, "medium" or "high"; with "high" no record of
    the page is used. ``useful`` means it declared at least one event or job with a name and a date or URL."""
    title: str
    description: str
    lang: str
    links: list[str]
    hidden_links: int
    records: list[dict]
    taint: str | None = None
    taint_codes: tuple[str, ...] = ()
    limited: bool = False
    useful: bool = False
    texts_scanned: int = 0
    notes: list[str] = field(default_factory=list)


def read_page(o: dict) -> Page:
    """Scan every text the sandbox returned (title, description, alt/title/aria-label/meta texts, record fields) for
    taint on the raw string, then sanitise what is kept."""
    texts = [o["title"], o["description"], *o["attr_texts"]]
    for r in o["records"]:
        texts += [r["name"], r["place"], r["org"]]
    findings = [f for t in texts for f in detectors.scan_text(t)]
    taint = detectors.worst(findings)
    records = []
    if taint != "high":
        for r in o["records"]:
            clean = {k: (sanitize_str(v, RECORD_CAPS.get(k, 200)) or "") for k, v in r.items()}
            if clean["name"]:
                records.append(clean)
    useful = any(r["start"] or r["url"] for r in records)
    return Page(title=sanitize_str(o["title"], 300) or "", description=sanitize_str(o["description"], 500) or "",
                lang=sanitize_str(o["lang"], 20) or "", links=list(o["links"]), hidden_links=o["hidden_links"],
                records=records, taint=taint, taint_codes=detectors.codes(findings), limited=o["limited"],
                useful=useful, texts_scanned=len(texts))
