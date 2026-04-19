"""Database schema and connection management."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = Path.home() / ".kindb" / "kindle.duckdb"

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS books (
    asin VARCHAR NOT NULL,
    product_name VARCHAR,
    sortable_title VARCHAR,
    sortable_author_name VARCHAR,
    series_title VARCHAR,
    series_author VARCHAR,
    position_in_collection VARCHAR,
    marketplace VARCHAR,
    relationship_creation_date TIMESTAMP,
    source_file VARCHAR,
    imported_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS book_authors (
    asin VARCHAR NOT NULL,
    author_name VARCHAR,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS book_genres (
    asin VARCHAR NOT NULL,
    genre VARCHAR,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS book_images (
    asin VARCHAR NOT NULL,
    image_url VARCHAR,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS reading_sessions (
    asin VARCHAR NOT NULL,
    start_timestamp TIMESTAMP,
    end_timestamp TIMESTAMP,
    content_type VARCHAR,
    total_reading_millis BIGINT,
    number_of_page_flips INTEGER,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS reading_insight_sessions (
    asin VARCHAR NOT NULL,
    product_name VARCHAR,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    total_reading_milliseconds BIGINT,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS personal_documents (
    document_id VARCHAR NOT NULL,
    title VARCHAR,
    document_provider VARCHAR,
    filename VARCHAR,
    document_original_type VARCHAR,
    document_size_in_bytes BIGINT,
    entry_creation_date TIMESTAMP,
    source_file VARCHAR
);

CREATE TABLE IF NOT EXISTS import_metadata (
    import_id INTEGER PRIMARY KEY DEFAULT 1,
    source_path VARCHAR,
    source_type VARCHAR,
    imported_at TIMESTAMP DEFAULT current_timestamp,
    books_count INTEGER,
    reading_sessions_count INTEGER
);
"""

VIEWS_SQL = """
CREATE OR REPLACE VIEW v_books AS
SELECT
    b.asin,
    b.product_name,
    b.sortable_title,
    b.sortable_author_name,
    (SELECT list(DISTINCT ba.author_name ORDER BY ba.author_name)
     FROM book_authors ba WHERE ba.asin = b.asin) AS authors,
    (SELECT list(DISTINCT bg.genre ORDER BY bg.genre)
     FROM book_genres bg WHERE bg.asin = b.asin) AS genres,
    (SELECT min(bi.image_url)
     FROM book_images bi WHERE bi.asin = b.asin) AS image_url,
    b.series_title,
    b.series_author,
    b.position_in_collection,
    b.marketplace,
    b.relationship_creation_date
FROM books b;

CREATE OR REPLACE VIEW v_reading_summary AS
SELECT
    asin,
    count(*) AS reading_session_count,
    min(start_timestamp) AS first_read_at,
    max(start_timestamp) AS last_read_at,
    sum(total_reading_millis) AS total_reading_millis,
    sum(number_of_page_flips) AS total_page_flips
FROM reading_sessions
GROUP BY asin;

CREATE OR REPLACE VIEW v_books_with_reading AS
SELECT
    vb.*,
    rs.reading_session_count,
    rs.first_read_at,
    rs.last_read_at,
    rs.total_reading_millis,
    rs.total_page_flips
FROM v_books vb
LEFT JOIN v_reading_summary rs ON vb.asin = rs.asin;
"""


def get_db_path(db: str | None = None) -> Path:
    if db:
        return Path(db)
    return Path(os.environ.get("KINDB_DB_PATH", str(DEFAULT_DB_PATH)))


def wal_path(db_path: Path | str) -> Path:
    """Return the DuckDB WAL sidecar path for a given DB path.

    DuckDB appends ``.wal`` to the full DB path regardless of the DB file's
    extension (``foo.db`` → ``foo.db.wal``, ``foo`` → ``foo.wal``), so we
    never use ``Path.with_suffix`` here.
    """
    return Path(str(db_path) + ".wal")


def connect(db_path: Path | str, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(TABLES_SQL)
    con.execute(VIEWS_SQL)
