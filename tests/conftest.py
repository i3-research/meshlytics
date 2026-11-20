"""
Shared fixtures for the Meshlytics test suite.

The tests run offline: every socket connection is refused, credentials from the environment are
removed, and web services (NCBI E-utilities, OpenAlex, OpenAI) are replaced by small fakes.
A tiny MeSH tree (fixture `mesh_files`) is used instead of the full MeSH dictionary where possible.
"""

import json
import os
import socket
from types import SimpleNamespace

import matplotlib
import pytest

matplotlib.use("Agg")

CREDENTIAL_ENV = ["NCBI_EMAIL", "NCBI_API_KEY", "OPENALEX_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "MESHLYTICS_CONFIG"]

# {level 1: {level 2: {level 3: {UID: term}}}}, same layout as essentials/MeSH_Dictionary_*.json.
# A level-3 key equal to its level-2 key marks a descriptor whose own tree number is a level-2 code.
MESH_DICTIONARY = {
    "A": {"A08": {"A08.186": {"D002490": "Central Nervous System"}}},
    "C": {
        "C04": {"C04": {"D009369": "Neoplasms"}, "C04.588": {"D001932": "Brain Neoplasms"}},
        "C10": {"C10.228": {"D001932": "Brain Neoplasms", "D002493": "Central Nervous System Diseases"}},
    },
    "F": {"F03": {"F03": {"D001523": "Mental Disorders"}, "F03.625": {"D065886": "Neurodevelopmental Disorders"}}},
    "M": {"M01": {"M01.060": {"D002648": "Child"}}},
}

KEYS_AND_CLASSES = {
    "A": "Anatomy", "C": "Diseases", "F": "Psychiatry and Psychology", "M": "Named Groups",
    "A08": "Nervous System", "C04": "Neoplasms", "C10": "Nervous System Diseases", "F03": "Mental Disorders",
    "M01": "Persons",
    "A08.186": "Central Nervous System", "C04.588": "Neoplasms by Site", "C10.228": "Central Nervous System Diseases",
    "F03.625": "Neurodevelopmental Disorders", "M01.060": "Age Groups",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Refuses every network connection, so a test can never reach a web service."""
    def refuse(*args, **kwargs):
        raise OSError("network access is disabled in the Meshlytics tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Removes credentials and config locations from the environment; clears the MeSH index cache."""
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    from meshlytics import utils
    utils._MESH_INDEX_CACHE.clear()
    yield
    utils._MESH_INDEX_CACHE.clear()


@pytest.fixture
def mesh_files(tmp_path):
    """Tiny MeSH dictionary and keys_and_classes.json in one folder (the layout fetch/mesh-count expect)."""
    folder = tmp_path / "essentials"
    folder.mkdir()
    dictionary = folder / "MeSH_Dictionary_test.json"
    keys = folder / "keys_and_classes.json"
    dictionary.write_text(json.dumps(MESH_DICTIONARY), encoding="utf-8")
    keys.write_text(json.dumps(KEYS_AND_CLASSES), encoding="utf-8")
    return SimpleNamespace(dir=str(folder), dictionary=str(dictionary), keys=str(keys), keys_dict=dict(KEYS_AND_CLASSES))


def pytest_configure(config):
    os.environ.setdefault("MPLBACKEND", "Agg")
