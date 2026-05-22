"""Tests for official Kindle.zip import functionality."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kindb.cli import app
from kindb.db import connect
from kindb.importer import import_kindle_json, import_official_zip
from tests.create_fixture import create_kindle_json
from tests.create_official_fixture import BASE, DATASETS, create_official_zip

runner = CliRunner()


def test_import_official_zip_populates_tables_and_views(imported_db: Path, tmp_path: Path) -> None:
    zip_path = create_official_zip(tmp_path / "Kindle.zip")

    result = import_official_zip(zip_path, imported_db)

    assert result["genres_count"] == 4
    assert result["series_count"] == 2
    assert result["author_ids_count"] == 3
    assert result["author_names_count"] == 4
    assert result["distinct_asin_count"] == 4

    con = connect(imported_db, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM import_metadata_official").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM book_genres WHERE asin = 'B000DEL001'").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM book_genres WHERE asin = 'Not Available'").fetchone()[0] == 0
        assert con.execute("SELECT genres FROM v_books WHERE asin = 'B000TEST01'").fetchone()[0] == [
            "Fantasy",
            "Fiction",
        ]
        assert con.execute(
            "SELECT series_title, series_asin, series_position FROM v_books WHERE asin = 'B000TEST01'"
        ).fetchone() == ("Series Alpha", "B07D4FP6XQ", 1)
        assert con.execute(
            "SELECT series_asin, series_position FROM v_books WHERE asin = 'B000TEST02'"
        ).fetchone() == (None, None)
        assert con.execute(
            "SELECT asin FROM v_book_genres WHERE asin = 'B000ZIP001'"
        ).fetchall() == []
        assert con.execute(
            """SELECT author_id, author_name, book_count
               FROM v_author_id_counts
               ORDER BY author_id"""
        ).fetchall() == [
            ("AID001", "Same Name", 1),
            ("AID002", "Translator", 1),
            ("AID003", "Same Name", 1),
        ]
        assert con.execute(
            """SELECT asin, author_order, author_id, author_name
               FROM v_book_authors_official
               WHERE asin = 'B000TEST04'"""
        ).fetchall() == [("B000TEST04", 1, None, "Name Only")]
    finally:
        con.close()


def test_import_official_zip_allows_empty_books_table(tmp_path: Path) -> None:
    db = tmp_path / "empty.duckdb"
    zip_path = create_official_zip(tmp_path / "Kindle.zip")

    import_official_zip(zip_path, db)

    con = connect(db, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM book_genres").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM v_book_genres").fetchone()[0] == 0
    finally:
        con.close()


def test_json_import_preserves_official_tables(imported_db: Path, tmp_path: Path) -> None:
    zip_path = create_official_zip(tmp_path / "Kindle.zip")
    import_official_zip(zip_path, imported_db)
    before = _official_counts(imported_db)

    json_path = create_kindle_json(tmp_path / "second.json", [
        {
            "title": "Second",
            "authors": "Author",
            "acquiredTime": 1704067200000,
            "readStatus": "UNKNOWN",
            "asin": "B000SECOND",
        }
    ])
    import_kindle_json(json_path, imported_db)

    assert _official_counts(imported_db) == before


def test_official_import_preserves_json_tables(imported_db: Path, tmp_path: Path) -> None:
    before = _book_count(imported_db)
    import_official_zip(create_official_zip(tmp_path / "Kindle.zip"), imported_db)
    assert _book_count(imported_db) == before


def test_import_official_zip_missing_required_file_errors(tmp_path: Path) -> None:
    zip_path = tmp_path / "Kindle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for key, dataset in DATASETS.items():
            if key == "author_names":
                continue
            zf.writestr(f"{BASE}/{dataset}/part-000.csv", "ASIN\nB000TEST01\n")

    with pytest.raises(ValueError, match="CustomerAuthorNameRelationship"):
        import_official_zip(zip_path, tmp_path / "db.duckdb")


def test_import_official_zip_header_mismatch_errors(tmp_path: Path) -> None:
    zip_path = create_official_zip(tmp_path / "Kindle.zip")
    broken = tmp_path / "Broken.zip"
    with zipfile.ZipFile(zip_path) as source, zipfile.ZipFile(broken, "w") as dest:
        for name in source.namelist():
            if "CustomerGenres_FE" in name:
                dest.writestr(name, "ASIN,Bad\nB000TEST01,Fiction\n")
            else:
                dest.writestr(name, source.read(name))

    with pytest.raises(ValueError, match="Genre"):
        import_official_zip(broken, tmp_path / "db.duckdb")


def test_import_official_zip_failure_preserves_existing(imported_db: Path, tmp_path: Path) -> None:
    good = create_official_zip(tmp_path / "Kindle.zip")
    import_official_zip(good, imported_db)
    before = _official_counts(imported_db)

    broken = tmp_path / "Broken.zip"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(broken, "w") as dest:
        for name in source.namelist():
            if "CustomerGenres_FE" in name:
                dest.writestr(name, "ASIN,Bad\nB000TEST01,Fiction\n")
            else:
                dest.writestr(name, source.read(name))

    with pytest.raises(ValueError):
        import_official_zip(broken, imported_db)
    assert _official_counts(imported_db) == before


def test_v_books_without_official_import_returns_empty_lists(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        row = con.execute(
            """SELECT genres, series_title, series_asin, series_position, author_ids, author_names_official
               FROM v_books
               WHERE asin = 'B000TEST01'"""
        ).fetchone()
        assert row == ([], None, None, None, [], [])
    finally:
        con.close()


def test_import_official_cli_and_status(imported_db: Path, tmp_path: Path) -> None:
    zip_path = create_official_zip(tmp_path / "Kindle.zip")

    result = runner.invoke(app, ["import-official", str(zip_path), "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "Official import complete" in result.output
    assert "Genres: 4" in result.output

    status = runner.invoke(app, ["status", "--db", str(imported_db)])
    assert status.exit_code == 0
    assert "Official import" in status.output
    assert "Author names (rows)" in status.output


def _official_counts(db_path: Path) -> tuple[int, int, int, int, int]:
    con = connect(db_path, read_only=True)
    try:
        return (
            con.execute("SELECT count(*) FROM book_genres").fetchone()[0],
            con.execute("SELECT count(*) FROM book_series").fetchone()[0],
            con.execute("SELECT count(*) FROM book_author_ids").fetchone()[0],
            con.execute("SELECT count(*) FROM book_author_names").fetchone()[0],
            con.execute("SELECT count(*) FROM import_metadata_official").fetchone()[0],
        )
    finally:
        con.close()


def _book_count(db_path: Path) -> int:
    con = connect(db_path, read_only=True)
    try:
        return con.execute("SELECT count(*) FROM books").fetchone()[0]
    finally:
        con.close()
