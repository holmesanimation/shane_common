"""Tests for shared preferences path resolution."""

from pathlib import Path

import pytest

from shane_common.preferences.paths import app_settings_path


def test_app_settings_path_uses_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    path = app_settings_path("trading_platform")

    assert path == tmp_path / "trading_platform" / "settings.yaml"


def test_app_settings_path_separates_app_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    trading_path = app_settings_path("trading_platform")
    purity_path = app_settings_path("purity_app")

    assert trading_path == tmp_path / "trading_platform" / "settings.yaml"
    assert purity_path == tmp_path / "purity_app" / "settings.yaml"
    assert trading_path != purity_path


def test_app_settings_path_requires_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        app_settings_path("trading_platform")