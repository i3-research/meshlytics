"""fetch: PubMed record parsing, publication-type screening and cross-cohort de-duplication (E-utilities mocked)."""

import logging

import pandas as pd
import pytest

from meshlytics import fetch
from pubmed_records import efetch_result, make_article

LOGGER = logging.getLogger("meshlytics-test")


# ---------- publication types and DOI ----------

def test_publication_types_and_primary_type():
    article = make_article(1, pub_types=("Research Support, Non-U.S. Gov't", "Journal Article", "Review"))
    types = fetch.get_publication_types(article)
    assert types == ["Research Support, Non-U.S. Gov't", "Journal Article", "Review"]
    assert fetch.choose_primary_publication_type(types) == "Journal Article"
    assert fetch.choose_primary_publication_type(["English Abstract"]) == "English Abstract"
    assert fetch.choose_primary_publication_type([]) is None


@pytest.mark.parametrize("types, excluded", [
    (["Journal Article"], False),
    (["Review"], False),                       # reviews are kept and flagged, not excluded
    (["Editorial"], True),
    (["Journal Article", "Retracted Publication"], True),
])
def test_exclusion_by_publication_type(types, excluded):
    assert fetch.should_exclude_by_publication_type(types)[0] is excluded


def test_doi_from_elocation_then_article_ids():
    assert fetch.get_doi(make_article(1, doi="10.1/a", doi_in="elocation")) == "10.1/a"
    assert fetch.get_doi(make_article(1, doi="10.1/b", doi_in="articleid")) == "10.1/b"
    assert fetch.get_doi(make_article(1, doi=None)) is None


def test_publication_year_fallbacks():
    article = make_article(1, year="2020")
    assert fetch.get_publication_year(article) == "2020"
    # journals with issue dates given as text, e.g. "2023 Nov-Dec 01"
    issue = article["MedlineCitation"]["Article"]["Journal"]["JournalIssue"]
    issue["PubDate"] = {"MedlineDate": "2023 Nov-Dec 01"}
    assert fetch.get_publication_year(article) == "2023"
    # no issue year at all: electronic publication date
    issue["PubDate"] = {}
    article["MedlineCitation"]["Article"]["ArticleDate"] = [{"Year": "2024", "Month": "11", "Day": "18"}]
    assert fetch.get_publication_year(article) == "2024"
    article["MedlineCitation"]["Article"]["ArticleDate"] = []
    assert fetch.get_publication_year(article) is None
    assert fetch.extract_article_fields(article)["year"] is None


def test_clean_abstract():
    cleaned = fetch.clean_abstract("<i>BMI</i>\u00ae rose by 5% (95% CI) \u2013 caf\u00e9")
    assert cleaned == "BMI rose by 5% (95% CI) cafe"
    assert fetch.clean_abstract(None) == ""


# ---------- one record -> output fields ----------

def test_extract_article_fields():
    fields = fetch.extract_article_fields(make_article(
        7, pub_types=("Journal Article", "Systematic Review"),
        mesh=(("Child", "D002648"), ("Brain Neoplasms", "D001932"))))
    assert fields["review_paper"] == "Yes"
    assert fields["article_type"] == "Journal Article"
    assert fields["year"] == "2020"
    assert fields["doi"] == "10.1000/test.1"
    assert fields["author_institution"] == "(Smith J||Dept X, Univ A, City|Hosp B),(Lee K||Univ C)"
    assert fields["mesh_term_count"] == 2
    assert fields["mesh_terms"] == "('Child','D002648'),('Brain Neoplasms','D001932')"
    assert fields["_needs_mod"] is False


def test_paper_without_mesh_is_kept_and_marked():
    fields = fetch.extract_article_fields(make_article(8, mesh=()))
    assert fields["mesh_term_count"] == 0 and fields["mesh_terms"] is None
    assert fields["_needs_mod"] is True


def test_mesh_on_demand_is_off_by_default(monkeypatch):
    monkeypatch.setattr(fetch, "mesh_on_demand", lambda *a, **k: pytest.fail("MeSH on Demand must not be called"))
    rows = [{"pubmed_id": "8", "_needs_mod": True, "abstract": "text"}, {"pubmed_id": "9", "_needs_mod": False}]
    requested, retrieved, manual, no_mesh = fetch.run_mod_stage(rows, "unused.json", LOGGER)
    assert (requested, retrieved, manual, no_mesh) == (0, 0, [], ["8"])
    assert rows[0]["mesh_terms"] is None


