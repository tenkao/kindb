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
