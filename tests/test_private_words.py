"""scripts/private_words.py: a listed word is found by hash in a file body, a file name, a pair, an accented or
full-width variant; a clean tree passes; and the output never carries the word. Synthetic repositories and made-up
words only; the words are built from parts so that this file itself holds none of them."""
from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("private_words", ROOT / "scripts" / "private_words.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

WORD = "zorb" + "laxgnu"                      # the made-up word whose hash ships in tests/private_words.sha256
ONE = "quux" + "ington"
PAIR = ("fro" + "bnitz", "wib" + "ble")
TRIPLE = ("snor" + "kel", "plun" + "der", "gri" + "ft")
SUMMARY = re.compile(r"^private-words check: \d+ files, \d+ tokens, \d+ hashes, \d+ hits$", re.M)


def h(*words: str) -> str:
    return pw.digest(pw.tokens(" ".join(words)))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv(pw.ENV, raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    return r


def put(repo: Path, rel: str, text: str | bytes) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def listing(repo: Path, *hashes: str, rel: str = "tests/private_words.sha256", comment: str = "") -> None:
    put(repo, rel, ("# made-up words for a test\n" + (f"# {comment}\n" if comment else "") + "\n".join(hashes) + "\n"))


def check(repo: Path, capsys, *extra: str) -> tuple[int, str]:
    git(repo, "add", "-A")
    code = pw.main(["check", "--root", str(repo), *extra])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_a_word_in_a_file_body_is_found_with_file_and_line_and_never_printed(repo, capsys):
    listing(repo, h(WORD))
    put(repo, "docs/notes.md", f"line one\nline two\nhello {WORD} again\n")
    put(repo, "src/clean.py", "print('nothing to see')\n")
    code, out = check(repo, capsys)
    assert code == 1 and "HIT docs/notes.md:3 (tracked list)" in out
    assert "clean.py" not in out and SUMMARY.search(out) and out.strip().endswith("1 hits")
    assert WORD not in out and WORD.upper() not in out.upper()


def test_a_word_in_a_file_name_is_found_and_the_name_is_not_printed(repo, capsys):
    listing(repo, h(WORD))
    put(repo, f"docs/{WORD}-plan.md", "an ordinary body\n")
    code, out = check(repo, capsys)
    assert code == 1 and re.search(r"HIT \[git ls-files no\. \d+\]:0 \(tracked list\)", out)
    assert WORD not in out and "-plan" not in out


def test_a_word_in_both_name_and_body_is_still_not_printed(repo, capsys):
    listing(repo, h(WORD))
    put(repo, f"{WORD}.txt", f"{WORD}\n")
    code, out = check(repo, capsys)
    assert code == 1 and out.count("HIT ") == 2 and WORD not in out


@pytest.mark.parametrize("text", [
    "Alpha-" + "{one}", "{one}", "x {ONE_UP} y", "a_b_{one}_c", "(\"{one}\")", "{one}.example.org", "{one}s? no: {one}",
], ids=["hyphen", "bare", "upper", "underscores", "quoted", "host", "twice"])
def test_a_single_word_is_found_however_it_is_set(repo, capsys, text):
    listing(repo, h(ONE))
    put(repo, "a.txt", text.replace("{one}", ONE).replace("{ONE_UP}", ONE.upper()) + "\n")
    code, out = check(repo, capsys)
    assert code == 1 and "HIT a.txt:1 (tracked list)" in out and ONE not in out.lower()


def test_accented_and_full_width_and_cased_variants_are_the_same_word(repo, capsys):
    listing(repo, h(WORD))
    accented = WORD.replace("o", "ö", 1).replace("a", "á", 1)
    full_width = "".join(chr(ord(c) + 0xFEE0) for c in WORD)
    put(repo, "a.txt", f"{accented}\n{full_width}\n{WORD.title()}\n{WORD.upper()}\n")
    code, out = check(repo, capsys)
    assert code == 1 and out.count("HIT a.txt:") == 4
    assert {m for m in re.findall(r"HIT a.txt:(\d)", out)} == {"1", "2", "3", "4"} and WORD not in out.lower()


def test_a_pair_and_a_triple_are_found_across_separators_but_not_one_part_alone(repo, capsys):
    listing(repo, h(*PAIR), h(*TRIPLE))
    put(repo, "pair.txt", f"{PAIR[0].title()}, {PAIR[1]}\nonly {PAIR[0]} here\nonly {PAIR[1]} here\n")
    put(repo, "triple.txt", f"{TRIPLE[0]}-{TRIPLE[1]}/{TRIPLE[2]}\n{TRIPLE[0]} {TRIPLE[1]}\n")
    put(repo, "split.txt", f"{PAIR[0]}\n{PAIR[1]}\n")  # across two lines is not a pair
    code, out = check(repo, capsys)
    assert code == 1
    assert "HIT pair.txt:1" in out and "pair.txt:2" not in out and "pair.txt:3" not in out
    assert "HIT triple.txt:1" in out and "triple.txt:2" not in out and "split.txt" not in out
    assert all(w not in out for w in (*PAIR, *TRIPLE))


def test_a_clean_tree_passes_and_says_how_much_it_read(repo, capsys):
    listing(repo, h(WORD))
    put(repo, "a.py", "def f():\n    return 1\n")
    put(repo, "b.md", "# Title\n\nSome words here.\n")
    code, out = check(repo, capsys)
    assert code == 0 and "private list: not set" in out
    # two files, not three: the hash file is not scanned. Tokens by hand: paths a.py, b.md (2 + 2); "def f():" and
    # "return 1" (2 + 2); "# Title" and "Some words here." (1 + 3)
    assert "private-words check: 2 files, 12 tokens, 1 hashes, 0 hits" in out
    assert SUMMARY.search(out)


def test_binary_files_are_skipped_but_their_names_are_not(repo, capsys):
    listing(repo, h(WORD))
    put(repo, "blob.bin", b"\x00\x01" + WORD.encode() + b"\n")
    code, out = check(repo, capsys)
    assert code == 0 and "0 hits" in out
    put(repo, f"{WORD}.bin", b"\x00\x01")
    code, out = check(repo, capsys)
    assert code == 1 and "no." in out and WORD not in out


def test_the_hash_files_themselves_are_not_scanned(repo, capsys):
    listing(repo, h(WORD), comment=f"the made-up word is {WORD}")
    code, out = check(repo, capsys)
    assert code == 0 and "0 hits" in out


def test_the_local_list_is_read_through_the_variable_and_named_in_the_hit(repo, capsys, monkeypatch, tmp_path):
    listing(repo, h(WORD))
    local = tmp_path / "outside" / "mine.sha256"
    local.parent.mkdir()
    local.write_text(f"# real words, outside the repository\n{h(ONE)}\n", encoding="utf-8")
    put(repo, "a.txt", f"a line with {ONE}\nand {WORD}\n")
    monkeypatch.setenv(pw.ENV, str(local))
    code, out = check(repo, capsys)
    assert code == 1 and "HIT a.txt:1 (local list)" in out and "HIT a.txt:2 (tracked list)" in out
    assert "private list: 1 hash(es) from NINSHUBUR_PRIVATE_WORDS_FILE" in out and ONE not in out and WORD not in out
    assert "2 hashes, 2 hits" in out
    monkeypatch.setenv(pw.ENV, str(tmp_path / "nope.sha256"))
    code, out = check(repo, capsys)
    assert code == 2 and "names no file" in out


def test_a_list_line_that_is_not_a_hash_is_refused_without_echoing_it(repo, capsys):
    listing(repo, WORD)  # someone pasted the word itself instead of its hash
    code, out = check(repo, capsys)
    assert code == 2 and "neither a comment nor a sha256 hash" in out and WORD not in out


def test_the_shipped_tracked_list_catches_its_made_up_word(repo, capsys):
    put(repo, "tests/private_words.sha256", (ROOT / "tests" / "private_words.sha256").read_text(encoding="utf-8"))
    put(repo, "x/y.txt", f"planted {WORD} here\n")
    code, out = check(repo, capsys)
    assert code == 1 and "HIT x/y.txt:1 (tracked list)" in out and WORD not in out


def test_the_shipped_tracked_list_is_comments_and_hashes_only():
    lines = (ROOT / "tests" / "private_words.sha256").read_text(encoding="utf-8").splitlines()
    hashes = [x for x in lines if x and not x.startswith("#")]
    assert hashes == [h(WORD)] and all(re.fullmatch(r"[0-9a-f]{64}", x) for x in hashes)
    assert any("Made with" in x for x in lines)


def test_the_real_tree_passes_the_tracked_list(capsys, monkeypatch):
    monkeypatch.delenv(pw.ENV, raising=False)
    assert pw.main(["check"]) == 0
    out = capsys.readouterr().out
    assert SUMMARY.search(out) and out.strip().endswith("0 hits") and "private list: not set" in out


def test_the_real_tree_passes_the_local_list_when_there_is_one(capsys):
    if not os.environ.get(pw.ENV):
        pytest.skip(f"{pw.ENV} is not set: the real words are kept outside the repository, so CI and a fresh clone "
                    "cannot check them; run `NINSHUBUR_PRIVATE_WORDS_FILE=<your list> python scripts/private_words.py check`")
    assert pw.main(["check"]) == 0
    out = capsys.readouterr().out
    assert SUMMARY.search(out) and out.strip().endswith("0 hits") and "hash(es) from NINSHUBUR_PRIVATE_WORDS_FILE" in out


# --- the hash subcommand -------------------------------------------------------------------------
def run_hash(text: str, capsys, monkeypatch) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = pw.main(["hash"])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_hash_reads_words_from_stdin_and_matches_the_check(capsys, monkeypatch):
    code, out = run_hash(f"# a comment\n\n{WORD}\n{PAIR[0].title()} {PAIR[1]}\n", capsys, monkeypatch)
    assert code == 0 and out.split() == [h(WORD), h(*PAIR)]
    assert run_hash("zörb" + "laxgnu\n", capsys, monkeypatch)[1].split() == [h(WORD)]  # an accent does not change it


def test_hash_refuses_a_long_phrase_or_nothing_and_does_not_echo(capsys, monkeypatch):
    long_phrase = " ".join(("alp" + "ha", "be" + "ta", "gam" + "ma", "del" + "ta"))
    code, out = run_hash(long_phrase + "\n", capsys, monkeypatch)
    assert code == 2 and "line 1" in out and "alpha" not in out.lower()
    code, out = run_hash("\n# only a comment\n", capsys, monkeypatch)
    assert code == 2 and "no words" in out
    code, out = run_hash("--\n", capsys, monkeypatch)  # no letters or digits: nothing to hash
    assert code == 2


def test_the_script_runs_as_a_program_with_the_documented_exit_codes(repo, tmp_path):
    listing(repo, h(WORD))
    put(repo, "a.txt", "clean\n")
    git(repo, "add", "-A")
    script = str(ROOT / "scripts" / "private_words.py")
    env = {k: v for k, v in os.environ.items() if k != pw.ENV}
    ok = subprocess.run([sys.executable, script, "check", "--root", str(repo)], capture_output=True, text=True, env=env)
    assert ok.returncode == 0 and SUMMARY.search(ok.stdout)
    put(repo, "a.txt", f"dirty {WORD}\n")
    bad = subprocess.run([sys.executable, script, "check", "--root", str(repo)], capture_output=True, text=True, env=env)
    assert bad.returncode == 1 and "HIT a.txt:1" in bad.stdout and WORD not in bad.stdout + bad.stderr
    not_a_repo = subprocess.run([sys.executable, script, "check", "--root", str(tmp_path)], capture_output=True, text=True, env=env)
    assert not_a_repo.returncode == 2 and "cannot check" in not_a_repo.stderr
    hashed = subprocess.run([sys.executable, script, "hash"], input=f"{WORD}\n", capture_output=True, text=True, env=env)
    assert hashed.returncode == 0 and hashed.stdout.strip() == h(WORD)
