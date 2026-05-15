"""Tests for CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kindb.cli import app
from kindb.db import connect
from kindb.importer import import_kindle_json
from tests.create_fixture import create_kindle_json

runner = CliRunner()


def test_import_success(kindle_json: Path, db_path: Path) -> None:
    result = runner.invoke(app, ["import", str(kindle_json), "--db", str(db_path)])
    assert result.exit_code == 0
    assert "5 books" in result.output


def test_import_nonexistent(tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", str(tmp_path / "nope.json"), "--db", str(tmp_path / "t.duckdb")])
    assert result.exit_code == 1
    assert "JSON file not found" in result.output


def test_import_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    result = runner.invoke(app, ["import", str(bad), "--db", str(tmp_path / "t.duckdb")])
    assert result.exit_code == 1
    assert "Invalid JSON" in result.output


def test_import_unknown_key_warns_on_stderr(tmp_path: Path) -> None:
    src = create_kindle_json(tmp_path / "warn.json", [
        {
            "title": "Warn",
            "authors": "Author",
            "acquiredTime": 1704067200000,
            "readStatus": "UNKNOWN",
            "asin": "B000WARN1",
            "extra": "ignored",
        }
    ])
    result = runner.invoke(app, ["import", str(src), "--db", str(tmp_path / "db.duckdb")])
    assert result.exit_code == 0
    assert "Warning:" in result.output
    assert "extra" in result.output


def test_status_no_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(tmp_path / "nope.duckdb")])
    assert result.exit_code == 1


def test_status_with_db(imported_db: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "Books" in result.output
    assert "5" in result.output
    assert "Read status: READ" in result.output
    assert "Read status: READING" in result.output
    assert "Read status: UNKNOWN" in result.output
    assert "With image URL" in result.output


def test_search_by_title(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "テスト", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "テストの本" in result.output


def test_search_by_author(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "山田", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST01" in result.output
    assert "B000TEST03" in result.output


def test_search_by_asin(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "B000TEST02", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "Another Book" in result.output


def test_search_by_read_status(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "READING", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output


def test_search_is_case_insensitive(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "reading", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output


def test_search_no_results(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "ZZZNOTFOUND", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_escapes_percent_wildcard(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "50%", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output
    assert "B000TEST01" not in result.output


def test_search_escapes_underscore_wildcard(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "OFF_", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output
    assert "B000TEST01" not in result.output


def test_search_escapes_backslash(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "A\\B", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST05" in result.output
    assert "B000TEST02" not in result.output


def test_query_json(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "SELECT count(*) AS n FROM books", "--db", str(imported_db)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["n"] == 5


def test_query_table(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1", "--table", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    assert "B000TEST01" in result.output


def test_query_allows_limit_without_offset(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1", "--db", str(imported_db)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["asin"] == "B000TEST01"


def test_query_allows_limit_with_offset(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1 OFFSET 1", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["asin"] == "B000TEST02"


def test_query_rejects_select_without_limit(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "SELECT asin FROM books ORDER BY asin", "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output
    assert "--allow-unlimited" in result.output


def test_query_rejects_select_without_limit_in_table_mode(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "SELECT asin FROM books ORDER BY asin", "--table", "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output
    assert "--allow-unlimited" in result.output


def test_query_allows_unlimited_when_explicit(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "--allow-unlimited", "SELECT asin FROM books ORDER BY asin", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    assert "--allow-unlimited" not in result.output


def test_query_rejects_write(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "DELETE FROM books", "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "Only SELECT" in result.output


def test_query_rejects_with_without_top_level_limit(imported_db: Path) -> None:
    result = runner.invoke(
        app,
        ["query", "WITH c AS (SELECT count(*) AS n FROM books) SELECT * FROM c", "--db", str(imported_db)],
    )
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output


def test_query_allows_with_top_level_limit(imported_db: Path) -> None:
    result = runner.invoke(
        app,
        ["query", "WITH c AS (SELECT count(*) AS n FROM books) SELECT * FROM c LIMIT 1", "--db", str(imported_db)],
    )
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW TABLES",
        "DESCRIBE v_books",
        "EXPLAIN SELECT 1",
        "PRAGMA database_list",
    ],
)
def test_query_allows_readonly_prefixes(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM v_books",
        "SELECT count(*) AS n FROM v_books",
        "SELECT count(distinct asin) AS n FROM v_books",
        "SELECT sum(book_count) FROM v_author_counts",
        "SELECT avg(book_count), min(book_count), max(book_count) FROM v_author_counts",
    ],
)
def test_query_allows_simple_aggregate_without_limit(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT read_status, count(*) FROM books GROUP BY read_status",
        "SELECT author_name, count(*) FROM book_authors GROUP BY author_name",
    ],
)
def test_query_rejects_grouped_aggregate_without_limit(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*)::VARCHAR FROM books UNION ALL SELECT title FROM v_books",
        "SELECT count(*)::VARCHAR FROM books INTERSECT SELECT title FROM v_books",
        "SELECT count(*)::VARCHAR FROM books EXCEPT SELECT title FROM v_books",
    ],
)
def test_query_rejects_set_operation_aggregate_without_limit(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT asin FROM books ORDER BY asin Limit 1",
        "SELECT asin FROM books ORDER BY asin LIMIT 1;",
        "SELECT asin FROM books ORDER BY asin limit 1  ",
    ],
)
def test_query_accepts_limit_case_and_trailing_semicolon(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'LIMIT' AS x FROM v_books",
        "SELECT asin FROM books -- LIMIT 10",
        "SELECT asin FROM books /* LIMIT 10 */",
        "SELECT * FROM (SELECT * FROM v_books LIMIT 10) t",
        "WITH t AS (SELECT * FROM v_books LIMIT 10) SELECT * FROM t",
        "SELECT * FROM v_books OFFSET 10",
        "SELECT title FROM v_books FETCH FIRST 10 ROWS ONLY",
    ],
)
def test_query_rejects_non_top_level_limit_forms(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS n FROM books; SELECT asin FROM books ORDER BY asin",
        "SELECT asin FROM books ORDER BY asin LIMIT 1; SELECT title FROM books",
    ],
)
def test_query_rejects_multiple_statements(imported_db: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "Only a single SQL statement" in result.output


def test_query_rejects_compound_write_before_execution(imported_db: Path) -> None:
    before = _title(imported_db, "B000TEST01")
    result = runner.invoke(app, [
        "query",
        "SELECT 1; UPDATE books SET title='hacked' WHERE asin='B000TEST01'",
        "--db", str(imported_db),
    ])
    assert result.exit_code == 1
    assert "Only a single SQL statement" in result.output
    assert _title(imported_db, "B000TEST01") == before


def test_authors(imported_db: Path) -> None:
    result = runner.invoke(app, ["authors", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "山田太郎" in result.output
    assert result.output.find("山田太郎") < result.output.find("Alice Brown")


def test_recent(imported_db: Path) -> None:
    result = runner.invoke(app, ["recent", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST02" in result.output
    assert "UNKNOWN" in result.output
    assert "https://images.example.com/B000TEST02.jpg" in result.output


def test_recent_respects_limit(imported_db: Path) -> None:
    result = runner.invoke(app, ["recent", "--limit", "1", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST02" in result.output
    assert "B000TEST01" not in result.output


def test_removed_commands_absent() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "genres" not in result.output
    assert "series" not in result.output
    assert "reading" not in result.output


def test_delete_with_yes(imported_db: Path) -> None:
    result = runner.invoke(app, ["delete", "--db", str(imported_db), "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    assert not imported_db.exists()


def test_delete_no_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["delete", "--db", str(tmp_path / "nope.duckdb")])
    assert result.exit_code == 0
    assert "No database" in result.output


def test_delete_cancel(imported_db: Path) -> None:
    runner.invoke(app, ["delete", "--db", str(imported_db)], input="n\n")
    assert imported_db.exists()


def test_delete_removes_wal(imported_db: Path) -> None:
    wal = Path(str(imported_db) + ".wal")
    wal.write_text("fake-wal")
    result = runner.invoke(app, ["delete", "--db", str(imported_db), "--yes"])
    assert result.exit_code == 0
    assert not imported_db.exists()
    assert not wal.exists()


def test_import_removes_wal_with_db_ext(kindle_json: Path, tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    wal = Path(str(db) + ".wal")
    wal.write_text("fake-wal")
    import_kindle_json(kindle_json, db)
    assert db.exists()
    assert not wal.exists()


def _title(db_path: Path, asin: str) -> str:
    con = connect(db_path, read_only=True)
    try:
        return con.execute("SELECT title FROM books WHERE asin = ?", [asin]).fetchone()[0]
    finally:
        con.close()
