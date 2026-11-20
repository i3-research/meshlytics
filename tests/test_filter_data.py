"""filter-data: removal of marked terms and of whole MeSH tree branches."""

from types import SimpleNamespace

import pandas as pd
import pytest

from meshlytics import filter_data
from meshlytics.utils import KEYWORD_FILE_BASENAME
from pubmed_records import mesh_string

C_PATH = "C (Diseases) > C04 (Neoplasms) > C04.588 (Neoplasms by Site)"
F_PATH = "F (Psychiatry and Psychology) > F03 (Mental Disorders)"


def test_path_codes():
    assert filter_data.path_codes(C_PATH) == ["C", "C04", "C04.588"]
    assert filter_data.path_codes("No tree position (check tag or descriptor not in this MeSH version)") == []


@pytest.mark.parametrize("categories, match, expected", [
    (["C"], "all", False),       # also indexed under F -> kept
    (["C"], "any", True),
    (["C", "F"], "all", True),
    (["C04"], "any", True),      # level-2 branch
    ([], "any", False),
])
def test_term_in_categories(categories, match, expected):
    assert filter_data.term_in_categories(f"{C_PATH} | {F_PATH}", categories, match) is expected


def write_keyword_file(path):
    pd.DataFrame({
        "Term": ["Child", "Brain Neoplasms", "Mixed Term", "Humans"],
        "Path with Keys": ["M (Named Groups) > M01 (Persons) > M01.060 (Age Groups)", C_PATH,
                           f"{C_PATH} | {F_PATH}", "B (Organisms) > B01 (Eukaryota) > B01.050 (Animals)"],
        "filter_out_term?": ["Yes", "N", "N", "N"],
    }).to_excel(path, index=False)


def test_keywords_from_marks_categories_and_special_terms(tmp_path):
    keywords = tmp_path / f"{KEYWORD_FILE_BASENAME}.xlsx"
    write_keyword_file(keywords)
    assert filter_data.get_keywords_to_filter(str(keywords), ["C"], ["Humans"], "all") == {"Child", "Brain Neoplasms", "Humans"}
    assert filter_data.get_keywords_to_filter(str(keywords), ["c"], [], "any") == {"Child", "Brain Neoplasms", "Mixed Term"}


def test_filter_mesh_terms_updates_count():
    text = mesh_string(("Child", "D002648"), ("Brain Neoplasms", "D001932"), ("Humans", "D006801"))
    cleaned, count = filter_data.filter_mesh_terms(text, {"Child", "Humans"})
    assert cleaned == "('Brain Neoplasms','D001932')"
    assert count == 1


def test_run_filter_data_end_to_end(tmp_path):
    data = tmp_path / "papers.xlsx"
    pd.DataFrame({"pubmed_id": [1, 2],
                  "mesh_term_count": [2, 1],
                  "mesh_terms": [mesh_string(("Child", "D002648"), ("Brain Neoplasms", "D001932")),
                                 mesh_string(("Child", "D002648"))]}).to_excel(data, index=False)
    write_keyword_file(tmp_path / f"{KEYWORD_FILE_BASENAME}.xlsx")

    args = SimpleNamespace(file=str(data), keywords=None, output=None, exclude_categories=[],
                           special_terms=[], category_match="all")
    filter_data.run_filter_data(args)

    out = pd.read_excel(tmp_path / "papers_filtered.xlsx")
    assert out["mesh_terms"].fillna("").tolist() == ["('Brain Neoplasms','D001932')", ""]
    assert out["mesh_term_count"].tolist() == [1, 0]


def test_missing_keyword_file_is_reported(tmp_path):
    data = tmp_path / "papers.xlsx"
    pd.DataFrame({"mesh_terms": []}).to_excel(data, index=False)
    with pytest.raises(FileNotFoundError, match="mesh-count"):
        filter_data.resolve_filter_paths(str(data))


def test_output_name_without_folder_is_saved_beside_input(tmp_path):
    data = tmp_path / "papers.xlsx"
    keywords = tmp_path / "k.txt"
    keywords.write_text("Child\n", encoding="utf-8")
    _, output = filter_data.resolve_filter_paths(str(data), str(keywords), "mine.xlsx")
    assert output == str(tmp_path / "mine.xlsx")
