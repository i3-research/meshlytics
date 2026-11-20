"""top-cited: journal lookup, PubMed screening with backfill and citation counts (OpenAlex/E-utilities mocked)."""

import json
from datetime import datetime

import pytest

from meshlytics import top_cited
from pubmed_records import make_article

JOURNALS = [
    {"JournalTitle": "Lancet (London, England)", "MedAbbr": "Lancet", "IsoAbbr": "Lancet",
     "ISSN (Print)": "0140-6736", "ISSN (Online)": "1474-547X", "NlmId": "2985213R"},
    {"JournalTitle": "The Lancet. Oncology", "MedAbbr": "Lancet Oncol", "IsoAbbr": "Lancet Oncol.",
     "ISSN (Print)": "1470-2045", "ISSN (Online)": "1474-5488", "NlmId": "100957246"},
    {"JournalTitle": "Nature neuroscience", "MedAbbr": "Nat Neurosci", "IsoAbbr": "Nat. Neurosci.",
     "ISSN (Print)": "1097-6256", "ISSN (Online)": "1546-1726", "NlmId": "9809671"},
]
JOURNAL_ROW = {"NlmId": "9809671", "MedAbbr": "Nat Neurosci"}


@pytest.fixture
def journals(tmp_path):
    path = tmp_path / "journals.json"
    path.write_text(json.dumps(JOURNALS), encoding="utf-8")
    return top_cited.load_pubmed_journals(str(path))


def test_normalisation():
    assert top_cited.normalize_journal_name("The Journal of Neuroscience & Sleep") == "journal of neuroscience and sleep"
    assert top_cited.normalize_issn("1546-1726") == "1546-1726"
    assert top_cited.normalize_issn("1474547x") == "1474-547X"
    assert top_cited.normalize_issn("lancet") == ""


def test_exact_match_listed_before_journal_family(journals):
    hits = top_cited.find_journal_matches("lancet", journals)
    assert hits.iloc[0]["MedAbbr"] == "Lancet" and hits.iloc[0]["match_type"] == "exact"
    assert "Lancet Oncol" in hits["MedAbbr"].tolist()


def test_spelling_error_and_issn(journals):
    assert top_cited.find_journal_matches("nature neurosience", journals).iloc[0]["MedAbbr"] == "Nat Neurosci"
    hit = top_cited.find_journal_matches("1546-1726", journals)
    assert hit["MedAbbr"].tolist() == ["Nat Neurosci"] and hit.iloc[0]["match_type"] == "ISSN"
    assert top_cited.find_journal_matches("zzzz qqqq", journals).empty


def test_extract_pmid():
    assert top_cited.extract_pmid("https://pubmed.ncbi.nlm.nih.gov/33885740") == "33885740"
    assert top_cited.extract_pmid("123") == "123"
    assert top_cited.extract_pmid(None) is None
    assert top_cited.extract_pmid("not a pmid") is None


def test_year_window():
    assert top_cited.resolve_year_window(year_range=(2016, 2025)) == (2016, 2025)
    last = datetime.now().year - 1
    assert top_cited.resolve_year_window(years_back=10) == (last - 9, last)
    with pytest.raises(ValueError):
        top_cited.resolve_year_window(year_range=(2025, 2016))


def test_screening_rules():
    keep = top_cited.screen_pubmed_record(make_article(1, nlm_id="9809671"), JOURNAL_ROW)
    assert keep["keep"] is True
    review = make_article(2, pub_types=("Review",), nlm_id="9809671")
    assert top_cited.screen_pubmed_record(review, JOURNAL_ROW)["reason"] == "Review paper"
    assert top_cited.screen_pubmed_record(review, JOURNAL_ROW, include_reviews=True)["keep"] is True
    other = top_cited.screen_pubmed_record(make_article(3, nlm_id="1234567"), JOURNAL_ROW)
    assert other["keep"] is False and other["reason"].startswith("Journal mismatch")
    erratum = top_cited.screen_pubmed_record(make_article(4, pub_types=("Published Erratum",), nlm_id="9809671"), JOURNAL_ROW)
    assert erratum["reason"].startswith("Excluded publication type")


def fake_batch(monkeypatch):
    def fetch_batch(pmids, cps):
        articles = {}
        for p in pmids:
            if p == "3":
                continue                                   # not returned by PubMed
            types = ("Review",) if p == "2" else ("Journal Article",)
            articles[p] = make_article(p, pub_types=types, nlm_id="9809671")
        return articles
    monkeypatch.setattr(top_cited, "fetch_pubmed_records_batch", fetch_batch)


def test_excluded_papers_are_backfilled_from_the_ranking(monkeypatch):
    fake_batch(monkeypatch)
    candidates = [{"openalex_rank": r, "pmid": str(r), "cited_by_count": 100 - r} for r in range(1, 7)]
    selected, log, exhausted = top_cited.select_top_n_pubmed(candidates, 3, JOURNAL_ROW, 10, batch_size=2)
    assert [s["pmid"] for s in selected] == ["1", "4", "5"]
    assert [s["top_cited_rank"] for s in selected] == [1, 2, 3]
    status = {e["pmid"]: (e["status"], e["reason"]) for e in log}
    assert status["2"] == ("excluded", "Review paper")
    assert status["3"] == ("excluded", "Not returned by PubMed efetch")
    assert status["6"][0] == "not needed"
    assert exhausted is False


def test_ranking_exhausted(monkeypatch):
    fake_batch(monkeypatch)
    candidates = [{"openalex_rank": r, "pmid": str(r)} for r in (1, 2)]
    selected, _, exhausted = top_cited.select_top_n_pubmed(candidates, 5, JOURNAL_ROW, 10)
    assert [s["pmid"] for s in selected] == ["1"] and exhausted is True


class _Handle:
    def close(self):
        pass


def test_pubmed_cited_in_counts(monkeypatch):
    record = [
        {"IdList": ["1"], "LinkSetDb": [{"LinkName": "pubmed_pubmed_citedin", "Link": [{"Id": "a"}, {"Id": "b"}]}]},
        {"IdList": ["2"], "LinkSetDb": []},
    ]
    monkeypatch.setattr(top_cited.Entrez, "elink", lambda **kwargs: _Handle())
    monkeypatch.setattr(top_cited.Entrez, "read", lambda handle: record)
    assert top_cited.get_pubmed_cited_in_counts(["1", "2", "3"], 10) == {"1": 2, "2": 0, "3": None}


def test_pubmed_cited_in_counts_failed_request(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("HTTP 500")
    monkeypatch.setattr(top_cited.Entrez, "elink", fail)
    assert top_cited.get_pubmed_cited_in_counts(["1", "2"], 10) == {"1": None, "2": None}
