"""Category co-occurrence at MeSH tree levels 1-3 (all tree positions; within-category pairs not counted)."""

import pandas as pd
import pytest

from meshlytics import utils
from pubmed_records import mesh_string


@pytest.fixture
def matrices(tmp_path, mesh_files):
    df = pd.DataFrame({"mesh_terms": [
        # paper 1: Diseases (C04 and C10), Named Groups (M01), Psychiatry (F03, level-2 descriptor)
        mesh_string(("Brain Neoplasms", "D001932"), ("Child", "D002648"), ("Mental Disorders", "D001523")),
        # paper 2: two descriptors of the same level-1 category -> no count at any level
        mesh_string(("Neoplasms", "D009369"), ("Brain Neoplasms", "D001932")),
        # paper 3: a single descriptor -> no pair
        mesh_string(("Child", "D002648")),
        None,
    ]})
    out = tmp_path / "out"
    paths = utils.generate_cooccurrence_matrix(df, mesh_files.keys, mesh_files.dictionary, str(out))

    def read(level):
        return pd.read_csv(out / f"level{level}_co_occurrence_matrix.csv", index_col=0)

    return paths, read(1), read(2), read(3), out


def test_level1_counts(matrices):
    _, level1, _, _, _ = matrices
    assert level1.loc["Diseases", "Named Groups"] == 1
    assert level1.loc["Diseases", "Psychiatry and Psychology"] == 1
    assert level1.loc["Named Groups", "Psychiatry and Psychology"] == 1
    assert (level1.values == level1.values.T).all()
    assert all(level1.loc[k, k] == 0 for k in level1.index)
    assert level1.loc["Anatomy"].sum() == 0


def test_level2_uses_every_tree_position(matrices):
    _, _, level2, _, _ = matrices
    # Brain Neoplasms sits in C04 and C10: both are paired with Persons (M01), once per descriptor pair
    assert level2.loc["Neoplasms", "Persons"] == 1
    assert level2.loc["Nervous System Diseases", "Persons"] == 1
    assert level2.loc["Neoplasms", "Mental Disorders"] == 1
    # paper 2 pairs C04 with C10 inside category C: not counted
    assert level2.loc["Neoplasms", "Nervous System Diseases"] == 0


def test_level3_skips_level2_descriptors(matrices):
    _, _, _, level3, _ = matrices
    assert level3.loc["Neoplasms by Site", "Age Groups"] == 1
    assert level3.loc["Central Nervous System Diseases", "Age Groups"] == 1
    # Mental Disorders has no level-3 position, so it adds nothing at level 3
    assert level3.loc["Neurodevelopmental Disorders"].sum() == 0


def test_outputs_written(matrices):
    paths, _, _, _, out = matrices
    assert (out / "level1_cooccurrence_heatmap.png").exists()
    assert (out / "UID_MeSH_term_path_with_levels.csv").exists()
    assert paths["level1_path"].endswith("level1_co_occurrence_matrix.csv")
