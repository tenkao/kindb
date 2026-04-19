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
from kindb.importer import import_kindle_zip

app = typer.Typer(help="Kindle library manager powered by DuckDB.")
console = Console(width=200)
err_console = Console(stderr=True, width=200)


def _db_option() -> Path:
    return typer.Option(None, "--db", help="Database path (default: ~/.kindb/kindle.duckdb)")


@app.command("import")
def import_cmd(
    zip_path: str = typer.Argument(..., help="Path to Kindle.zip"),
    db: Optional[str] = _db_option(),
) -> None:
    """Import Kindle.zip into the database."""
    db_path = get_db_path(db)
    try:
        result = import_kindle_zip(zip_path, db_path)
        console.print(f"[green]Import complete:[/green] {result['books_count']} books, "
                       f"{result['reading_sessions_count']} reading sessions")
        console.print(f"Database: {result['db_path']}")
    except FileNotFoundError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def status(db: Optional[str] = _db_option()) -> None:
    """Show database status."""
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print("[yellow]No database found.[/yellow] Run 'kindb import' first.")
        raise typer.Exit(1)

    con = connect(db_path, read_only=True)
    try:
        meta = con.execute("SELECT * FROM import_metadata LIMIT 1").fetchone()
        books = con.execute("SELECT count(*) FROM books").fetchone()[0]
        authors = con.execute("SELECT count(DISTINCT author_name) FROM book_authors").fetchone()[0]
        genres = con.execute("SELECT count(DISTINCT genre) FROM book_genres").fetchone()[0]
        sessions = con.execute("SELECT count(*) FROM reading_sessions").fetchone()[0]
        docs = con.execute("SELECT count(*) FROM personal_documents").fetchone()[0]

        table = Table(title="kindb status")
        table.add_column("Item", style="bold")
        table.add_column("Value")
        if meta:
            table.add_row("Last import", str(meta[3]))
            table.add_row("Source", meta[1])
        table.add_row("Books", str(books))
        table.add_row("Authors", str(authors))
        table.add_row("Genres", str(genres))
        table.add_row("Reading sessions", str(sessions))
        table.add_row("Personal documents", str(docs))
        table.add_row("Database", str(db_path))
        console.print(table)
    finally:
        con.close()


@app.command()
def search(
    term: str = typer.Argument(..., help="Search term"),
    db: Optional[str] = _db_option(),
) -> None:
    """Search books by title, author, genre, series, or ASIN."""
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print("[yellow]No database found.[/yellow]")
        raise typer.Exit(1)

    con = connect(db_path, read_only=True)
    try:
        like = f"%{term}%"
        rows = con.execute(
            """SELECT asin, product_name, authors, genres, series_title,
                      reading_session_count, last_read_at
               FROM v_books_with_reading
               WHERE product_name ILIKE ?
                  OR array_to_string(authors, ', ') ILIKE ?
                  OR array_to_string(genres, ', ') ILIKE ?
                  OR series_title ILIKE ?
                  OR asin ILIKE ?
               ORDER BY product_name""",
            [like, like, like, like, like],
        ).fetchall()

        if not rows:
            console.print("No results found.")
            return

        table = Table(title=f"Search: {term}")
        table.add_column("ASIN", style="dim")
        table.add_column("Title")
        table.add_column("Authors")
        table.add_column("Genres")
        table.add_column("Series")
        table.add_column("Sessions", justify="right")
        table.add_column("Last Read")
        for row in rows:
            table.add_row(
                row[0],
                row[1] or "",
                ", ".join(row[2]) if row[2] else "",
                ", ".join(row[3]) if row[3] else "",
                row[4] or "",
                str(row[5] or 0),
                str(row[6] or ""),
            )
        console.print(table)
    finally:
        con.close()


_ALLOWED_SQL = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN|PRAGMA)\b", re.IGNORECASE)


