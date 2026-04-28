"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from kindb.importer import import_kindle_json
from tests.create_fixture import create_kindle_json


@pytest.fixture(autouse=True)
def _force_wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "240")


@pytest.fixture
def kindle_json(tmp_path: Path) -> Path:
    return create_kindle_json(tmp_path / "kindle.json")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


@pytest.fixture
def imported_db(kindle_json: Path, db_path: Path) -> Path:
    import_kindle_json(kindle_json, db_path)
    return db_path
