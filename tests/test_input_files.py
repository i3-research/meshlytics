"""-f input resolution: path as given, search by name in the current folder, choice among several files."""

import os

import pytest

from meshlytics.utils import resolve_input_file


def touch(path, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def no_input(prompt):
    pytest.fail("no question expected")


def test_path_as_given(tmp_path):
    f = touch(tmp_path / "data.xlsx")
    assert resolve_input_file(str(f), input_func=no_input) == str(f)
    assert resolve_input_file(str(tmp_path / "data"), input_func=no_input) == str(f)   # extension added


def test_found_by_name_in_subfolder_skipping_analysis_folders(tmp_path):
    f = touch(tmp_path / "TopCited_x" / "data.xlsx")
    touch(tmp_path / "Analysis_data_Nov19" / "data.xlsx")
    touch(tmp_path / ".hidden" / "data.xlsx")
    assert resolve_input_file("data", search_roots=[str(tmp_path)], input_func=no_input) == str(f)


def test_default_search_root_is_current_folder(tmp_path, monkeypatch):
    f = touch(tmp_path / "sub" / "papers.xlsx")
    monkeypatch.chdir(tmp_path)
    assert resolve_input_file("papers.xlsx", input_func=no_input) == str(f)


def test_several_matches_newest_first(tmp_path):
    old = touch(tmp_path / "a" / "data.xlsx", mtime=1_600_000_000)
    new = touch(tmp_path / "b" / "data.xlsx", mtime=1_700_000_000)
    roots = [str(tmp_path)]
    assert resolve_input_file("data", search_roots=roots, input_func=lambda p: "") == str(new)
    assert resolve_input_file("data", search_roots=roots, input_func=lambda p: "2") == str(old)
    with pytest.raises(FileNotFoundError):
        resolve_input_file("data", search_roots=roots, input_func=lambda p: "q")


def test_similar_names_offered(tmp_path):
    f = touch(tmp_path / "cohort_data.xlsx")
    assert resolve_input_file("cohort_dta", search_roots=[str(tmp_path)], input_func=lambda p: "1") == str(f)


def test_not_found(tmp_path):
    touch(tmp_path / "unrelated.xlsx")
    with pytest.raises(FileNotFoundError, match="Input file not found"):
        resolve_input_file("zzzz", search_roots=[str(tmp_path)], input_func=no_input)
