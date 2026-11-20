"""MeSH tree mapping: every tree position of a descriptor is kept."""

import math

from meshlytics import utils

BRAIN_NEOPLASMS_PATHS = [("C", "C04", "C04.588"), ("C", "C10", "C10.228")]


def test_index_keeps_every_tree_position(mesh_files):
    index = utils.load_mesh_index(mesh_files.dictionary)
    assert index["D001932"]["term"] == "Brain Neoplasms"
    assert index["D001932"]["paths"] == BRAIN_NEOPLASMS_PATHS
    assert index["D002648"]["paths"] == [("M", "M01", "M01.060")]


def test_descriptor_with_level2_tree_number_has_no_level3(mesh_files):
    index = utils.load_mesh_index(mesh_files.dictionary)
    assert index["D009369"]["paths"] == [("C", "C04", None)]       # Neoplasms (C04)
    assert index["D001523"]["paths"] == [("F", "F03", None)]       # Mental Disorders (F03)


def test_index_is_cached_per_file(mesh_files):
    assert utils.load_mesh_index(mesh_files.dictionary) is utils.load_mesh_index(mesh_files.dictionary)


def test_format_mesh_path(mesh_files):
    keys = mesh_files.keys_dict
    assert utils.format_mesh_path(("C", "C04", "C04.588"), keys) == \
        "C (Diseases) > C04 (Neoplasms) > C04.588 (Neoplasms by Site)"
    assert utils.format_mesh_path(("C", "C04", None), keys) == "C (Diseases) > C04 (Neoplasms)"
    assert utils.format_mesh_path(("Z", "Z01", None), keys) == "Z (Unknown) > Z01 (Unknown)"


def test_terms_to_mesh_tuples(mesh_files):
    count, text = utils.get_mesh_tuples_from_terms(["Child", "Not a MeSH term", "Brain Neoplasms"], mesh_files.dictionary)
    assert count == 2
    assert text == "('Child','D002648'),('Brain Neoplasms','D001932')"


def test_parse_mesh_terms():
    text = "('Brain Neoplasms','D001932'),('Neoplasms, Glandular and Epithelial','D009375')"
    assert utils.parse_mesh_terms(text) == [("Brain Neoplasms", "D001932"),
                                            ("Neoplasms, Glandular and Epithelial", "D009375")]
    assert utils.parse_mesh_terms(math.nan) == []
    assert utils.parse_mesh_terms(None) == []
