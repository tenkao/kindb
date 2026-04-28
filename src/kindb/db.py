"""Database schema and connection management."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = Path.home() / ".kindb" / "kindle.duckdb"

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS books (
    asin VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    authors_text VARCHAR NOT NULL,
    acquired_at TIMESTAMP NOT NULL,
    read_status VARCHAR NOT NULL,
    product_image_url VARCHAR,
    imported_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS book_authors (
    asin VARCHAR NOT NULL,
    author_name VARCHAR NOT NULL,
    author_order INTEGER NOT NULL,
    PRIMARY KEY (asin, author_order)
);

CREATE TABLE IF NOT EXISTS import_metadata (
    source_path VARCHAR,
    source_type VARCHAR,
    books_count INTEGER,
    imported_at TIMESTAMP
);
"""

VIEWS_SQL = """
CREATE OR REPLACE VIEW v_books AS
SELECT
    b.asin,
    b.title,
    (
        SELECT list(ba.author_name ORDER BY ba.author_order)
        FROM book_authors ba
        WHERE ba.asin = b.asin
    ) AS authors,
    b.authors_text,
    b.read_status,
    b.product_image_url,
    b.acquired_at
FROM books b;

CREATE OR REPLACE VIEW v_author_counts AS
SELECT
    author_name,
    count(DISTINCT asin) AS book_count
FROM book_authors
GROUP BY author_name
ORDER BY book_count DESC, author_name ASC;
"""


def get_db_path(db: str | None = None) -> Path:
    if db:
        return Path(db)
    return Path(os.environ.get("KINDB_DB_PATH", str(DEFAULT_DB_PATH)))


def wal_path(db_path: Path | str) -> Path:
    """Return the DuckDB WAL sidecar path for a given DB path."""
    return Path(str(db_path) + ".wal")


def connect(db_path: Path | str, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(TABLES_SQL)
    con.execute(VIEWS_SQL)
