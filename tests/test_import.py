"""Tests for kindle.json import functionality."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest

from kindb.db import connect, ensure_schema
from kindb.importer import MAX_ACQUIRED_TIME_MS, import_kindle_json
from tests.create_fixture import create_kindle_json


def _book(asin: str, book_title: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "title": book_title,
        "authors": "Author One",
        "acquiredTime": 1704067200000,
        "readStatus": "UNKNOWN",
        "asin": asin,
        "productImage": "https://images.example.com/default.jpg",
    }
    row.update(overrides)
    return row


def _book_without(asin: str, book_title: str, missing: str) -> dict[str, object]:
    row = _book(asin, book_title)
    row.pop(missing, None)
    return row


def test_import_atomic_replace_swaps_content(tmp_path: Path) -> None:
    json_a = create_kindle_json(tmp_path / "a.json", [
        _book("B000AAA01", "First Import A"),
        _book("B000AAA02", "First Import B"),
    ])
    json_b = create_kindle_json(tmp_path / "b.json", [_book("B000BBB01", "Second Import Only")])
    db = tmp_path / "db.duckdb"

    import_kindle_json(json_a, db)
    import_kindle_json(json_b, db)

    con = connect(db, read_only=True)
    try:
        asins = [r[0] for r in con.execute("SELECT asin FROM books ORDER BY asin").fetchall()]
        assert asins == ["B000BBB01"]
        assert con.execute("SELECT source_path FROM import_metadata").fetchone()[0] == str(json_b.resolve())
        assert con.execute("SELECT count(*) FROM import_metadata").fetchone()[0] == 1
    finally:
        con.close()


def test_import_failure_preserves_existing(kindle_json: Path, db_path: Path, tmp_path: Path) -> None:
    import_kindle_json(kindle_json, db_path)
    before = _asins(db_path)
    bad_json = create_kindle_json(tmp_path / "bad.json", [_book("B000BAD01", "Bad", acquiredTime=-1)])

    with pytest.raises(ValueError):
        import_kindle_json(bad_json, db_path)

    assert _asins(db_path) == before


def test_first_import_creates_missing_parent_directory(kindle_json: Path, tmp_path: Path) -> None:
    # 初回は既定の ~/.kindb がまだない
    db_path = tmp_path / "db_root" / "store.duckdb"
    import_kindle_json(kindle_json, db_path)

    assert _asins(db_path)[0] == "B000TEST01"


def test_failed_first_import_leaves_no_database(tmp_path: Path) -> None:
    # 空の DB が残ると、読み取り系コマンドが「No database found.」ではなく 0 冊と表示してしまう
    bad_json = create_kindle_json(tmp_path / "bad.json", [_book("B000BAD01", "Bad", acquiredTime=-1)])
    db_dir = tmp_path / "db_root"
    db_path = db_dir / "store.duckdb"

    with pytest.raises(ValueError):
        import_kindle_json(bad_json, db_path)

    assert not db_dir.exists()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"not": "array"}, "root must be an array"),
        ([1], "each item must be an object"),
        ([_book("B000MISS01", "Missing", title="")], "missing or empty required key 'title'"),
        ([_book("B000MISS02", "Missing", authors="")], "missing or empty required key 'authors'"),
        ([_book("B000MISS03", "Missing", readStatus=None)], "missing or empty required key 'readStatus'"),
        ([_book_without("B000MISS04", "Missing", "acquiredTime")], "missing or empty required key 'acquiredTime'"),
        ([{**_book("B000MISS05", "Missing"), "asin": None}], "index 0: missing or empty required key 'asin'"),
        ([_book("B000TYPE01", "Type", title=123)], "'title' must be str"),
        ([_book("B000TYPE02", "Type", productImage={})], "'productImage' must be str or null"),
        ([_book("B000TIME01", "Time", acquiredTime="123")], "'acquiredTime' must be int"),
        ([_book("B000TIME02", "Time", acquiredTime=True)], "'acquiredTime' must be int"),
        ([_book("B000TIME03", "Time", acquiredTime=1.5)], "'acquiredTime' must be int"),
        ([_book("B000TIME04", "Time", acquiredTime=-1)], "0 <= acquiredTime"),
        ([_book("B000TIME05", "Time", acquiredTime=MAX_ACQUIRED_TIME_MS)], "0 <= acquiredTime"),
        ([_book("B000AUTH01", "Authors", authors=["A", "B"])], "'authors' must be str"),
    ],
)
def test_invalid_payloads_raise(tmp_path: Path, payload: object, message: str) -> None:
    json_path = tmp_path / "bad.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        import_kindle_json(json_path, tmp_path / "db.duckdb")


def test_duplicate_asin_raises(tmp_path: Path) -> None:
    json_path = create_kindle_json(tmp_path / "dup.json", [
        _book("B000DUP01", "One"),
        _book("B000DUP01", "Two"),
    ])
    with pytest.raises(ValueError, match="B000DUP01"):
        import_kindle_json(json_path, tmp_path / "db.duckdb")


def test_product_image_missing_null_and_empty_are_stored_as_null(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        rows = con.execute(
            "SELECT asin FROM books WHERE product_image_url IS NULL ORDER BY asin"
        ).fetchall()
        assert [r[0] for r in rows] == ["B000TEST03", "B000TEST04", "B000TEST05"]
    finally:
        con.close()


@pytest.fixture
def non_utc_timezone() -> Iterator[None]:
    # 実行環境が UTC だと、ローカル時刻で解釈するバグを見逃すため
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.mark.usefixtures("non_utc_timezone")
def test_acquired_time_is_stored_as_utc(tmp_path: Path) -> None:
    json_path = create_kindle_json(tmp_path / "time.json", [
        _book("B000TIME0", "Epoch", acquiredTime=0),
        _book("B000TIME1", "Millis", acquiredTime=1775589770148),
    ])
    db = tmp_path / "time.duckdb"
    import_kindle_json(json_path, db)

    con = connect(db, read_only=True)
    try:
        rows = con.execute("SELECT asin, acquired_at FROM v_books ORDER BY asin").fetchall()
    finally:
        con.close()
    assert rows == [
        ("B000TIME0", datetime(1970, 1, 1, 0, 0)),
        ("B000TIME1", datetime(2026, 4, 7, 19, 22, 50, 148000)),
    ]


def test_empty_array_success(tmp_path: Path) -> None:
    json_path = create_kindle_json(tmp_path / "empty.json", [])
    db = tmp_path / "empty.duckdb"
    result = import_kindle_json(json_path, db)
    assert result["books_count"] == 0

    con = connect(db, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
        assert con.execute("SELECT books_count, source_type FROM import_metadata").fetchone() == (0, "kindle_json")
    finally:
        con.close()


def test_metadata_source_path_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_book("B000REL01", "Relative")]
    json_path = create_kindle_json(tmp_path / "relative.json", rows)
    monkeypatch.chdir(tmp_path)

    import_kindle_json(Path("relative.json"), Path("relative.duckdb"))

    con = connect(tmp_path / "relative.duckdb", read_only=True)
    try:
        source_path, source_type = con.execute("SELECT source_path, source_type FROM import_metadata").fetchone()
        assert source_path == str(json_path.resolve())
        assert source_type == "kindle_json"
    finally:
        con.close()


def test_schema_tables_and_views(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert tables == {
            "book_author_ids",
            "book_author_names",
            "book_authors",
            "book_genres",
            "book_series",
            "books",
            "import_metadata",
            "import_metadata_official",
            "schema_meta",
            "v_author_counts",
            "v_author_id_counts",
            "v_book_authors_official",
            "v_book_genres",
            "v_book_series",
            "v_books",
            "v_genre_counts",
            "v_series_counts",
        }
    finally:
        con.close()


def test_v02_database_is_migrated_and_importable(kindle_json: Path, tmp_path: Path) -> None:
    # 旧版の DB で読み取り系コマンドと import が動くこと。列を足して移行を書き忘れると import が落ちる
    db = tmp_path / "v02.duckdb"
    con = connect(db)
    try:
        con.execute(
            """CREATE TABLE books (
                asin VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                authors_text VARCHAR NOT NULL,
                acquired_at TIMESTAMP NOT NULL,
                read_status VARCHAR NOT NULL,
                product_image_url VARCHAR,
                imported_at TIMESTAMP NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE book_authors (
                asin VARCHAR NOT NULL,
                author_name VARCHAR NOT NULL,
                author_order INTEGER NOT NULL,
                PRIMARY KEY (asin, author_order)
            )"""
        )
        con.execute(
            """CREATE TABLE import_metadata (
                source_path VARCHAR,
                source_type VARCHAR,
                books_count INTEGER,
                imported_at TIMESTAMP
            )"""
        )
    finally:
        con.close()

    ensure_schema(db)

    con = connect(db, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        assert "book_genres" in tables
        assert "v_author_id_counts" in tables
        row = con.execute(
            """SELECT genres, series_title, author_ids, author_names_official
               FROM v_books
               LIMIT 0"""
        )
        assert [desc[0] for desc in row.description] == [
            "genres",
            "series_title",
            "author_ids",
            "author_names_official",
        ]
    finally:
        con.close()

    import_kindle_json(kindle_json, db)

    con = connect(db, read_only=True)
    try:
        row = con.execute("SELECT asin, authors, genres FROM v_books ORDER BY asin LIMIT 1").fetchone()
    finally:
        con.close()
    assert row == ("B000TEST01", ["山田太郎", "佐藤花子"], [])


def test_ensure_schema_recreates_views_when_schema_is_stale(imported_db: Path) -> None:
    con = connect(imported_db)
    try:
        con.execute("CREATE OR REPLACE VIEW v_genre_counts AS SELECT 1 AS x")
        con.execute("UPDATE schema_meta SET schema_hash = 'stale'")
    finally:
        con.close()

    ensure_schema(imported_db)

    con = connect(imported_db, read_only=True)
    try:
        cols = [desc[0] for desc in con.execute("SELECT * FROM v_genre_counts LIMIT 0").description]
        assert cols == ["genre", "book_count"]
    finally:
        con.close()


def test_empty_author_elements_are_skipped(tmp_path: Path) -> None:
    json_path = create_kindle_json(tmp_path / "authors.json", [_book("B000AUTH2", "Authors", authors="A, , B")])
    db = tmp_path / "authors.duckdb"
    import_kindle_json(json_path, db)
    con = connect(db, read_only=True)
    try:
        rows = con.execute("SELECT author_name, author_order FROM book_authors ORDER BY author_order").fetchall()
        assert rows == [("A", 1), ("B", 2)]
    finally:
        con.close()


def test_v_books_one_row_per_asin_and_authors_order(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM v_books").fetchone()[0] == 5
        authors = con.execute("SELECT authors FROM v_books WHERE asin = 'B000TEST02'").fetchone()[0]
        assert authors == ["John Smith", "Jane Doe", "Alice Brown"]
        authors_text = con.execute("SELECT authors_text FROM v_books WHERE asin = 'B000TEST02'").fetchone()[0]
        assert authors_text == "John Smith, Jane Doe, Alice Brown"
    finally:
        con.close()


def test_v_author_counts_counts_books_per_author(imported_db: Path) -> None:
    # ビュー定義の ORDER BY は順序を保証しないので、呼び出し側で並べる(docs/spec.md)
    con = connect(imported_db, read_only=True)
    try:
        rows = con.execute(
            "SELECT author_name, book_count FROM v_author_counts ORDER BY book_count DESC, author_name"
        ).fetchall()
        assert rows[:2] == [("山田太郎", 2), ("Alice Brown", 1)]
    finally:
        con.close()


def test_stale_wal_removed_on_import(kindle_json: Path, tmp_path: Path) -> None:
    db = tmp_path / "store.duckdb"
    wal = Path(str(db) + ".wal")
    wal.write_text("fake-wal")
    import_kindle_json(kindle_json, db)
    assert db.exists()
    assert not wal.exists()


def _asins(db_path: Path) -> list[str]:
    con = connect(db_path, read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT asin FROM books ORDER BY asin").fetchall()]
    finally:
        con.close()
