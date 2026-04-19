"""Tests for CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kindb.cli import app

runner = CliRunner()


class TestImportCommand:
    def test_import_success(self, kindle_zip: Path, db_path: Path) -> None:
        result = runner.invoke(app, ["import", str(kindle_zip), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "3 books" in result.output

    def test_import_nonexistent(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["import", str(tmp_path / "nope.zip"), "--db", str(tmp_path / "t.duckdb")])
        assert result.exit_code == 1

    def test_import_invalid_zip(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"bad")
        result = runner.invoke(app, ["import", str(bad), "--db", str(tmp_path / "t.duckdb")])
        assert result.exit_code == 1


class TestStatusCommand:
    def test_status_no_db(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["status", "--db", str(tmp_path / "nope.duckdb")])
        assert result.exit_code == 1

    def test_status_with_db(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["status", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "3" in result.output  # books count


class TestSearchCommand:
    def test_search_by_title(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "テスト", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "テストの本" in result.output

    def test_search_by_author(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "山田", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "B000TEST01" in result.output

    def test_search_by_genre(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "Science Fiction", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "B000TEST02" in result.output

    def test_search_by_series(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "テストシリーズ", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "B000TEST01" in result.output

    def test_search_by_asin(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "B000TEST02", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "Another Book" in result.output

    def test_search_no_results(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["search", "ZZZNOTFOUND", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "No results" in result.output

    def test_search_escapes_percent_wildcard(self, tmp_path: Path) -> None:
        """検索語に含まれる `%` はワイルドカードではなくリテラルとして扱われる。"""
        db = _make_search_fixture(tmp_path, [
            ("B000PERC01", "50% OFF Sale Book"),
            ("B000PERC02", "Regular Book"),
        ])
        result = runner.invoke(app, ["search", "50%", "--db", str(db)])
        assert result.exit_code == 0
        assert "B000PERC01" in result.output
        assert "B000PERC02" not in result.output, (
            "エスケープしないと % が任意マッチして Regular Book もヒットしてしまう"
        )

    def test_search_escapes_underscore_wildcard(self, tmp_path: Path) -> None:
        """検索語に含まれる `_` もリテラル扱い。"""
        db = _make_search_fixture(tmp_path, [
            ("B000UND01", "A_B Book"),
            ("B000UND02", "AXB Book"),
        ])
        result = runner.invoke(app, ["search", "A_B", "--db", str(db)])
        assert result.exit_code == 0
        assert "B000UND01" in result.output
        assert "B000UND02" not in result.output, (
            "エスケープしないと _ が 1 文字マッチして AXB Book もヒットしてしまう"
        )

    def test_search_escapes_backslash(self, tmp_path: Path) -> None:
        """エスケープ順序(先に \\、後にワイルドカード)の回帰ガード。

        もし順序を間違うと、`\\` が `\\\\` になってから `%` のエスケープで
        さらにバックスラッシュが増え、リテラル `\\` を探しているつもりが
        マッチしなくなる。
        """
        db = _make_search_fixture(tmp_path, [
            ("B000BS01", "Path A\\B Book"),
            ("B000BS02", "Path AXB Book"),
        ])
        result = runner.invoke(app, ["search", "A\\B", "--db", str(db)])
        assert result.exit_code == 0
        assert "B000BS01" in result.output
        assert "B000BS02" not in result.output


def _make_search_fixture(tmp_path: Path, books: list[tuple[str, str]]) -> Path:
    """検索エスケープ検証用のミニ kindle.zip → DB を作る。グローバル fixture を汚さない。"""
    import csv
    import io
    import zipfile

    from kindb.importer import import_kindle_zip

    zip_path = tmp_path / "mini.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[
            "ASIN", "Product Name", "Sortable Title", "Sortable Author Name",
            "Series Title", "Series Author", "Position In Collection", "Marketplace",
            "Relationship Creation Date", "Resource Type", "Ownership Type",
            "Deleted By Customer",
        ])
        writer.writeheader()
        for asin, name in books:
            writer.writerow({
                "ASIN": asin, "Product Name": name,
                "Sortable Title": name, "Sortable Author Name": "A",
                "Series Title": "", "Series Author": "", "Position In Collection": "",
                "Marketplace": "JP", "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM", "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            })
        zf.writestr("Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv", buf.getvalue())

    db = tmp_path / "mini.duckdb"
    import_kindle_zip(zip_path, db)
    return db


class TestQueryCommand:
    def test_query_json(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["query", "SELECT count(*) AS n FROM books", "--db", str(imported_db)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["n"] == 3

    def test_query_table(self, imported_db: Path) -> None:
        result = runner.invoke(
            app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1", "--table", "--db", str(imported_db)]
        )
        assert result.exit_code == 0
        assert "B000TEST01" in result.output

    def test_query_rejects_write(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["query", "DELETE FROM books", "--db", str(imported_db)])
        assert result.exit_code == 1
        assert "Only SELECT" in result.output

    def test_query_rejects_insert(self, imported_db: Path) -> None:
        result = runner.invoke(
            app, ["query", "INSERT INTO books (asin) VALUES ('X')", "--db", str(imported_db)]
        )
        assert result.exit_code == 1

    def test_query_rejects_drop(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["query", "DROP TABLE books", "--db", str(imported_db)])
        assert result.exit_code == 1

    def test_query_allows_with(self, imported_db: Path) -> None:
        result = runner.invoke(
            app,
            ["query", "WITH c AS (SELECT count(*) AS n FROM books) SELECT * FROM c", "--db", str(imported_db)],
        )
        assert result.exit_code == 0

    def test_query_allows_describe(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["query", "DESCRIBE books", "--db", str(imported_db)])
        assert result.exit_code == 0

    def test_query_compound_write_rejected_by_readonly(self, imported_db: Path) -> None:
        """regex を通る複文(先頭 SELECT + 後続 UPDATE)を DuckDB の
        read-only 接続が拒否することを二重防御として検証する。"""
        from kindb.db import connect
        # 実行前の値を控える
        con = connect(imported_db, read_only=True)
        try:
            before = con.execute(
                "SELECT product_name FROM books WHERE asin = 'B000TEST01'"
            ).fetchone()[0]
        finally:
            con.close()

        result = runner.invoke(app, [
            "query",
            "SELECT 1; UPDATE books SET product_name='hacked' WHERE asin='B000TEST01'",
            "--db", str(imported_db),
        ])
        assert result.exit_code != 0

        # DB が実際に書き換わっていないこと
        con = connect(imported_db, read_only=True)
        try:
            after = con.execute(
                "SELECT product_name FROM books WHERE asin = 'B000TEST01'"
            ).fetchone()[0]
        finally:
            con.close()
        assert after == before

    def test_query_compound_drop_rejected_by_readonly(self, imported_db: Path) -> None:
        """先頭 SELECT + DROP TABLE もテーブル存続することを確認。"""
        result = runner.invoke(app, [
            "query",
            "SELECT 1; DROP TABLE books",
            "--db", str(imported_db),
        ])
        assert result.exit_code != 0

        from kindb.db import connect
        con = connect(imported_db, read_only=True)
        try:
            # books が残っている
            count = con.execute("SELECT count(*) FROM books").fetchone()[0]
            assert count == 3
        finally:
            con.close()


class TestAggCommands:
    def test_authors(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["authors", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "山田太郎" in result.output

    def test_genres(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["genres", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "文学・評論" in result.output

    def test_series(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["series", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "テストシリーズ" in result.output

    def test_recent(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["recent", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "B000TEST02" in result.output  # most recent by date

    def test_reading(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["reading", "--db", str(imported_db)])
        assert result.exit_code == 0
        assert "B000TEST01" in result.output

    def test_reading_does_not_double_count_insights(self, imported_db: Path) -> None:
        """fixture には reading_sessions に B000TEST01 が 2 件、
        reading_insight_sessions にも同 ASIN で 1 件追加で入っている。
        kindb reading の sessions 列は 2(reading_sessions のみ由来)で
        あり、insight を合算した 3 にならないことを検証する。"""
        from kindb.db import connect
        # まず SQL レベルで確定: reading_sessions = 2 rows, insight = 1 row
        con = connect(imported_db, read_only=True)
        try:
            rs_count = con.execute(
                "SELECT count(*) FROM reading_sessions WHERE asin = 'B000TEST01'"
            ).fetchone()[0]
            insight_count = con.execute(
                "SELECT count(*) FROM reading_insight_sessions WHERE asin = 'B000TEST01'"
            ).fetchone()[0]
            assert rs_count == 2
            assert insight_count == 1  # もし 0 なら fixture 側の前提が変わっている
            summary = con.execute(
                "SELECT reading_session_count FROM v_reading_summary WHERE asin = 'B000TEST01'"
            ).fetchone()[0]
            assert summary == 2, "v_reading_summary は reading_sessions のみ集計(insight 非混入)"
        finally:
            con.close()

        result = runner.invoke(app, ["reading", "--db", str(imported_db)])
        assert result.exit_code == 0
        # 出力から B000TEST01 行を抽出し、sessions 列が 2 であること
        import re
        match = re.search(r"B000TEST01.*", result.output)
        assert match is not None
        line = match.group(0)
        # rich の Table 出力では列は│区切り or 空白区切り。"2" と "3" のどちらが
        # 含まれるかで区別する(2 が正、3 なら二重計上)
        assert " 2 " in line or line.rstrip().endswith(" 2") or "│ 2 " in line, (
            f"sessions=2 が期待されるが、行: {line!r}"
        )


class TestDeleteCommand:
    def test_delete_with_confirm(self, imported_db: Path) -> None:
        result = runner.invoke(app, ["delete", "--db", str(imported_db), "--yes"])
        assert result.exit_code == 0
        assert "Deleted" in result.output
        assert not imported_db.exists()

    def test_delete_no_db(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["delete", "--db", str(tmp_path / "nope.duckdb")])
        assert result.exit_code == 0
        assert "No database" in result.output

    def test_delete_cancel(self, imported_db: Path) -> None:
        runner.invoke(app, ["delete", "--db", str(imported_db)], input="n\n")
        assert imported_db.exists()

    def test_delete_removes_wal_with_duckdb_ext(self, imported_db: Path) -> None:
        """delete が foo.duckdb.wal を消す。"""
        wal = Path(str(imported_db) + ".wal")
        wal.write_text("fake-wal")
        result = runner.invoke(app, ["delete", "--db", str(imported_db), "--yes"])
        assert result.exit_code == 0
        assert not imported_db.exists()
        assert not wal.exists()

    def test_delete_removes_wal_with_db_ext(self, kindle_zip: Path, tmp_path: Path) -> None:
        """拡張子が .db でも foo.db.wal が消える(旧実装は foo.wal を見て失敗)。"""
        from kindb.importer import import_kindle_zip
        db = tmp_path / "store.db"
        import_kindle_zip(kindle_zip, db)
        wal = Path(str(db) + ".wal")
        wal.write_text("fake-wal")
        result = runner.invoke(app, ["delete", "--db", str(db), "--yes"])
        assert result.exit_code == 0
        assert not db.exists()
        assert not wal.exists()
