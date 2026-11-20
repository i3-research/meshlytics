"""mesh-count: MeSH term table with counts and all tree positions."""

import pandas as pd

from meshlytics import fetch
from meshlytics.utils import KEYWORD_FILE_BASENAME
from pubmed_records import mesh_string


def write_cohort_workbook(path):
    cohorts = pd.DataFrame({"Cohort": ["CohortA", "CohortB"]})
    cohort_a = pd.DataFrame({
        "pubmed_id": [1, 2],
        "review_paper": ["No", "Yes"],
        "article_type": ["Journal Article", "Review"],
        "mesh_terms": [mesh_string(("Brain Neoplasms", "D001932"), ("Child", "D002648")),
                       mesh_string(("Mental Disorders", "D001523"))],
    })
    cohort_b = pd.DataFrame({
        "pubmed_id": [3, 4],
        "review_paper": ["No", "No"],
        "article_type": ["Journal Article", "Editorial"],
        "mesh_terms": [mesh_string(("Brain Neoplasms", "D001932"), ("Male", "D008297")),
                       mesh_string(("Child", "D002648"))],
    })
    with pd.ExcelWriter(path) as writer:
        cohorts.to_excel(writer, sheet_name="Cohorts", index=False)
        cohort_a.to_excel(writer, sheet_name="CohortA", index=False)
        cohort_b.to_excel(writer, sheet_name="CohortB", index=False)


def test_counts_and_tree_positions(tmp_path, mesh_files):
    data = tmp_path / "cohorts.xlsx"
    write_cohort_workbook(data)
    fetch.generate_mesh_terms_with_keys_and_counts(str(data), mesh_files.dictionary)

    table = pd.read_excel(tmp_path / f"{KEYWORD_FILE_BASENAME}.xlsx").set_index("Term")
    # review (paper 2) and non-original publication type (paper 4) are not counted
    assert set(table.index) == {"Brain Neoplasms", "Child", "Male"}
    assert table.loc["Brain Neoplasms", "Count"] == 2
    assert table.loc["Child", "Count"] == 1
    assert table.loc["Brain Neoplasms", "Tree positions"] == 2
    assert table.loc["Brain Neoplasms", "Path with Keys"] == (
        "C (Diseases) > C04 (Neoplasms) > C04.588 (Neoplasms by Site) | "
        "C (Diseases) > C10 (Nervous System Diseases) > C10.228 (Central Nervous System Diseases)")
    # check tags such as Male have no tree number but are listed so they can be marked
    assert table.loc["Male", "Tree positions"] == 0
    assert (table["filter_out_term?"] == "N").all()
    assert list(table["Count"]) == sorted(table["Count"], reverse=True)


def test_marked_keyword_file_is_never_overwritten(tmp_path, mesh_files):
    data = tmp_path / "cohorts.xlsx"
    write_cohort_workbook(data)
    marked = tmp_path / f"{KEYWORD_FILE_BASENAME}.xlsx"
    pd.DataFrame({"Term": ["Child"], "filter_out_term?": ["Y"]}).to_excel(marked, index=False)

    fetch.generate_mesh_terms_with_keys_and_counts(str(data), mesh_files.dictionary)

    assert pd.read_excel(marked)["Term"].tolist() == ["Child"]
    new_files = [p for p in tmp_path.glob(f"{KEYWORD_FILE_BASENAME}_*.xlsx")]
    assert len(new_files) == 1
