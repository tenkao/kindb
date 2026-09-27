"""Database schema and connection management."""

from __future__ import annotations

import hashlib
import importlib.resources
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

CREATE TABLE IF NOT EXISTS schema_meta (
    schema_hash VARCHAR NOT NULL
);

-- 以下は kindb enrich が NDL サーチから取得する書誌情報。import 系はこれらに触れない
CREATE TABLE IF NOT EXISTS bib_fetches (
    asin VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    source VARCHAR,
    stages VARCHAR,
    error VARCHAR,
    fetched_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bib_candidates (
    asin VARCHAR NOT NULL,
    candidate_id VARCHAR NOT NULL,
    search_rank INTEGER NOT NULL,
    item_xml VARCHAR NOT NULL,
    PRIMARY KEY (asin, candidate_id)
);

CREATE TABLE IF NOT EXISTS bib_matches (
    asin VARCHAR PRIMARY KEY,
    method VARCHAR NOT NULL,
    candidate_ids VARCHAR[] NOT NULL,
    isbn VARCHAR,
    paper_issued VARCHAR,
    publisher VARCHAR,
    pages INTEGER,
    bib_series VARCHAR,
    ndc VARCHAR,
    ndc_edition VARCHAR,
    matched_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bib_subjects (
    asin VARCHAR NOT NULL,
    subject_order INTEGER NOT NULL,
    subject VARCHAR NOT NULL,
    PRIMARY KEY (asin, subject_order)
);

CREATE TABLE IF NOT EXISTS bib_notes (
    asin VARCHAR NOT NULL,
    note_order INTEGER NOT NULL,
    note VARCHAR NOT NULL,
    PRIMARY KEY (asin, note_order)
);

CREATE TABLE IF NOT EXISTS bib_overrides (
    asin VARCHAR PRIMARY KEY,
    isbn VARCHAR
);

-- 中断した引き直し(--refresh / --retry-missing)の開始日時。次の引き直しは、この日時以降に取得した本を飛ばす
CREATE TABLE IF NOT EXISTS bib_pending_refresh (
    started_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bib_metadata (
    last_enrich_at TIMESTAMP,
    last_enrich_fetched INTEGER,
    last_rematch_at TIMESTAMP,
    last_rematch_books INTEGER,
    overrides_source_path VARCHAR,
    overrides_updated_at TIMESTAMP
);
"""

def _ndc_labels_view_sql() -> str:
    """同梱の NDC9 の 3 桁の分類名(日本図書館協会、CC BY)を VALUES のビューにする。

    テーブルにせずビュー定義に埋め込むのは、create_schema() を冪等な DDL だけに保つため。
    TSV を差し替えればビュー定義とハッシュが変わり、読み取り系コマンドで自動的に移行される。
    """
    text = (importlib.resources.files("kindb") / "data" / "ndc9_3digit.tsv").read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        code, label = line.split("\t")
        rows.append("('{}', '{}')".format(code.replace("'", "''"), label.replace("'", "''")))
    values = ",\n    ".join(rows)
    return f"CREATE OR REPLACE VIEW v_ndc_labels AS\nSELECT * FROM (VALUES\n    {values}\n) AS t(ndc3, label);\n"


VIEWS_SQL = _ndc_labels_view_sql() + """
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
    ) AS author_names_official,
    m.isbn,
    m.paper_issued,
    m.publisher,
    m.pages,
    m.bib_series,
    m.ndc,
    nl.label AS ndc_label,
    coalesce(
        (SELECT list(bs.subject ORDER BY bs.subject_order)
         FROM bib_subjects bs
         WHERE bs.asin = b.asin),
        CAST([] AS VARCHAR[])
    ) AS subjects,
    coalesce(
        (SELECT list(bn.note ORDER BY bn.note_order)
         FROM bib_notes bn
         WHERE bn.asin = b.asin),
        CAST([] AS VARCHAR[])
    ) AS bib_notes,
    m.method AS bib_match
FROM books b
LEFT JOIN bib_matches m ON m.asin = b.asin
LEFT JOIN v_ndc_labels nl ON nl.ndc3 = substr(m.ndc, 1, 3);

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
-- 名前が対応しない本は多数決に入れない。入れると名前のない本が多い著者で '(unknown)' が勝つ。
-- 候補が 1 つもない著者 ID だけ、最後の coalesce で '(unknown)' にする。
WITH paired_names AS (
    SELECT
        ai.author_id,
        an.author_name,
        count(*) AS name_count
    FROM book_author_ids ai
    INNER JOIN books b ON b.asin = ai.asin
    INNER JOIN book_author_names an
      ON an.asin = ai.asin AND an.author_order = ai.author_order
    GROUP BY ai.author_id, an.author_name
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


# 手で上げる版番号は上げ忘れるため、DDL の文字列そのもののハッシュでスキーマの版を表す
SCHEMA_HASH = hashlib.sha256((TABLES_SQL + VIEWS_SQL).encode()).hexdigest()


def get_db_path(db: str | None = None) -> Path:
    if db:
        return Path(db)
    return Path(os.environ.get("KINDB_DB_PATH", str(DEFAULT_DB_PATH)))


def wal_path(db_path: Path | str) -> Path:
    """Return the DuckDB WAL sidecar path for a given DB path."""
    return Path(str(db_path) + ".wal")


def enrich_lock_path(db_path: Path | str) -> Path:
    """kindb enrich / rematch が同時に 1 つだけ動くようにするロックファイルのパス。"""
    return Path(str(db_path) + ".enrich.lock")


class DatabaseLockedError(Exception):
    """別のプロセスが DB を開いていて、ロックを取れない。"""


def connect(db_path: Path | str, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return duckdb.connect(str(db_path), read_only=read_only)
    except duckdb.IOException as e:
        # ロックの衝突だけを案内に置き換える。ディスク障害などほかの IO エラーは、調べられるよう元の例外のまま出す
        if "Could not set lock" not in str(e):
            raise
        raise DatabaseLockedError(
            f"Database is in use by another process: {db_path}. Wait for it to finish, then retry."
        ) from e


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(TABLES_SQL)
    con.execute(VIEWS_SQL)
    con.execute("DELETE FROM schema_meta")
    con.execute("INSERT INTO schema_meta VALUES (?)", [SCHEMA_HASH])


def _schema_is_current(db_path: Path) -> bool:
    con = connect(db_path, read_only=True)
    try:
        row = con.execute("SELECT schema_hash FROM schema_meta").fetchone()
    except duckdb.CatalogException:
        return False
    finally:
        con.close()
    return row is not None and row[0] == SCHEMA_HASH


def ensure_schema(db_path: Path | str) -> None:
    db_path = Path(db_path)
    if not db_path.exists():
        return
    # 書き込み接続は他プロセスの接続(読み取り専用を含む)と共存できないため、移行が必要なときだけ開く
    if _schema_is_current(db_path):
        return
    con = connect(db_path)
    try:
        create_schema(con)
    finally:
        con.close()
