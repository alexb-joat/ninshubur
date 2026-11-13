"""Profiles: the qualify block is validated strictly, and keys nobody reads are named instead of ignored."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ninshubur.profiles import DEFAULT_LABELS, ProfileError, load_profiles

QUALIFY = {"question": "Would this company plausibly buy the service this quarter?", "subject": "company",
           "must": ["it sells to other businesses"], "must_not": ["it is an agency itself"], "labels": ["fit", "no_fit"]}


def load(tmp_path: Path, profile: dict):
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"profiles": {"acme": profile}}), encoding="utf-8")
    return load_profiles(path=path)["acme"]


def test_qualify_block_loads_and_first_label_is_positive(tmp_path: Path):
    q = load(tmp_path, {"qualify": QUALIFY}).qualify
    assert q.question.startswith("Would this company") and q.subject == "company"
    assert q.must == ("it sells to other businesses",) and q.must_not == ("it is an agency itself",)
    assert q.labels == ("fit", "no_fit") and q.positive == "fit"


def test_qualify_defaults_and_absence(tmp_path: Path):
    assert load(tmp_path, {"keywords": ["python"]}).qualify is None
    q = load(tmp_path, {"qualify": {"question": "Is this a fit for the service?"}}).qualify
    assert q.labels == DEFAULT_LABELS and q.subject == "company" and q.must == () and q.must_not == ()


def test_bare_yaml_yes_no_labels_are_read_as_words(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    path.write_text("profiles:\n  acme:\n    qualify:\n      question: Is this a fit for the service?\n      labels: [yes, no, unsure]\n",
                    encoding="utf-8")
    assert load_profiles(path=path)["acme"].qualify.labels == ("yes", "no", "unsure")


def test_question_whitespace_is_collapsed(tmp_path: Path):
    q = load(tmp_path, {"qualify": {"question": "Is this\n   a fit for\tthe service?"}}).qualify
    assert q.question == "Is this a fit for the service?"


@pytest.mark.parametrize("change, said", [
    ({"nice": ["a science domain"]}, "unknown key(s) nice"),
    ({"question": None}, "qualify.question must be text"),
    ({"question": "short"}, "10 to 400 characters"),
    ({"question": "Is this a fit?\x07 Really a fit?"}, "control character"),
    ({"labels": ["yes"]}, "2 to 5 answers"),
    ({"labels": ["yes", "Yes!"]}, "must be lower-case"),
    ({"labels": ["fit", "fit"]}, "repeats an answer"),
    ({"subject": "<b>company</b>"}, "short noun"),
    ({"must": "one sentence"}, "must be a list"),
    ({"must": ["x"] * 13}, "at most 12"),
    ({"must_not": [{"a": 1}]}, "must_not[0] must be text"),
])
def test_bad_qualify_blocks_are_refused_with_the_place(tmp_path: Path, change: dict, said: str):
    with pytest.raises(ProfileError) as e:
        load(tmp_path, {"qualify": {**QUALIFY, **change}})
    assert said in str(e.value) and "profile acme" in str(e.value)


def test_qualify_without_question_or_as_a_list_is_refused(tmp_path: Path):
    with pytest.raises(ProfileError, match="needs a question"):
        load(tmp_path, {"qualify": {"must": ["it sells to other businesses"]}})
    with pytest.raises(ProfileError, match="must be a mapping"):
        load(tmp_path, {"qualify": ["a question?"]})


def test_keys_nobody_reads_are_kept_for_the_check(tmp_path: Path):
    p = load(tmp_path, {"keywords": ["python"], "icp": {"summary": "x"}, "qualfy": {}})
    assert p.unknown_keys == ("icp", "qualfy")
    assert load(tmp_path, {"keywords": ["python"], "geos": {"cities": ["Lisbon"]}}).unknown_keys == ()


def test_example_profiles_load_with_a_qualify_block():
    example = Path(__file__).parent.parent / "profiles.example.yaml"
    profiles = load_profiles(path=example)
    assert profiles["agency-internal"].qualify.labels == ("yes", "no", "unsure")
    assert all(p.unknown_keys == () for p in profiles.values())


def test_profiles_check_names_the_qualify_block_and_unknown_keys(home, capsys):
    from ninshubur.__main__ import main
    from tests.conftest import write_config, write_profiles
    write_config(home)
    write_profiles(home, {"with-block": {"keywords": ["python"], "qualify": QUALIFY},
                          "without": {"keywords": ["python"], "icp": {"summary": "x"}}})
    assert main(["profiles", "check"]) == 0
    out = capsys.readouterr().out
    assert "ok: one company at a time, answers fit/no_fit (positive: fit), 1 must, 1 must_not" in out
    assert "not set (add a qualify: block" in out
    assert "warning: unknown key(s) ignored: icp" in out
