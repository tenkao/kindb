"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from kindb.importer import import_kindle_zip
from tests.create_fixture import create_kindle_zip


@pytest.fixture(autouse=True)
def _force_wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """CliRunner は non-tty 環境でデフォルト幅が狭く、rich が ASIN/書名を
    省略して出力する。テストは出力文字列を部分一致で検証するため、幅を
    広めに固定して切り詰めを抑止する。"""
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture
def kindle_zip(tmp_path: Path) -> Path:
    return create_kindle_zip(tmp_path / "Kindle.zip")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


@pytest.fixture
def imported_db(kindle_zip: Path, db_path: Path) -> Path:
    import_kindle_zip(kindle_zip, db_path)
    return db_path