# ---------- PubMed stage with screening ----------

def test_fetch_pubmed_rows_screens_records(monkeypatch):
    results = {
        "1": efetch_result(make_article(1)),
        "2": efetch_result(make_article(2, pub_types=("Editorial",))),
        "3": None,                                             # malformed efetch result
        "4": {"PubmedArticle": [], "PubmedBookArticle": [{}]},  # book record
    }
    monkeypatch.setattr(fetch, "fetch_info", lambda pmid, cps, logger=None: results[pmid])
    stats, skipped = fetch.new_fetch_stats(), []
    rows = fetch.fetch_pubmed_rows([("100", "1"), ("100", "2"), ("100", "3"), ("100", "4")], 10, LOGGER,
                                   stats, skipped, desc="test", cohort="CohortA")
    assert [r["pubmed_id"] for r in rows] == ["1"]
    assert rows[0]["associated_cohort_paper_id"] == "100"
    assert (stats["kept"], stats["excluded_type"], stats["errors"]) == (1, 1, 2)
    assert {s["pmid"] for s in skipped} == {"2", "3", "4"}
    assert all(s["cohort"] == "CohortA" for s in skipped)


# ---------- full cohort fetch ----------

CITATIONS = {"111": ["9001", "9002"], "222": ["9002", "9003"], "333": ["9001", "9004"]}


def fake_entrez(monkeypatch):
    calls = []

    def get_citations(pmid, cps, logger=None):
        calls.append(pmid)
        return CITATIONS[str(int(float(pmid)))]

    def fetch_info(pmid, cps, logger=None):
        return efetch_result(make_article(pmid, mesh=(("Child", "D002648"),)))

    monkeypatch.setattr(fetch, "get_citations", get_citations)
    monkeypatch.setattr(fetch, "fetch_info", fetch_info)
    return calls


def test_cohort_fetch_stores_each_citing_paper_once(tmp_path, monkeypatch, mesh_files):
    calls = fake_entrez(monkeypatch)
    workbook = tmp_path / "cohorts.xlsx"
    pd.DataFrame({"Cohort": ["CohortA", "CohortB"], "PMID 1": [111, 333], "PMID 2": [222, None]}).to_excel(
        workbook, sheet_name="Cohorts", index=False)

    fetch.fetch_data(str(workbook), "tester@example.org", None, mesh_files.dictionary)

    sheets = pd.read_excel(workbook, sheet_name=None, dtype=str)
    assert list(sheets) == ["Cohorts", "CohortA", "CohortB"]
    assert sheets["CohortA"]["pubmed_id"].tolist() == ["9001", "9002", "9003"]   # 9002 once
    assert sheets["CohortB"]["pubmed_id"].tolist() == ["9004"]                   # 9001 already in CohortA
    assert len(calls) == 3
    summary = sheets["Cohorts"].set_index("Cohort")
    assert summary.loc["CohortA", "total_citations"] == "3"
    assert (tmp_path / "cohorts_skipped_pmids.txt").exists()
    assert (tmp_path / "mesh_terms_with_keys_and_counts.xlsx").exists()


def test_pmid_list_fetch(tmp_path, monkeypatch, mesh_files):
    fake_entrez(monkeypatch)
    pmids = tmp_path / "my_papers.txt"
    pmids.write_text("PMID\n9001\n9002\n9001\nnot-a-pmid\n", encoding="utf-8")

    fetch.fetch_data_from_txt(str(pmids), "tester@example.org", None, mesh_files.dictionary)

    out = pd.read_excel(tmp_path / "my_papers_fetched_data.xlsx", dtype=str)
    assert out["pubmed_id"].tolist() == ["9001", "9002"]
    assert "associated_cohort_paper_id" not in out.columns


def test_entrez_identifies_tool_and_email():
    from Bio import Entrez
    assert fetch.setup_entrez("tester@example.org", None) == 3
    assert fetch.setup_entrez("tester@example.org", "key") == 10
    assert Entrez.email == "tester@example.org"
    assert Entrez.tool == "meshlytics"
