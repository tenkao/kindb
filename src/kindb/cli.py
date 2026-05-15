"""kindb CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from kindb.db import connect, get_db_path, wal_path
from kindb.importer import import_kindle_json

app = typer.Typer(help="Kindle library manager powered by DuckDB.")
console = Console()
err_console = Console(stderr=True)


def _db_option() -> Path:
    return typer.Option(None, "--db", help="Database path (default: ~/.kindb/kindle.duckdb)")


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_db(db: str | None) -> Path:
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print("[yellow]No database found.[/yellow] Run 'kindb import' first.")
        raise typer.Exit(1)
    return db_path


@app.command("import")
def import_cmd(
    json_path: str = typer.Argument(..., help="Path to kindle.json"),
    db: Optional[str] = _db_option(),
) -> None:
    """Import kindle.json into the database."""
    db_path = get_db_path(db)
    try:
        result = import_kindle_json(
            json_path,
            db_path,
            warn=lambda msg: err_console.print(f"[yellow]Warning:[/yellow] {msg}"),
        )
        console.print(f"[green]Import complete:[/green] {result['books_count']} books")
        console.print(f"Database: {result['db_path']}")
    except (FileNotFoundError, ValueError) as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def status(db: Optional[str] = _db_option()) -> None:
    """Show database status."""
    db_path = _require_db(db)

    con = connect(db_path, read_only=True)
    try:
        meta = con.execute(
            "SELECT source_path, source_type, books_count, imported_at FROM import_metadata LIMIT 1"
        ).fetchone()
        books = con.execute("SELECT count(*) FROM books").fetchone()[0]
        authors = con.execute("SELECT count(DISTINCT author_name) FROM book_authors").fetchone()[0]
        images = con.execute("SELECT count(*) FROM books WHERE product_image_url IS NOT NULL").fetchone()[0]
        statuses = con.execute(
            "SELECT read_status, count(*) FROM books GROUP BY read_status ORDER BY read_status"
        ).fetchall()

        table = Table(title="kindb status")
        table.add_column("Item", style="bold")
        table.add_column("Value")
        if meta:
            table.add_row("Last import", str(meta[3]))
            table.add_row("Source", meta[0])
            table.add_row("Source type", meta[1])
        table.add_row("Books", str(books))
        table.add_row("Authors", str(authors))
        for read_status, count in statuses:
            table.add_row(f"Read status: {read_status}", str(count))
        table.add_row("With image URL", str(images))
        table.add_row("Database", str(db_path))
        console.print(table)
    finally:
        con.close()


@app.command()
def search(
    term: str = typer.Argument(..., help="Search term"),
    db: Optional[str] = _db_option(),
) -> None:
    """Search books by title, authors, ASIN, or read status."""
    db_path = _require_db(db)

    con = connect(db_path, read_only=True)
    try:
        like = f"%{_escape_like(term)}%"
        rows = con.execute(
            r"""SELECT asin, title, authors, read_status, product_image_url, acquired_at
               FROM v_books
               WHERE title ILIKE ? ESCAPE '\'
                  OR authors_text ILIKE ? ESCAPE '\'
                  OR asin ILIKE ? ESCAPE '\'
                  OR read_status ILIKE ? ESCAPE '\'
               ORDER BY title""",
            [like, like, like, like],
        ).fetchall()

        if not rows:
            console.print("No results found.")
            return

        table = Table(title=f"Search: {term}")
        table.add_column("ASIN", style="dim")
        table.add_column("Title")
        table.add_column("Authors")
        table.add_column("Status")
        table.add_column("Image URL")
        table.add_column("Acquired")
        for row in rows:
            table.add_row(row[0], row[1], _format_value(row[2]), row[3], row[4] or "", _format_value(row[5]))
        console.print(table)
    finally:
        con.close()


_ALLOWED_SQL = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN|PRAGMA)\b", re.IGNORECASE)
_LIMIT_AT_END = re.compile(r"\blimit\s+\d+\s*(?:offset\s+\d+\s*)?$", re.IGNORECASE)
_AGGREGATE_EXPR = re.compile(
    r"^(count|sum|avg|min|max)\s*\([^()]*\)\s*(?:(?:as\s+)?[a-z_][a-z0-9_]*)?$",
    re.IGNORECASE,
)
_LIMIT_REQUIRED_SQL = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@app.command()
def query(
    sql: str = typer.Argument(..., help="SQL query"),
    table: bool = typer.Option(False, "--table", "-t", help="Output as table instead of JSON"),
    allow_unlimited: bool = typer.Option(
        False,
        "--allow-unlimited",
        help="Allow SELECT/WITH queries without a top-level LIMIT.",
    ),
    db: Optional[str] = _db_option(),
) -> None:
    """Run a read-only SQL query."""
    db_path = _require_db(db)

    if not _ALLOWED_SQL.match(sql):
        err_console.print(
            "[red]Error:[/red] Only SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, PRAGMA statements are allowed."
        )
        raise typer.Exit(1)
    if _has_multiple_statements(sql):
        err_console.print("[red]Error:[/red] Only a single SQL statement is allowed.")
        raise typer.Exit(1)
    if not allow_unlimited and _requires_limit(sql) and not _has_safe_limit_or_aggregate(sql):
        err_console.print(
            "[red]Error:[/red] SELECT/WITH queries must include a top-level LIMIT, for example "
            "`SELECT title FROM v_books ORDER BY title LIMIT 100 OFFSET 0`. "
            "Use `--allow-unlimited` only when you intentionally want an unlimited result."
        )
        raise typer.Exit(1)

    con = connect(db_path, read_only=True)
    try:
        result = con.execute(sql)
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        if table:
            t = Table()
            for col in columns:
                t.add_column(col)
            for row in rows:
                t.add_row(*[_format_value(v) for v in row])
            console.print(t)
        else:
            data = [dict(zip(columns, row)) for row in rows]
            console.print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    finally:
        con.close()


def _requires_limit(sql: str) -> bool:
    return bool(_LIMIT_REQUIRED_SQL.match(sql))


def _has_safe_limit_or_aggregate(sql: str) -> bool:
    stripped = _strip_sql_literals_and_comments(sql)
    return _has_top_level_limit(stripped) or _is_simple_aggregate_query(stripped)


def _has_multiple_statements(sql: str) -> bool:
    normalized = _strip_sql_literals_and_comments(sql).strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return ";" in normalized


def _strip_sql_literals_and_comments(sql: str) -> str:
    chars: list[str] = []
    i = 0
    in_single_quote = False
    in_double_quote = False
    while i < len(sql):
        char = sql[i]
        next_char = sql[i + 1] if i + 1 < len(sql) else ""

        if in_single_quote:
            if char == "'" and next_char == "'":
                chars.extend("  ")
                i += 2
                continue
            if char == "'":
                in_single_quote = False
            chars.append(" ")
            i += 1
            continue

        if in_double_quote:
            if char == '"' and next_char == '"':
                chars.extend("  ")
                i += 2
                continue
            if char == '"':
                in_double_quote = False
            chars.append(" ")
            i += 1
            continue

        if char == "-" and next_char == "-":
            chars.extend("  ")
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                chars.append(" ")
                i += 1
            continue

        if char == "/" and next_char == "*":
            chars.extend("  ")
            i += 2
            while i < len(sql):
                if sql[i] == "*" and i + 1 < len(sql) and sql[i + 1] == "/":
                    chars.extend("  ")
                    i += 2
                    break
                chars.append(" ")
                i += 1
            continue

        if char == "'":
            in_single_quote = True
            chars.append(" ")
        elif char == '"':
            in_double_quote = True
            chars.append(" ")
        else:
            chars.append(char)
        i += 1

    return "".join(chars)


def _has_top_level_limit(sql: str) -> bool:
    normalized = sql.strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return bool(_LIMIT_AT_END.search(normalized))


def _is_simple_aggregate_query(sql: str) -> bool:
    if _has_top_level_group_by(sql) or _has_top_level_set_operation(sql):
        return False

    select_start = _find_top_level_keyword(sql, "select")
    if select_start is None:
        return False
    from_start = _find_top_level_keyword(sql, "from", select_start + len("select"))
    if from_start is None:
        return False

    select_clause = sql[select_start + len("select") : from_start]
    expressions = [expr.strip() for expr in _split_top_level_csv(select_clause)]
    return bool(expressions) and all(_AGGREGATE_EXPR.match(expr) for expr in expressions)


def _has_top_level_group_by(sql: str) -> bool:
    group_start = _find_top_level_keyword(sql, "group")
    if group_start is None:
        return False
    return _find_top_level_keyword(sql, "by", group_start + len("group")) is not None


def _has_top_level_set_operation(sql: str) -> bool:
    return any(_find_top_level_keyword(sql, keyword) is not None for keyword in ("union", "intersect", "except"))


def _find_top_level_keyword(sql: str, keyword: str, start: int = 0) -> int | None:
    depth = 0
    keyword_lower = keyword.lower()
    i = start
    while i < len(sql):
        char = sql[i]
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            i += 1
            continue
        if depth == 0 and sql[i : i + len(keyword)].lower() == keyword_lower:
            before = sql[i - 1] if i > 0 else " "
            after = sql[i + len(keyword)] if i + len(keyword) < len(sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return i
        i += 1
    return None


def _split_top_level_csv(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


def _format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value)


@app.command()
def authors(db: Optional[str] = _db_option()) -> None:
    """Show authors by book count."""
    _run_table_query(
        db,
        """SELECT author_name, book_count
           FROM v_author_counts
           ORDER BY book_count DESC, author_name ASC""",
        title="Authors",
        columns=[("Author", None), ("Books", "right")],
    )


@app.command()
def recent(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of books to show"),
    db: Optional[str] = _db_option(),
) -> None:
    """Show recently acquired books."""
    _run_table_query(
        db,
        """SELECT asin, title, authors, read_status, product_image_url, acquired_at
           FROM v_books
           ORDER BY acquired_at DESC
           LIMIT ?""",
        title="Recent Books",
        columns=[
            ("ASIN", "dim"),
            ("Title", None),
            ("Authors", None),
            ("Status", None),
            ("Image URL", None),
            ("Acquired", None),
        ],
        params=[limit],
    )


@app.command()
def delete(
    db: Optional[str] = _db_option(),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete the database."""
    db_path = get_db_path(db)
    if not db_path.exists() and not wal_path(db_path).exists():
        console.print("No database to delete.")
        return

    if not yes:
        confirm = typer.confirm(f"Delete {db_path}?")
        if not confirm:
            console.print("Cancelled.")
            return

    db_path.unlink(missing_ok=True)
    wal_path(db_path).unlink(missing_ok=True)
    console.print(f"[green]Deleted:[/green] {db_path}")


def _run_table_query(
    db: str | None,
    sql: str,
    *,
    title: str,
    columns: list[tuple[str, str | None]],
    params: list | None = None,
) -> None:
    db_path = _require_db(db)
    con = connect(db_path, read_only=True)
    try:
        rows = con.execute(sql, params or []).fetchall()
        if not rows:
            console.print("No data.")
            return

        table = Table(title=title)
        for name, justify in columns:
            table.add_column(name, justify=justify)
        for row in rows:
            table.add_row(*[_format_value(v) for v in row])
        console.print(table)
    finally:
        con.close()


if __name__ == "__main__":
    app()
