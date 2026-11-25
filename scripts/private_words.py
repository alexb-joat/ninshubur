#!/usr/bin/env python3
"""Words that must never reach the public tree, checked by hash.

    python scripts/private_words.py check [--root DIR] [--tracked FILE]
    python scripts/private_words.py hash < words.txt

``hash`` reads words (or short phrases of up to three words), one per line, from standard input and prints one
sha256 per line. The word never goes on the command line, where a shell history or a process list would keep it.
Lines that are blank or start with ``#`` are skipped.

``check`` scans the path and the text of every file ``git ls-files`` lists in the repository (binary files and the
hash files themselves are skipped) and compares what it finds with two lists of hashes:

- the tracked list, ``tests/private_words.sha256``: comment lines that say how it was made, and hashes only;
- the file named by ``NINSHUBUR_PRIVATE_WORDS_FILE``, if set: a list kept outside the repository, where the real words
  (a former employer, a person) are hashed. When it is not set, ``check`` says ``private list: not set``.

How a word is matched. Text is normalised with NFKD, combining marks are dropped, it is case-folded, and it is split
into tokens on every run of characters that are not letters or digits (so ``Zörb-Lax``, ``zorb lax`` and ``ZORB_LAX``
are the same two tokens). Per line, every token, every adjacent pair of tokens and every adjacent triple is joined
with one space and hashed with sha256; a hash on a list is a hit. A name is matched as one word, two or three; a longer
phrase has to be listed as its parts.

On a hit it prints the file and line number (line 0 for the path) and which list matched, never the matched text; a file
whose own name is a hit is shown as its number in `git ls-files`.
Exit status: 0 for no hit, 1 for any hit, 2 for a usage error.

What it does not do. It reads the working tree's tracked files only: not git history, not issues or pull requests, not
CI logs, not files that are not tracked. A word that was committed once and removed stays in history; this check does
not see that, and a hash of a short or guessable word can be confirmed by anyone who guesses it, so keep the real list
outside the repository.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ENV = "NINSHUBUR_PRIVATE_WORDS_FILE"
TRACKED_LIST = "tests/private_words.sha256"
MAX_WORDS = 3
_HEX = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"[^\W_]+")


def tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return _TOKEN.findall(s)


def digest(words: list[str]) -> str:
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def candidates(toks: list[str]):
    """Every token, adjacent pair and adjacent triple of one line, as hashes."""
    for n in range(1, MAX_WORDS + 1):
        for i in range(len(toks) - n + 1):
            yield digest(toks[i:i + n])


def load_list(path: Path) -> set[str]:
    out = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if line and not line.startswith("#"):
            if not _HEX.match(line):
                raise ValueError(f"{path.name}: a line that is neither a comment nor a sha256 hash")
            out.add(line)
    return out


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def cmd_hash(stdin) -> int:
    out = []
    for n, raw in enumerate(stdin.read().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        toks = tokens(line)
        if not toks or len(toks) > MAX_WORDS:
            print(f"line {n}: a word or phrase of one to {MAX_WORDS} words is needed; list a longer phrase as its parts", file=sys.stderr)
            return 2
        out.append(digest(toks))
    if not out:
        print("no words on standard input (one per line)", file=sys.stderr)
        return 2
    print("\n".join(out))
    return 0


def cmd_check(root: Path, tracked: Path) -> int:
    lists: dict[str, set[str]] = {}
    skip = {tracked.resolve()}
    if tracked.is_file():
        lists["tracked list"] = load_list(tracked)
    env = os.environ.get(ENV)
    if env:
        local = Path(env).expanduser()
        if not local.is_file():
            print(f"{ENV} is set but names no file", file=sys.stderr)
            return 2
        lists["local list"] = load_list(local)
        skip.add(local.resolve())
        print(f"private list: {len(lists['local list'])} hash(es) from {ENV}")
    else:
        print("private list: not set")
    known = set().union(*lists.values()) if lists else set()
    files = ntokens = hits = 0

    def scan(text: str) -> set[str]:
        nonlocal ntokens
        toks = tokens(text)
        ntokens += len(toks)
        return {h for h in candidates(toks) if h in known} if toks else set()

    for number, rel in enumerate(tracked_files(root), 1):
        path = root / rel
        if path.resolve() in skip or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        files += 1
        in_path = scan(rel)
        # A file whose own name is a hit is shown by its number in `git ls-files`: its path would print the word.
        shown = f"[git ls-files no. {number}]" if in_path else rel

        def report(line_no: int, found: set[str]) -> None:
            nonlocal hits
            for name, hs in lists.items():
                if found & hs:
                    hits += 1
                    print(f"HIT {shown}:{line_no} ({name})")

        report(0, in_path)
        if b"\0" in data[:8192]:
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            found = scan(line)
            if found:
                report(n, found)
    print(f"private-words check: {files} files, {ntokens} tokens, {len(known)} hashes, {hits} hits")
    return 1 if hits else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="private_words.py", description="Check the tracked files against hashed private words.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="scan the tracked files; print file:line and the list that matched, never the text")
    ck.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="the repository (default: this one)")
    ck.add_argument("--tracked", type=Path, help=f"the tracked list (default: <root>/{TRACKED_LIST})")
    sub.add_parser("hash", help="print the sha256 of each word or short phrase read from standard input")
    args = ap.parse_args(argv)
    if args.cmd == "hash":
        return cmd_hash(sys.stdin)
    root = args.root.resolve()
    try:
        return cmd_check(root, args.tracked or root / TRACKED_LIST)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"cannot check: {e if isinstance(e, ValueError) else 'is this a git repository, and is git installed?'}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
