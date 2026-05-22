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

CREATE TABLE IF NOT EXISTS book_genres (
    asin VARCHAR NOT NULL,
    genre VARCHAR NOT NULL,
    PRIMARY KEY (asin, genre)
);

CREATE TABLE IF NOT EXISTS book_series (
    asin VARCHAR NOT NULL,
    series_asin VARCHAR NOT NULL,
    series_title VARCHAR NOT NULL,
    series_author VARCHAR,
    series_author_id VARCHAR,
    position_in_collection INTEGER,
    relation_type VARCHAR NOT NULL,
    PRIMARY KEY (asin, series_asin, relation_type)
);

CREATE TABLE IF NOT EXISTS book_author_ids (
    asin VARCHAR NOT NULL,
    author_id VARCHAR NOT NULL,
    author_order INTEGER NOT NULL,
    PRIMARY KEY (asin, author_order)
);

CREATE TABLE IF NOT EXISTS book_author_names (
    asin VARCHAR NOT NULL,
    author_name VARCHAR NOT NULL,
    author_order INTEGER NOT NULL,
    PRIMARY KEY (asin, author_order)
);

CREATE TABLE IF NOT EXISTS import_metadata_official (
    source_path VARCHAR,
    source_type VARCHAR,
    genres_count INTEGER,
    series_count INTEGER,
    author_ids_count INTEGER,
    author_names_count INTEGER,
    distinct_asin_count INTEGER,
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
    b.acquired_at,
    coalesce(
        (SELECT list(g.genre ORDER BY g.genre)
         FROM book_genres g
         WHERE g.asin = b.asin),
        CAST([] AS VARCHAR[])
    ) AS genres,
    (
        SELECT s.series_title
        FROM book_series s
        WHERE s.asin = b.asin AND s.relation_type = 'PRIMARY'
        ORDER BY s.series_title ASC, s.position_in_collection ASC NULLS LAST, s.series_asin ASC
        LIMIT 1
    ) AS series_title,
    (
        SELECT NULLIF(s.series_asin, '')
        FROM book_series s
        WHERE s.asin = b.asin AND s.relation_type = 'PRIMARY'
        ORDER BY s.series_title ASC, s.position_in_collection ASC NULLS LAST, s.series_asin ASC
        LIMIT 1
    ) AS series_asin,
    (
        SELECT s.position_in_collection
        FROM book_series s
        WHERE s.asin = b.asin AND s.relation_type = 'PRIMARY'
        ORDER BY s.series_title ASC, s.position_in_collection ASC NULLS LAST, s.series_asin ASC
        LIMIT 1
    ) AS series_position,
    coalesce(
        (SELECT list(ai.author_id ORDER BY ai.author_order)
         FROM book_author_ids ai
         WHERE ai.asin = b.asin),
        CAST([] AS VARCHAR[])
    ) AS author_ids,
    coalesce(
        (SELECT list(an.author_name ORDER BY an.author_order)
         FROM book_author_names an
         WHERE an.asin = b.asin),
        CAST([] AS VARCHAR[])
    ) AS author_names_official
FROM books b;

CREATE OR REPLACE VIEW v_author_counts AS
SELECT
    author_name,
    count(DISTINCT asin) AS book_count
FROM book_authors
GROUP BY author_name
ORDER BY book_count DESC, author_name ASC;

CREATE OR REPLACE VIEW v_book_genres AS
SELECT
    b.asin,
    b.title,
    g.genre
FROM books b
INNER JOIN book_genres g ON g.asin = b.asin
ORDER BY g.genre ASC, b.title ASC, b.asin ASC;

CREATE OR REPLACE VIEW v_book_series AS
SELECT
    NULLIF(s.series_asin, '') AS series_asin,
    s.series_title,
    s.position_in_collection AS series_position,
    b.asin,
    b.title,
    s.relation_type
FROM books b
INNER JOIN book_series s ON s.asin = b.asin
ORDER BY s.series_title ASC, s.position_in_collection ASC NULLS LAST, b.asin ASC;

CREATE OR REPLACE VIEW v_series_counts AS
SELECT
    NULLIF(s.series_asin, '') AS series_asin,
    s.series_title,
    count(DISTINCT b.asin) AS book_count
FROM books b
INNER JOIN book_series s ON s.asin = b.asin
WHERE s.relation_type = 'PRIMARY'
GROUP BY NULLIF(s.series_asin, ''), s.series_title
ORDER BY book_count DESC, s.series_title ASC;

CREATE OR REPLACE VIEW v_genre_counts AS
SELECT
    g.genre,
    count(DISTINCT b.asin) AS book_count
FROM books b
INNER JOIN book_genres g ON g.asin = b.asin
GROUP BY g.genre
ORDER BY book_count DESC, g.genre ASC;

CREATE OR REPLACE VIEW v_book_authors_official AS
SELECT
    coalesce(ai.asin, an.asin) AS asin,
    coalesce(ai.author_order, an.author_order) AS author_order,
    ai.author_id,
    an.author_name
FROM book_author_ids ai
FULL OUTER JOIN book_author_names an
  ON an.asin = ai.asin AND an.author_order = ai.author_order
INNER JOIN books b ON b.asin = coalesce(ai.asin, an.asin)
ORDER BY asin ASC, author_order ASC;

CREATE OR REPLACE VIEW v_author_id_counts AS
WITH paired_names AS (
    SELECT
        ai.author_id,
        coalesce(an.author_name, '(unknown)') AS author_name,
        count(*) AS name_count
    FROM book_author_ids ai
    INNER JOIN books b ON b.asin = ai.asin
    LEFT JOIN book_author_names an
      ON an.asin = ai.asin AND an.author_order = ai.author_order
    GROUP BY ai.author_id, coalesce(an.author_name, '(unknown)')
),
ranked_names AS (
    SELECT
        author_id,
        author_name,
        row_number() OVER (
            PARTITION BY author_id
            ORDER BY name_count DESC, author_name ASC
        ) AS rn
    FROM paired_names
),
counts AS (
    SELECT
        ai.author_id,
        count(DISTINCT b.asin) AS book_count
    FROM book_author_ids ai
    INNER JOIN books b ON b.asin = ai.asin
    GROUP BY ai.author_id
)
SELECT
    c.author_id,
    coalesce(r.author_name, '(unknown)') AS author_name,
    c.book_count
FROM counts c
LEFT JOIN ranked_names r ON r.author_id = c.author_id AND r.rn = 1
ORDER BY c.book_count DESC, author_name ASC, c.author_id ASC;
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


def ensure_schema(db_path: Path | str) -> None:
    db_path = Path(db_path)
    if not db_path.exists():
        return
    con = connect(db_path)
    try:
        create_schema(con)
    finally:
        con.close()
