"""Tests for import functionality."""

from __future__ import annotations

import zipfile
from pathlib import Path

import duckdb
import pytest

from kindb.db import connect, wal_path
from kindb.importer import _parse_timestamp, import_kindle_zip


class TestImportBasic:
    def test_import_creates_db(self, kindle_zip: Path, db_path: Path) -> None:
        result = import_kindle_zip(kindle_zip, db_path)
        assert db_path.exists()
        assert result["books_count"] == 3
        assert result["reading_sessions_count"] == 3

    def test_import_atomic_replace(self, kindle_zip: Path, db_path: Path) -> None:
        """Successful import replaces existing DB."""
        import_kindle_zip(kindle_zip, db_path)
        import_kindle_zip(kindle_zip, db_path)
        assert db_path.exists()

    def test_import_atomic_replace_swaps_content(self, tmp_path: Path) -> None:
        """2 回目の import で内容が差し替わる(古いデータが残らない)。"""
        zip_a = _write_books_only_zip_multi(tmp_path / "a.zip", [
            ("B000AAA01", "First Import A"),
            ("B000AAA02", "First Import B"),
        ])
        zip_b = _write_books_only_zip_multi(tmp_path / "b.zip", [
            ("B000BBB01", "Second Import Only"),
        ])
        db = tmp_path / "db.duckdb"

        import_kindle_zip(zip_a, db)
        import_kindle_zip(zip_b, db)

        con = connect(db, read_only=True)
        try:
            asins = [r[0] for r in con.execute("SELECT asin FROM books ORDER BY asin").fetchall()]
            assert asins == ["B000BBB01"], "古い ASIN が残らず、2 回目の内容だけになる"
            source = con.execute("SELECT source_path FROM import_metadata").fetchone()[0]
            assert source == str(zip_b)
            # シングルトンメタデータも維持
            assert con.execute("SELECT count(*) FROM import_metadata").fetchone()[0] == 1
        finally:
            con.close()

    def test_import_failure_preserves_existing(self, kindle_zip: Path, db_path: Path, tmp_path: Path) -> None:
        """Failed import leaves existing DB intact."""
        import_kindle_zip(kindle_zip, db_path)
        original_size = db_path.stat().st_size

        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"not a zip")

        with pytest.raises(ValueError):
            import_kindle_zip(bad_zip, db_path)

        assert db_path.exists()
        assert db_path.stat().st_size == original_size


