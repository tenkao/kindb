"""Tests for CLI commands."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
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
    assert "Invalid JSON" in result.stderr
    assert result.stdout == ""


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
    # どの本のどのキーかが分かり、パイプした stdout には混ざらないこと
    assert "B000WARN1" in result.stderr
    assert "extra" in result.stderr
    assert "Warning" not in result.stdout
    assert "1 books" in result.stdout


def test_status_without_db_guides_to_import_and_creates_nothing(tmp_path: Path) -> None:
    # 既定の ~/.kindb もまだない状態を想定し、親ディレクトリも作らないことを確かめる
    db_dir = tmp_path / "kindb"
    result = runner.invoke(app, ["status", "--db", str(db_dir / "nope.duckdb")])
    assert result.exit_code == 1
    assert "No database found" in result.stderr
    assert not db_dir.exists()


def test_status_with_db(imported_db: Path) -> None:
    result = runner.invoke(app, ["status", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert re.search(r"\bBooks\b\W+5\b", result.output)
    assert "Read status: READ" in result.output
    assert "Read status: READING" in result.output
    assert "Read status: UNKNOWN" in result.output
    assert "With image URL" in result.output


@contextmanager
def _another_process_connected(db_path: Path, *, read_only: bool) -> Iterator[None]:
    """MCP サーバや別の kindb のように、別プロセスが DB を開いたままの状態を作る。"""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys; con = duckdb.connect(sys.argv[1], read_only=sys.argv[2] == 'ro'); "
            "print('ready', flush=True); sys.stdin.read(); con.close()",
            str(db_path),
            "ro" if read_only else "rw",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        yield
    finally:
        # 別プロセスが止まってもテストが終わらなくならないよう、待ち時間に上限を置く
        try:
            holder.communicate(input="", timeout=30)
        except subprocess.TimeoutExpired:
            holder.kill()
            raise


def test_read_command_runs_while_another_process_reads(imported_db: Path) -> None:
    # MCP サーバや並列実行された kindb が読み取り接続を持っていても、スキーマが最新なら読み取り系コマンドは通る
    with _another_process_connected(imported_db, read_only=True):
        result = runner.invoke(app, ["status", "--db", str(imported_db)])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("holder_read_only", "args"),
    [
        # MCP サーバの問い合わせ中に import した場合
        (True, ["import", "KINDLE_JSON"]),
        # import の書き込み中に読み取り系コマンドを実行した場合
        (False, ["search", "テスト"]),
    ],
    ids=["import-while-reading", "search-while-writing"],
)
def test_lock_conflict_is_reported_in_one_line(
    imported_db: Path,
    kindle_json: Path,
    holder_read_only: bool,
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # パスより狭い端末幅でも、案内が折り返されず 1 行に収まること
    monkeypatch.setenv("COLUMNS", "40")
    argv = [str(kindle_json) if a == "KINDLE_JSON" else a for a in args]
    with _another_process_connected(imported_db, read_only=holder_read_only):
        result = runner.invoke(app, [*argv, "--db", str(imported_db)])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: Database is in use by another process: ")
    assert result.stderr.count("\n") == 1
    assert str(imported_db) in result.stderr
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _title(imported_db, "B000TEST01") == "テストの本"


def test_other_io_errors_are_not_reported_as_lock_conflict(tmp_path: Path) -> None:
    # ディスク障害などを「使用中」と誤って案内すると、原因を調べる手がかりが消える
    with pytest.raises(duckdb.IOException):
        connect(tmp_path)


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


def test_search_is_case_insensitive(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "reading", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output


def test_search_no_results(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "ZZZNOTFOUND", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_limit_shows_total(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "B000TEST", "-n", "2", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST04" in result.output
    assert "B000TEST02" in result.output
    assert "B000TEST05" not in result.output
    assert "Showing 2 of 5 results" in result.output
    assert "-n 0" in result.output


def test_search_default_limit_and_all(tmp_path: Path) -> None:
    rows = [
        {
            "title": f"Book {i:02d}",
            "authors": "Author",
            "acquiredTime": 1704067200000,
            "readStatus": "UNKNOWN",
            "asin": f"B000MANY{i:02d}",
        }
        for i in range(60)
    ]
    db = tmp_path / "many.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "many.json", rows), db)

    default = runner.invoke(app, ["search", "Book", "--db", str(db)])
    assert default.exit_code == 0
    assert "Showing 50 of 60 results" in default.output
    assert "B000MANY49" in default.output
    assert "B000MANY50" not in default.output

    everything = runner.invoke(app, ["search", "Book", "-n", "0", "--db", str(db)])
    assert everything.exit_code == 0
    assert "Showing 60 of 60 results" in everything.output
    assert "B000MANY59" in everything.output


def test_search_omits_image_url(imported_db: Path) -> None:
    # 表紙 URL は 1 行を長くするため、一覧には出さない(必要なら kindb query で選ぶ)
    result = runner.invoke(app, ["search", "テスト", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "images.example.com" not in result.output


def test_search_rejects_negative_limit(imported_db: Path) -> None:
    result = runner.invoke(app, ["search", "Book", "-n", "-1", "--db", str(imported_db)])
    # 2 は引数エラー。下限の検証がないと DuckDB の例外で 1 になる
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["search", "Paperback"],
        ["recent"],
        ["query", "--table", "SELECT asin, title FROM v_books ORDER BY asin LIMIT 10"],
    ],
    ids=["search", "recent", "query-table"],
)
def test_tables_show_titles_as_is(tmp_path: Path, args: list[str]) -> None:
    # 書名の [英字...] をマークアップとして解釈すると黙って消えるか MarkupError で落ち、:smile: は絵文字に化ける
    titles = ["Clean Code [Paperback]", "Broken [/i] Paperback", "Emoji :thumbs_up: Paperback"]
    rows = [
        {"title": t, "authors": "Author", "acquiredTime": 1704067200000 + i, "readStatus": "UNKNOWN",
         "asin": f"B000MARK0{i}"}
        for i, t in enumerate(titles)
    ]
    db = tmp_path / "markup.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "markup.json", rows), db)

    result = runner.invoke(app, [*args, "--db", str(db)])
    assert result.exit_code == 0, result.output
    for title in titles:
        assert title in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["search", "ソフトウェア"],
        ["recent"],
        ["query", "--table", "SELECT asin, title, authors_text FROM v_books LIMIT 1"],
    ],
    ids=["search", "recent", "query-table"],
)
def test_tables_keep_long_japanese_titles_at_80_columns(
    tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Code の Bash では 80 桁で表が組まれる(前提は test_e2e.py で確かめる)。
    # rich の既定では空白のない日本語の書名が 1 語として「…」で切られるので、その幅でも全文が残ること
    monkeypatch.setenv("COLUMNS", "80")
    title = "ソフトウェアアーキテクチャの基礎 ―エンジニアリングに基づく体系的アプローチ"
    book = {"title": title, "authors": "Mark Richards, Neal Ford, 島田浩二", "acquiredTime": 1704067200000,
            "readStatus": "UNKNOWN", "asin": "B08TWRWZFL"}
    db = tmp_path / "long.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "long.json", [book]), db)

    result = runner.invoke(app, [*args, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "…" not in result.stdout
    assert _column_text(result.stdout, 1) == title.replace(" ", "")


def _column_text(table_output: str, column: int) -> str:
    """1 行だけの表から、折り返された列の断片を空白を除いてつなぐ。rich の既定の罫線(│)を前提にする。"""
    body = [line.split("│") for line in table_output.splitlines() if line.startswith("│")]
    return "".join(cells[column + 1].replace(" ", "") for cells in body)


def test_errors_show_bracketed_paths_as_is(tmp_path: Path) -> None:
    missing = tmp_path / "[bold]missing[/bold].json"
    result = runner.invoke(app, ["import", str(missing), "--db", str(tmp_path / "db.duckdb")])
    assert result.exit_code == 1
    assert str(missing) in result.stderr


@pytest.mark.parametrize(
    ("term", "asin"),
    [
        # 1 文字だけで検索し、ワイルドカードとして解釈されたら全件に当たるようにする
        ("%", "B000TEST04"),
        ("_", "B000TEST04"),
        ("\\", "B000TEST05"),
    ],
)
def test_search_treats_like_wildcards_as_literals(imported_db: Path, term: str, asin: str) -> None:
    result = runner.invoke(app, ["search", term, "--db", str(imported_db)])
    assert result.exit_code == 0
    assert asin in result.output
    assert "Showing 1 of 1 results" in result.output


def test_query_json(imported_db: Path) -> None:
    result = runner.invoke(app, ["query", "SELECT count(*) AS n FROM books", "--db", str(imported_db)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["n"] == 5


def test_query_json_is_not_wrapped_or_markup_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 狭い端末幅(パイプ出力時の既定 80 桁相当)でも JSON が壊れず、角括弧もマークアップとして消えないこと
    monkeypatch.setenv("COLUMNS", "40")
    title = "[bold]Markup[/bold] " + "long title " * 20
    src = create_kindle_json(tmp_path / "long.json", [
        {
            "title": title,
            "authors": "Author",
            "acquiredTime": 1704067200000,
            "readStatus": "UNKNOWN",
            "asin": "B000LONG01",
        }
    ])
    db = tmp_path / "long.duckdb"
    import_kindle_json(src, db)
    result = runner.invoke(app, ["query", "SELECT title FROM v_books LIMIT 1", "--db", str(db)])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["title"] == title


def test_query_table(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1", "--table", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    assert "B000TEST01" in result.output


def test_query_allows_limit_with_offset(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "SELECT asin FROM books ORDER BY asin LIMIT 1 OFFSET 1", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["asin"] == "B000TEST02"


@pytest.mark.parametrize("mode", [[], ["--table"]], ids=["json", "table"])
def test_query_rejects_select_without_limit(imported_db: Path, mode: list[str]) -> None:
    result = runner.invoke(app, ["query", *mode, "SELECT asin FROM books ORDER BY asin", "--db", str(imported_db)])
    assert result.exit_code == 1
    assert "LIMIT 100 OFFSET 0" in result.output
    assert "--allow-unlimited" in result.output


def test_query_allows_unlimited_when_explicit(imported_db: Path) -> None:
    result = runner.invoke(
        app, ["query", "--allow-unlimited", "SELECT asin FROM books ORDER BY asin", "--db", str(imported_db)]
    )
    assert result.exit_code == 0
    assert len(json.loads(result.output)) == 5


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
        "SELECT count(*) FROM books GROUP BY read_status",
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


def test_authors_limit_shows_total(imported_db: Path) -> None:
    result = runner.invoke(app, ["authors", "-n", "2", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "山田太郎" in result.output
    assert "Alice Brown" in result.output
    assert "Jane Doe" not in result.output
    assert "Showing 2 of 7 authors" in result.output


def test_authors_all_with_zero_limit(imported_db: Path) -> None:
    result = runner.invoke(app, ["authors", "-n", "0", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "佐藤花子" in result.output
    assert "Showing 7 of 7 authors" in result.output


def test_recent(imported_db: Path) -> None:
    result = runner.invoke(app, ["recent", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST02" in result.output
    assert "UNKNOWN" in result.output
    assert "images.example.com" not in result.output


def test_recent_respects_limit(imported_db: Path) -> None:
    result = runner.invoke(app, ["recent", "--limit", "1", "--db", str(imported_db)])
    assert result.exit_code == 0
    assert "B000TEST02" in result.output
    assert "B000TEST01" not in result.output


def test_delete_no_db(tmp_path: Path) -> None:
    result = runner.invoke(app, ["delete", "--db", str(tmp_path / "nope.duckdb")])
    assert result.exit_code == 0
    assert "No database" in result.output


def test_delete_cancel(imported_db: Path) -> None:
    runner.invoke(app, ["delete", "--db", str(imported_db)], input="n\n")
    assert imported_db.exists()


def test_delete_with_yes_removes_db_and_wal(imported_db: Path) -> None:
    wal = Path(str(imported_db) + ".wal")
    wal.write_text("fake-wal")
    result = runner.invoke(app, ["delete", "--db", str(imported_db), "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    assert not imported_db.exists()
    assert not wal.exists()


def _title(db_path: Path, asin: str) -> str:
    con = connect(db_path, read_only=True)
    try:
        return con.execute("SELECT title FROM books WHERE asin = ?", [asin]).fetchone()[0]
    finally:
        con.close()
