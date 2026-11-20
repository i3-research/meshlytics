"""Figure titles state the period of the data shown."""

import pandas as pd
import pytest

from meshlytics import utils
from pubmed_records import mesh_string


@pytest.fixture
def titles(monkeypatch):
    seen = []
    monkeypatch.setattr(utils.ps, "set_title", lambda ax, title, subtitle=None: seen.append(title))
    return seen


def papers(years):
    terms = mesh_string(("Child", "D002648"), ("Brain", "D001921"))
    return pd.DataFrame({"year": years, "mesh_terms": [terms] * len(years)})


def test_wordcloud_title_names_the_years_of_the_data(tmp_path, titles):
    utils.generate_wordcloud(papers([2015, 2018, 2025, 2020, 2016]), str(tmp_path))
    assert titles == ["Most frequent MeSH terms, 2015-2025"]
    assert (tmp_path / "Wordcloud__freq_ge_5.png").exists()


def test_wordcloud_title_for_a_time_window(tmp_path, titles):
    utils.generate_wordcloud(papers([2015, 2016, 2017]), str(tmp_path), frequency_threshold=3, suffix="2015_2017")
    assert titles == ["Most frequent MeSH terms, 2015-2017"]


def test_wordcloud_title_single_year(tmp_path, titles):
    utils.generate_wordcloud(papers([2020] * 5), str(tmp_path))
    assert titles == ["Most frequent MeSH terms, 2020"]
