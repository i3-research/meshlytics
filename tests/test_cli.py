"""Command line: version, credential checks, and an end-to-end analyze run on a small synthetic dataset."""

import random

import pandas as pd
import pytest

import meshlytics
from meshlytics import cli
from pubmed_records import mesh_string

# real MeSH 2026 descriptors (the shipped dictionary is used here)
TERMS = [("Humans", "D006801"), ("Child", "D002648"), ("Pregnancy", "D011247"), ("Asthma", "D001249"),
         ("Obesity", "D009765"), ("Depression", "D003863"), ("Infant", "D007223"), ("Adolescent", "D000293")]
JOURNALS = ["Journal A", "Journal B", "Journal C"]
AUTHORS = ["Smith J", "Lee K", "Garcia M", "Chen L", "Okafor N"]


def run(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return exc.value.code


def test_version(capsys):
    assert run(["--version"]) == 0
    assert meshlytics.__version__ in capsys.readouterr().out


def test_fetch_requires_contact_email(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["fetch", "-f", "pmids.txt"]) == 1


def test_top_cited_requires_openalex_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NCBI_EMAIL", "tester@example.org")
    assert run(["top-cited", "-j", "lancet"]) == 1


def test_analyze_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["analyze", "-f", "does_not_exist.xlsx", "-hg"]) == 1


def synthetic_dataset(path, n=60, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        terms = rng.sample(TERMS, rng.randint(2, 5))
        authors = rng.sample(AUTHORS, rng.randint(1, 3))
        rows.append({
            "pubmed_id": 100000 + i,
            "review_paper": "Yes" if i % 10 == 0 else "No",
            "article_type": "Journal Article",
            "title": f"Paper {i}",
            "doi": f"10.1000/{i}",
            "author_institution": ",".join(f"({a}||Univ {a[0]})" for a in authors),
            "year": 2015 + i % 8,
            "journal_name": JOURNALS[i % 3],
            "abstract": "Text.",
            "mesh_term_count": len(terms),
            "mesh_terms": mesh_string(*terms),
        })
    pd.DataFrame(rows).to_excel(path, index=False)


def test_analyze_end_to_end(tmp_path, monkeypatch):
    data = tmp_path / "papers.xlsx"
    synthetic_dataset(data)
    monkeypatch.chdir(tmp_path)
    cli.main(["analyze", "-f", "papers", "-hg", "-co", "-mesh-tl", "-mesh-stacked", "-journals", "-authors",
              "--exclude-review-papers", "--year-range", "2015", "2022"])

    out_dirs = list(tmp_path.glob("Analysis_papers_*"))
    assert len(out_dirs) == 1
    files = {p.name for p in out_dirs[0].rglob("*") if p.is_file()}
    for name in ["full_histogram.png", "level1_co_occurrence_matrix.csv", "level1_cooccurrence_heatmap.png",
                 "Journal_Counts_All.xlsx", "Author_Counts_AllAuthors.xlsx", "analysis_summary.txt",
                 "top_5_MeSH_terms_trend_total_frequency.png"]:
        assert name in files, name

    journals = pd.read_excel(next(out_dirs[0].rglob("Journal_Counts_All.xlsx")))
    assert journals["paper_count"].sum() == 54            # 60 papers minus 6 reviews (--exclude-review-papers)
    level1 = pd.read_csv(next(out_dirs[0].rglob("level1_co_occurrence_matrix.csv")), index_col=0)
    assert level1.values.sum() > 0 and (level1.values == level1.values.T).all()
