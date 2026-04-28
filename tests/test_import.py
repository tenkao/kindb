"""Tests for kindle.json import functionality."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from kindb.db import connect
from kindb.importer import MAX_ACQUIRED_TIME_MS, _acquired_time_to_datetime, import_kindle_json
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


def test_import_creates_db(kindle_json: Path, db_path: Path) -> None:
    result = import_kindle_json(kindle_json, db_path)
    assert db_path.exists()
    assert result["books_count"] == 5
    assert result["source_path"] == str(kindle_json.resolve())


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


def test_tmp_dir_placed_in_db_parent(
    kindle_json: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile as _tempfile

    db_dir = tmp_path / "db_root"
    db_path = db_dir / "store.duckdb"
    captured: dict[str, str | None] = {"dir": None}
    original = _tempfile.mkdtemp

    def _spy(*, dir: str | None = None, **kwargs: object) -> str:
        captured["dir"] = dir
        return original(dir=dir, **kwargs)

    monkeypatch.setattr(_tempfile, "mkdtemp", _spy)
    import_kindle_json(kindle_json, db_path)

    assert captured["dir"] is not None
    assert Path(captured["dir"]) == db_dir


def test_failed_import_cleans_up_tmp_dir(tmp_path: Path) -> None:
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


def test_invalid_json_raises(tmp_path: Path) -> None:
    json_path = tmp_path / "bad.json"
    json_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        import_kindle_json(json_path, tmp_path / "db.duckdb")


def test_duplicate_asin_raises(tmp_path: Path) -> None:
    json_path = create_kindle_json(tmp_path / "dup.json", [
        _book("B000DUP01", "One"),
        _book("B000DUP01", "Two"),
    ])
    with pytest.raises(ValueError, match="B000DUP01"):
        import_kindle_json(json_path, tmp_path / "db.duckdb")


def test_unknown_keys_warn_and_continue(tmp_path: Path) -> None:
    warnings: list[str] = []
    json_path = create_kindle_json(tmp_path / "unknown.json", [
        _book("B000WARN1", "Warn", extraField="ignored"),
    ])
    result = import_kindle_json(json_path, tmp_path / "db.duckdb", warn=warnings.append)
    assert result["books_count"] == 1
    assert warnings == ["ASIN B000WARN1: ignoring unknown keys: extraField"]


def test_product_image_missing_null_and_empty_are_stored_as_null(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        rows = con.execute(
            "SELECT asin FROM books WHERE product_image_url IS NULL ORDER BY asin"
        ).fetchall()
        assert [r[0] for r in rows] == ["B000TEST03", "B000TEST04", "B000TEST05"]
    finally:
        con.close()


def test_acquired_time_converts_to_utc_naive() -> None:
    assert _acquired_time_to_datetime(0) == datetime(1970, 1, 1, 0, 0)
    assert _acquired_time_to_datetime(1775589770148) == datetime(2026, 4, 7, 19, 22, 50, 148000)


def test_imported_acquired_time(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        dt = con.execute("SELECT acquired_at FROM books WHERE asin = 'B000TEST01'").fetchone()[0]
        assert dt == datetime(2024, 1, 15, 10, 30)
    finally:
        con.close()


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
        assert tables == {"book_authors", "books", "import_metadata", "v_author_counts", "v_books"}
    finally:
        con.close()


def test_columns(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        book_cols = [desc[0] for desc in con.execute("SELECT * FROM books LIMIT 0").description]
        author_cols = [desc[0] for desc in con.execute("SELECT * FROM book_authors LIMIT 0").description]
        assert book_cols == [
            "asin",
            "title",
            "authors_text",
            "acquired_at",
            "read_status",
            "product_image_url",
            "imported_at",
        ]
        assert author_cols == ["asin", "author_name", "author_order"]
    finally:
        con.close()


def test_authors_split_and_order(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        rows = con.execute(
            "SELECT author_name, author_order FROM book_authors WHERE asin = 'B000TEST01' ORDER BY author_order"
        ).fetchall()
        assert rows == [("山田太郎", 1), ("佐藤花子", 2)]
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


def test_v_author_counts_order(imported_db: Path) -> None:
    con = connect(imported_db, read_only=True)
    try:
        rows = con.execute("SELECT author_name, book_count FROM v_author_counts").fetchall()
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


def test_nonexistent_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        import_kindle_json(tmp_path / "missing.json", tmp_path / "db.duckdb")


def _asins(db_path: Path) -> list[str]:
    con = connect(db_path, read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT asin FROM books ORDER BY asin").fetchall()]
    finally:
        con.close()
