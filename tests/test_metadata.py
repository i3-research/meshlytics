"""Release metadata stays consistent: package version, CITATION.cff, .zenodo.json and CHANGELOG.md."""

import json
import re
from pathlib import Path

import meshlytics

ROOT = Path(__file__).resolve().parents[1]


def cff_text():
    return (ROOT / "CITATION.cff").read_text(encoding="utf-8")


def cff_authors(text):
    """(family, given) pairs from the authors list of CITATION.cff."""
    families = re.findall(r"^  - family-names: (.+)$", text, flags=re.M)
    givens = re.findall(r"^    given-names: (.+)$", text, flags=re.M)
    return list(zip(families, givens, strict=True))


def test_citation_version_matches_package():
    assert re.search(r"^version: (.+)$", cff_text(), flags=re.M).group(1).strip() == meshlytics.__version__


def test_zenodo_creators_match_citation_authors():
    zenodo = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    creators = [c["name"] for c in zenodo["creators"]]
    assert creators == [f"{family}, {given}" for family, given in cff_authors(cff_text())]
    assert zenodo["title"] == re.search(r'^title: "(.+)"$', cff_text(), flags=re.M).group(1)


def test_changelog_has_current_version():
    assert f"## [{meshlytics.__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_licence_is_the_same_everywhere():
    spdx = "PolyForm-Noncommercial-1.0.0"
    assert re.search(r"^license: (.+)$", cff_text(), flags=re.M).group(1).strip() == spdx
    zenodo = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    assert zenodo["license"] == spdx.lower()
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert licence.startswith("Required Notice: Copyright")
    assert "# PolyForm Noncommercial License 1.0.0" in licence
