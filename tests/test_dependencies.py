"""Start-up dependency check: reports missing libraries, never installs."""

import pytest

from meshlytics import dependencies


def test_required_libraries_are_installed():
    assert dependencies.missing_libraries(dependencies.REQUIRED_LIBRARIES) == []


def test_missing_library_exits_with_install_hint(monkeypatch, capsys):
    monkeypatch.setattr(dependencies, "REQUIRED_LIBRARIES", {"not-a-package": "meshlytics_test_missing_module"})
    with pytest.raises(SystemExit) as exc:
        dependencies.ensure_required_libraries()
    assert exc.value.code == 1
    assert "pip install meshlytics" in capsys.readouterr().err