class TestBooksFilter:
    def test_only_item_owner_not_deleted(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        asins = [r[0] for r in con.execute("SELECT asin FROM books ORDER BY asin").fetchall()]
        assert asins == ["B000TEST01", "B000TEST02", "B000TEST03"]

    def test_sample_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM books WHERE asin = 'B000SAMPLE'").fetchone()[0]
        assert count == 0

    def test_recommendation_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM books WHERE asin = 'B000RECOM1'").fetchone()[0]
        assert count == 0

    def test_deleted_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM books WHERE asin = 'B000DELETE'").fetchone()[0]
        assert count == 0

    def test_empty_asin_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM books WHERE asin = ''").fetchone()[0]
        assert count == 0


class TestBookColumns:
    def test_product_name(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        name = con.execute("SELECT product_name FROM books WHERE asin = 'B000TEST01'").fetchone()[0]
        assert name == "テストの本"

    def test_date_parsed(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        dt = con.execute(
            "SELECT relationship_creation_date FROM books WHERE asin = 'B000TEST01'"
        ).fetchone()[0]
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1

    def test_series_fields(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        row = con.execute(
            "SELECT series_title, series_author, position_in_collection FROM books WHERE asin = 'B000TEST01'"
        ).fetchone()
        assert row[0] == "テストシリーズ"
        assert row[1] == "山田太郎"
        assert row[2] == "1"

    def test_no_deleted_by_customer_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM books LIMIT 0").description]
        assert "deleted_by_customer" not in columns


class TestAuthors:
    def test_authors_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM book_authors").fetchone()[0]
        assert count == 4  # 2 for TEST01, 1 for TEST02, 1 for TEST03 (empty ASIN excluded)

    def test_multiple_authors(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        authors = [
            r[0]
            for r in con.execute(
                "SELECT author_name FROM book_authors WHERE asin = 'B000TEST01' ORDER BY author_name"
            ).fetchall()
        ]
        assert authors == ["佐藤花子", "山田太郎"]

    def test_no_product_name_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM book_authors LIMIT 0").description]
        assert "product_name" not in columns


class TestGenres:
    def test_genres_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM book_genres").fetchone()[0]
        assert count == 4

    def test_multiple_genres(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        genres = [
            r[0]
            for r in con.execute(
                "SELECT genre FROM book_genres WHERE asin = 'B000TEST01' ORDER BY genre"
            ).fetchall()
        ]
        assert genres == ["コミック", "文学・評論"]

    def test_no_product_name_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM book_genres LIMIT 0").description]
        assert "product_name" not in columns


class TestImages:
    def test_images_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM book_images").fetchone()[0]
        assert count == 3  # 2 for TEST01, 1 for TEST02 (TEST03 has empty URL)

    def test_empty_url_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM book_images WHERE asin = 'B000TEST03'").fetchone()[0]
        assert count == 0

    def test_no_product_name_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM book_images LIMIT 0").description]
        assert "product_name" not in columns


class TestReadingSessions:
    def test_sessions_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM reading_sessions").fetchone()[0]
        assert count == 3

    def test_session_values(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        row = con.execute(
            """SELECT total_reading_millis, number_of_page_flips
               FROM reading_sessions WHERE asin = 'B000TEST01'
               ORDER BY start_timestamp LIMIT 1"""
        ).fetchone()
        assert row[0] == 1800000
        assert row[1] == 45

    def test_no_product_name_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM reading_sessions LIMIT 0").description]
        assert "product_name" not in columns


class TestReadingInsightSessions:
    def test_insight_sessions_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM reading_insight_sessions").fetchone()[0]
        assert count == 1

    def test_has_product_name(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        name = con.execute("SELECT product_name FROM reading_insight_sessions LIMIT 1").fetchone()[0]
        assert name == "テストの本"


class TestPersonalDocuments:
    def test_docs_imported(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM personal_documents").fetchone()[0]
        assert count == 1  # deleted doc excluded

    def test_deleted_excluded(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM personal_documents WHERE document_id = 'DOC002'").fetchone()[0]
        assert count == 0

    def test_no_has_been_deleted_column(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        columns = [desc[0] for desc in con.execute("SELECT * FROM personal_documents LIMIT 0").description]
        assert "has_been_deleted" not in columns


class TestImportMetadata:
    def test_metadata_created(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        row = con.execute("SELECT * FROM import_metadata").fetchone()
        assert row is not None
        assert row[4] == 3  # books_count
        assert row[5] == 3  # reading_sessions_count

    def test_metadata_singleton_after_single_import(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM import_metadata").fetchone()[0]
        assert count == 1
        import_id = con.execute("SELECT import_id FROM import_metadata").fetchone()[0]
        assert import_id == 1

    def test_metadata_singleton_after_reimport(self, kindle_zip: Path, db_path: Path) -> None:
        """2 回連続 import でも 1 行しか残らない。"""
        import_kindle_zip(kindle_zip, db_path)
        import_kindle_zip(kindle_zip, db_path)
        con = connect(db_path, read_only=True)
        count = con.execute("SELECT count(*) FROM import_metadata").fetchone()[0]
        assert count == 1

    def test_metadata_has_primary_key_on_import_id(self, imported_db: Path) -> None:
        """スキーマレベルで import_id が PRIMARY KEY である。"""
        con = connect(imported_db, read_only=True)
        rows = con.execute("PRAGMA table_info('import_metadata')").fetchall()
        # DuckDB PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        pk_cols = [r[1] for r in rows if r[5]]
        assert pk_cols == ["import_id"]

    def test_metadata_pk_rejects_duplicate_insert(self, imported_db: Path) -> None:
        """PK 重複する直接 INSERT は DuckDB 側で拒否される。"""
        con = connect(imported_db)
        try:
            with pytest.raises(duckdb.ConstraintException):
                con.execute(
                    "INSERT INTO import_metadata (import_id, source_path) VALUES (1, 'dup')"
                )
        finally:
            con.close()


class TestViews:
    def test_v_books_one_row_per_book(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM v_books").fetchone()[0]
        assert count == 3

    def test_v_books_authors_array(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        authors = con.execute("SELECT authors FROM v_books WHERE asin = 'B000TEST01'").fetchone()[0]
        assert sorted(authors) == ["佐藤花子", "山田太郎"]

    def test_v_books_genres_array(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        genres = con.execute("SELECT genres FROM v_books WHERE asin = 'B000TEST01'").fetchone()[0]
        assert sorted(genres) == ["コミック", "文学・評論"]

    def test_v_books_image_url_deterministic(self, imported_db: Path) -> None:
        """min(image_url) is selected for deterministic choice."""
        con = connect(imported_db, read_only=True)
        url = con.execute("SELECT image_url FROM v_books WHERE asin = 'B000TEST01'").fetchone()[0]
        assert url == "https://images.example.com/B000TEST01_a.jpg"

    def test_v_reading_summary_from_reading_sessions_only(self, imported_db: Path) -> None:
        """v_reading_summary uses reading_sessions, not reading_insight_sessions."""
        con = connect(imported_db, read_only=True)
        row = con.execute(
            "SELECT reading_session_count, total_reading_millis, total_page_flips "
            "FROM v_reading_summary WHERE asin = 'B000TEST01'"
        ).fetchone()
        assert row[0] == 2  # 2 sessions from reading_sessions
        assert row[1] == 4500000  # 1800000 + 2700000
        assert row[2] == 105  # 45 + 60

    def test_v_books_with_reading_one_row_per_book(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        count = con.execute("SELECT count(*) FROM v_books_with_reading").fetchone()[0]
        assert count == 3

    def test_v_books_with_reading_null_for_unread(self, imported_db: Path) -> None:
        con = connect(imported_db, read_only=True)
        row = con.execute(
            "SELECT reading_session_count FROM v_books_with_reading WHERE asin = 'B000TEST03'"
        ).fetchone()
        assert row[0] is None


class TestMissingFiles:
    def test_missing_books_csv_raises(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("dummy.txt", "dummy")

        with pytest.raises(FileNotFoundError, match="CustomerRelationshipIndex_FE.csv"):
            import_kindle_zip(zip_path, tmp_path / "test.duckdb")

    def test_missing_optional_files_ok(self, tmp_path: Path) -> None:
        """Optional CSVs missing => empty tables, no error."""
        import csv
        import io

        zip_path = tmp_path / "minimal.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=[
                    "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
                    "Series Title", "Series Author", "Position In Collection", "Marketplace",
                    "Relationship Creation Date", "Resource Type", "Ownership Type", "Deleted By Customer",
                ],
            )
            writer.writeheader()
            writer.writerow({
                "ASIN": "B000MINI01",
                "Product Name": "Minimal",
                "Sortable Title": "Minimal",
                "Sortable Author Name": "Author",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
            zf.writestr("Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv", buf.getvalue())

        db_path = tmp_path / "minimal.duckdb"
        result = import_kindle_zip(zip_path, db_path)
        assert result["books_count"] == 1
        assert result["reading_sessions_count"] == 0

        con = connect(db_path, read_only=True)
        assert con.execute("SELECT count(*) FROM book_authors").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM book_genres").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM reading_sessions").fetchone()[0] == 0
        con.close()


class TestParseTimestamp:
    def test_z_suffix_parses_as_utc_naive(self) -> None:
        dt = _parse_timestamp("2024-01-15T10:30:00.000Z")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.year == 2024 and dt.month == 1 and dt.day == 15
        assert dt.hour == 10 and dt.minute == 30

    def test_z_suffix_without_microseconds(self) -> None:
        dt = _parse_timestamp("2024-03-20T08:00:00Z")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 8

    def test_offset_converted_to_utc_naive(self) -> None:
        """+09:00 の 19:30 は UTC 10:30 に正規化される。"""
        dt = _parse_timestamp("2024-01-15T19:30:00+09:00")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 10 and dt.minute == 30

    def test_offset_with_microseconds(self) -> None:
        dt = _parse_timestamp("2024-01-15T19:30:00.500+09:00")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 10 and dt.microsecond == 500000

    def test_naive_space_separated_kept_as_naive(self) -> None:
        dt = _parse_timestamp("2024-01-15 10:30:00")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 10

    def test_date_only(self) -> None:
        dt = _parse_timestamp("2024-01-15")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 0

    def test_empty_returns_none(self) -> None:
        assert _parse_timestamp("") is None
        assert _parse_timestamp("   ") is None
        assert _parse_timestamp(None) is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_timestamp("not-a-date") is None
        assert _parse_timestamp("2024/01/15") is None


class TestWalPath:
    def test_wal_path_duckdb_ext(self) -> None:
        assert wal_path(Path("/tmp/foo.duckdb")) == Path("/tmp/foo.duckdb.wal")

    def test_wal_path_db_ext(self) -> None:
        assert wal_path(Path("/tmp/foo.db")) == Path("/tmp/foo.db.wal")

    def test_wal_path_no_ext(self) -> None:
        assert wal_path(Path("/tmp/foo")) == Path("/tmp/foo.wal")

    def test_wal_path_accepts_str(self) -> None:
        assert wal_path("/tmp/foo.db") == Path("/tmp/foo.db.wal")


class TestImportStaleWalCleanup:
    def test_stale_wal_removed_on_import(self, kindle_zip: Path, tmp_path: Path) -> None:
        """置換前から残っていた stale WAL は import 成功後に削除される。"""
        db = tmp_path / "store.duckdb"
        stale = Path(str(db) + ".wal")
        stale.write_text("orphaned-wal-from-previous-run")
        import_kindle_zip(kindle_zip, db)
        assert db.exists()
        assert not stale.exists(), "stale WAL should be removed after atomic replace"

    def test_stale_wal_removed_with_db_ext(self, kindle_zip: Path, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        stale = Path(str(db) + ".wal")
        stale.write_text("orphaned")
        import_kindle_zip(kindle_zip, db)
        assert db.exists()
        assert not stale.exists()


class TestInvalidInput:
    def test_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_kindle_zip(tmp_path / "nonexistent.zip", tmp_path / "test.duckdb")

    def test_invalid_zip(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip file")
        with pytest.raises(ValueError, match="Not a valid zip"):
            import_kindle_zip(bad, tmp_path / "test.duckdb")


class TestUnparseableDate:
    def test_invalid_relationship_date_becomes_null(self, tmp_path: Path) -> None:
        """日付パース不能な Relationship Creation Date は NULL で保存され、
        他の列は通常通り取り込まれる(import 全体は失敗しない)。"""
        import csv
        import io
        import zipfile

        zip_path = tmp_path / "bad_date.zip"
        fields = [
            "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
            "Series Title", "Series Author", "Position In Collection", "Marketplace",
            "Relationship Creation Date", "Resource Type", "Ownership Type",
            "Deleted By Customer",
        ]
        with zipfile.ZipFile(zip_path, "w") as zf:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fields)
            w.writeheader()
            w.writerow({
                "ASIN": "B000BAD01", "Product Name": "Broken Date Book",
                "Sortable Title": "Broken", "Sortable Author Name": "A",
                "Series Title": "", "Series Author": "", "Position In Collection": "",
                "Marketplace": "JP", "Relationship Creation Date": "not-a-date",
                "Resource Type": "ITEM", "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
            w.writerow({
                "ASIN": "B000OK01", "Product Name": "Good Book",
                "Sortable Title": "Good", "Sortable Author Name": "A",
                "Series Title": "", "Series Author": "", "Position In Collection": "",
                "Marketplace": "JP", "Relationship Creation Date": "2024-03-20T08:00:00Z",
                "Resource Type": "ITEM", "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
            zf.writestr(
                "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
                buf.getvalue(),
            )

        db = tmp_path / "db.duckdb"
        import_kindle_zip(zip_path, db)
        con = connect(db, read_only=True)
        try:
            row_bad = con.execute(
                "SELECT product_name, relationship_creation_date FROM books WHERE asin = 'B000BAD01'"
            ).fetchone()
            assert row_bad is not None
            assert row_bad[0] == "Broken Date Book"  # 他の列は正常
            assert row_bad[1] is None  # 日付は NULL

            row_ok = con.execute(
                "SELECT relationship_creation_date FROM books WHERE asin = 'B000OK01'"
            ).fetchone()
            assert row_ok[0] is not None
        finally:
            con.close()


class TestEmptyCsv:
    def test_books_csv_headers_only_imports_zero_books(self, tmp_path: Path) -> None:
        """books CSV がヘッダのみ(データ行 0)でも import は成功し、books が空になる。"""
        import csv
        import io
        import zipfile

        zip_path = tmp_path / "empty_books.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            buf = io.StringIO()
            csv.DictWriter(buf, fieldnames=[
                "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
                "Series Title", "Series Author", "Position In Collection", "Marketplace",
                "Relationship Creation Date", "Resource Type", "Ownership Type",
                "Deleted By Customer",
            ]).writeheader()
            zf.writestr(
                "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
                buf.getvalue(),
            )

        db = tmp_path / "db.duckdb"
        result = import_kindle_zip(zip_path, db)
        assert result["books_count"] == 0
        assert result["reading_sessions_count"] == 0
        assert db.exists()
        con = connect(db, read_only=True)
        try:
            assert con.execute("SELECT count(*) FROM books").fetchone()[0] == 0
            # import_metadata はシングルトンで 1 行入る
            assert con.execute("SELECT count(*) FROM import_metadata").fetchone()[0] == 1
        finally:
            con.close()


class TestRequiredColumns:
    def test_books_csv_missing_resource_type_raises(self, tmp_path: Path) -> None:
        """蔵書本体 CSV の必須列欠落は明示的に ValueError を投げる。"""
        zip_path = _write_books_only_zip(tmp_path, headers=[
            "ASIN", "Product Name", "Ownership Type", "Deleted By Customer",
        ], row={
            "ASIN": "B000X", "Product Name": "X",
            "Ownership Type": "Item Owner", "Deleted By Customer": "",
        })
        with pytest.raises(ValueError, match="missing required columns"):
            import_kindle_zip(zip_path, tmp_path / "db.duckdb")

    def test_books_csv_missing_asin_raises(self, tmp_path: Path) -> None:
        zip_path = _write_books_only_zip(tmp_path, headers=[
            "Product Name", "Resource Type", "Ownership Type", "Deleted By Customer",
        ], row={
            "Product Name": "X", "Resource Type": "ITEM",
            "Ownership Type": "Item Owner", "Deleted By Customer": "",
        })
        with pytest.raises(ValueError, match="ASIN"):
            import_kindle_zip(zip_path, tmp_path / "db.duckdb")

    def test_books_csv_invalid_does_not_leave_target_db(self, tmp_path: Path) -> None:
        """必須列欠落で失敗したとき、置換先 DB は作られない(tmp DB は破棄される)。"""
        zip_path = _write_books_only_zip(tmp_path, headers=[
            "ASIN", "Product Name",  # Resource Type / Ownership Type / Deleted By Customer 欠落
        ], row={"ASIN": "B000X", "Product Name": "X"})
        db = tmp_path / "db.duckdb"
        with pytest.raises(ValueError):
            import_kindle_zip(zip_path, db)
        assert not db.exists()

    def test_authors_csv_missing_author_name_raises(self, tmp_path: Path) -> None:
        """optional な authors CSV でも、必須列 Author Name が欠けたら明示エラー。"""
        zip_path = tmp_path / "bad_authors.zip"
        import csv
        import io
        import zipfile

        # 正しい books CSV + Author Name 欠落の authors CSV
        with zipfile.ZipFile(zip_path, "w") as zf:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=[
                "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
                "Series Title", "Series Author", "Position In Collection", "Marketplace",
                "Relationship Creation Date", "Resource Type", "Ownership Type",
                "Deleted By Customer",
            ])
            w.writeheader()
            w.writerow({
                "ASIN": "B000X", "Product Name": "X", "Sortable Title": "",
                "Sortable Author Name": "", "Series Title": "", "Series Author": "",
                "Position In Collection": "", "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM", "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
            zf.writestr(
                "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
                buf.getvalue(),
            )
            # Author Name 列がない authors CSV
            zf.writestr(
                "Kindle.UnifiedLibraryIndex/CustomerAuthorNameRelationship_FE.csv",
                "ASIN\nB000X\n",
            )

        with pytest.raises(ValueError, match="Author Name"):
            import_kindle_zip(zip_path, tmp_path / "db.duckdb")


def _write_books_only_zip_multi(path: Path, books: list[tuple[str, str]]) -> Path:
    """books CSV だけを含む小 zip を複数冊ぶん作る。置換検証用。"""
    import csv
    import io
    import zipfile

    fields = [
        "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
        "Series Title", "Series Author", "Position In Collection", "Marketplace",
        "Relationship Creation Date", "Resource Type", "Ownership Type",
        "Deleted By Customer",
    ]
    with zipfile.ZipFile(path, "w") as zf:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for asin, name in books:
            w.writerow({
                "ASIN": asin, "Product Name": name,
                "Sortable Title": name, "Sortable Author Name": "A",
                "Series Title": "", "Series Author": "", "Position In Collection": "",
                "Marketplace": "JP", "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM", "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
        zf.writestr(
            "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
            buf.getvalue(),
        )
    return path


def _write_books_only_zip(tmp_path: Path, *, headers: list[str], row: dict) -> Path:
    """books CSV だけを含むミニ zip を作る。必須列欠落テスト用。"""
    import csv
    import io
    import zipfile

    zip_path = tmp_path / "bad_books.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers)
        writer.writeheader()
        writer.writerow({h: row.get(h, "") for h in headers})
        zf.writestr(
            "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
            buf.getvalue(),
        )
    return zip_path
