"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from kindb.importer import import_kindle_zip
from tests.create_fixture import create_kindle_zip


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