@app.command()
def query(
    sql: str = typer.Argument(..., help="SQL query"),
    table: bool = typer.Option(False, "--table", "-t", help="Output as table instead of JSON"),
    db: Optional[str] = _db_option(),
) -> None:
    """Run a read-only SQL query."""
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print("[yellow]No database found.[/yellow]")
        raise typer.Exit(1)

    if not _ALLOWED_SQL.match(sql):
        err_console.print(
            "[red]Error:[/red] Only SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, PRAGMA statements are allowed."
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
            data = [dict(zip(columns, [_json_value(v) for v in row])) for row in rows]
            console.print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    finally:
        con.close()


def _format_value(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _json_value(v: object) -> object:
    if isinstance(v, list):
        return v
    return v


@app.command()
def authors(db: Optional[str] = _db_option()) -> None:
    """Show authors by book count."""
    _run_agg_query(
        db,
        """SELECT ba.author_name AS author, count(DISTINCT ba.asin) AS books
           FROM book_authors ba
           JOIN books b ON ba.asin = b.asin
           GROUP BY ba.author_name
           ORDER BY books DESC, author""",
        title="Authors",
        columns=[("Author", None), ("Books", "right")],
    )


@app.command()
def genres(db: Optional[str] = _db_option()) -> None:
    """Show genres by book count."""
    _run_agg_query(
        db,
        """SELECT bg.genre, count(DISTINCT bg.asin) AS books
           FROM book_genres bg
           JOIN books b ON bg.asin = b.asin
           GROUP BY bg.genre
           ORDER BY books DESC, genre""",
        title="Genres",
        columns=[("Genre", None), ("Books", "right")],
    )


@app.command()
def series(db: Optional[str] = _db_option()) -> None:
    """Show series with book counts and positions."""
    _run_agg_query(
        db,
        """SELECT series_title, series_author,
                  count(*) AS books,
                  string_agg(DISTINCT position_in_collection, ', ' ORDER BY position_in_collection) AS positions
           FROM books
           WHERE series_title IS NOT NULL AND series_title != ''
           GROUP BY series_title, series_author
           ORDER BY books DESC, series_title""",
        title="Series",
        columns=[("Series", None), ("Author", None), ("Books", "right"), ("Positions", None)],
    )


@app.command()
def recent(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of books to show"),
    db: Optional[str] = _db_option(),
) -> None:
    """Show recently added books."""
    _run_agg_query(
        db,
        f"""SELECT asin, product_name, sortable_author_name, relationship_creation_date
           FROM books
           ORDER BY relationship_creation_date DESC NULLS LAST
           LIMIT {limit}""",
        title="Recent Books",
        columns=[("ASIN", "dim"), ("Title", None), ("Author", None), ("Added", None)],
    )


@app.command()
def reading(db: Optional[str] = _db_option()) -> None:
    """Show reading session summary per book."""
    _run_agg_query(
        db,
        """SELECT rs.asin,
                  b.product_name,
                  rs.reading_session_count AS sessions,
                  rs.last_read_at AS last_read,
                  rs.total_reading_millis AS total_millis,
                  rs.total_page_flips AS total_flips
           FROM v_reading_summary rs
           LEFT JOIN books b ON rs.asin = b.asin
           ORDER BY last_read DESC NULLS LAST""",
        title="Reading Sessions",
        columns=[
            ("ASIN", "dim"),
            ("Title", None),
            ("Sessions", "right"),
            ("Last Read", None),
            ("Total ms", "right"),
            ("Page Flips", "right"),
        ],
    )


@app.command()
def delete(
    db: Optional[str] = _db_option(),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete the database."""
    db_path = get_db_path(db)
    if not db_path.exists():
        console.print("No database to delete.")
        return

    if not yes:
        confirm = typer.confirm(f"Delete {db_path}?")
        if not confirm:
            console.print("Cancelled.")
            return

    db_path.unlink()
    wal = wal_path(db_path)
    if wal.exists():
        wal.unlink()
    console.print(f"[green]Deleted:[/green] {db_path}")


def _run_agg_query(
    db: str | None,
    sql: str,
    *,
    title: str,
    columns: list[tuple[str, str | None]],
) -> None:
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print("[yellow]No database found.[/yellow]")
        raise typer.Exit(1)

    con = connect(db_path, read_only=True)
    try:
        rows = con.execute(sql).fetchall()
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
