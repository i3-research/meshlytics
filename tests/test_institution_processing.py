"""institution-processing: affiliation parsing and the OpenAI step (client mocked)."""

from types import SimpleNamespace

import pandas as pd
import pytest

pytest.importorskip("openai")   # optional extra: pip install "meshlytics[llm]"
from meshlytics import institution_processing as ip  # noqa: E402


def test_first_affiliation_per_author():
    df = pd.DataFrame({"author_institution": ["(Smith J||Dept X, Univ A|Hosp B),(Lee K||Univ C)", None]})
    df, institutions = ip.preprocess_author_institution(df)
    assert df["author_institution_clean"].tolist() == ["(Smith J||Dept X, Univ A),(Lee K||Univ C)", ""]
    assert sorted(institutions) == ["Dept X, Univ A", "Univ C"]


def test_short_names_with_acronym_fallback():
    df = pd.DataFrame({"author_institution_clean": ["(Smith J||Dept X, Univ A.),(Lee K||University of Calgary)", ""]})
    mapping = {"Dept X, Univ A": ("University A", "Univ A")}
    out = ip.update_author_institution_llm(df, mapping)
    assert out["author_institution_llm"].tolist() == ["(Smith J||Univ A),(Lee K||UOC)", ""]


class FakeClient:
    """Mimics client.chat.completions.create(...) of the OpenAI SDK."""

    def __init__(self, content):
        self.content = content
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_batch_standardisation_parses_fenced_json():
    client = FakeClient('```json\n[{"standardized": "Erasmus University", "short": "Erasmus U"},'
                        ' {"standardized": "Harvard University", "short": "Harvard"}]\n```')
    result = ip.batch_standardize_institutions(["Erasmus MC, Rotterdam.", "Harvard Medical School"], client, model="test-model")
    assert result == {"Erasmus MC, Rotterdam": ("Erasmus University", "Erasmus U"),
                      "Harvard Medical School": ("Harvard University", "Harvard")}
    assert client.calls[0]["model"] == "test-model"
    assert client.calls[0]["temperature"] == 0


def test_output_path():
    assert ip.derive_output_path("data/papers.xlsx").name == "papers_with_llm_standard_institution_names.xlsx"
    with pytest.raises(ValueError):
        ip.derive_output_path("papers.csv")
