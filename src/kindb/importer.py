"""Import Kindle.zip into DuckDB."""

from __future__ import annotations

import csv
import io
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from kindb.db import connect, create_schema, wal_path


def _find_csv_in_zip(zf: zipfile.ZipFile, suffix: str) -> str | None:
    for name in zf.namelist():
        if name.endswith(suffix):
            return name
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a timestamp string and return a timezone-naive ``datetime``.

    DuckDB ``TIMESTAMP`` is timezone-naive, so the return value is always
    naive. The normalization rule is:

    * **Aware input** (``...Z`` or ``...+09:00``) is converted to UTC, then
      ``tzinfo`` is stripped.
    * **Naive input** (e.g. ``2024-01-15 10:30:00`` or ``2024-01-15``) is
      returned as-is without any conversion.
    * Unparseable input returns ``None``.
    """
    if not value or not value.strip():
        return None
    s = value.strip()
    # Treat trailing "Z" as UTC so %z can parse it uniformly.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _parse_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_bigint(value: str | None) -> int | None:
    return _parse_int(value)


def _read_csv(
    zf: zipfile.ZipFile,
    path: str,
    *,
    required_columns: set[str] | None = None,
) -> list[dict[str, str]]:
    """Read a CSV from the zip. If required_columns is given, raise ValueError
    when any of them are absent from the header. Column names are compared
    case-sensitively to match Amazon's official export."""
    with zf.open(path) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        reader = csv.DictReader(text)
        if required_columns is not None:
            headers = set(reader.fieldnames or [])
            missing = required_columns - headers
            if missing:
                raise ValueError(
                    f"{path} is missing required columns: {sorted(missing)}"
                )
        return list(reader)


# Required columns per optional CSV. Missing any of these raises ValueError
# rather than silently importing NULLs — a silent 0-row success or all-NULL
# column is the failure mode we most want to surface to the user.
_BOOKS_REQUIRED_COLUMNS = {
    "ASIN",
    "Resource Type",
    "Ownership Type",
    "Deleted By Customer",
    "Relationship Creation Date",
}
_AUTHORS_REQUIRED_COLUMNS = {"ASIN", "Author Name"}
_GENRES_REQUIRED_COLUMNS = {"ASIN", "Genre"}
_IMAGES_REQUIRED_COLUMNS = {"ASIN", "Image URL"}


def _import_books(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> int:
    path = _find_csv_in_zip(zf, "CustomerRelationshipIndex_FE.csv")
    if path is None:
        raise FileNotFoundError("CustomerRelationshipIndex_FE.csv not found in zip")

    rows = _read_csv(zf, path, required_columns=_BOOKS_REQUIRED_COLUMNS)
    source = path.split("/")[-1]
    count = 0
    for row in rows:
        resource_type = (row.get("Resource Type") or "").strip()
        ownership_type = (row.get("Ownership Type") or "").strip()
        deleted = (row.get("Deleted By Customer") or "").strip()

        if resource_type != "ITEM" or ownership_type != "Item Owner" or deleted == "Yes":
            continue

        asin = (row.get("ASIN") or "").strip()
        if not asin:
            continue

        con.execute(
            """INSERT INTO books (asin, product_name, sortable_title, sortable_author_name,
               series_title, series_author, position_in_collection, marketplace,
               relationship_creation_date, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                asin,
                row.get("Product Name"),
                row.get("Sortable Title"),
                row.get("Sortable Author Name"),
                row.get("Series Title"),
                row.get("Series Author"),
                row.get("Position In Collection"),
                row.get("Marketplace"),
                _parse_timestamp(row.get("Relationship Creation Date")),
                source,
            ],
        )
        count += 1
    return count


def _import_authors(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> None:
    path = _find_csv_in_zip(zf, "CustomerAuthorNameRelationship_FE.csv")
    if path is None:
        return

    rows = _read_csv(zf, path, required_columns=_AUTHORS_REQUIRED_COLUMNS)
    source = path.split("/")[-1]
    for row in rows:
        asin = (row.get("ASIN") or "").strip()
        if not asin:
            continue
        con.execute(
            "INSERT INTO book_authors (asin, author_name, source_file) VALUES (?, ?, ?)",
            [asin, row.get("Author Name"), source],
        )


def _import_genres(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> None:
    path = _find_csv_in_zip(zf, "CustomerGenres_FE.csv")
    if path is None:
        return

    rows = _read_csv(zf, path, required_columns=_GENRES_REQUIRED_COLUMNS)
    source = path.split("/")[-1]
    for row in rows:
        asin = (row.get("ASIN") or "").strip()
        if not asin:
            continue
        con.execute(
            "INSERT INTO book_genres (asin, genre, source_file) VALUES (?, ?, ?)",
            [asin, row.get("Genre"), source],
        )


def _import_images(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> None:
    path = _find_csv_in_zip(zf, "CustomerTags_FE.csv")
    if path is None:
        return

    rows = _read_csv(zf, path, required_columns=_IMAGES_REQUIRED_COLUMNS)
    source = path.split("/")[-1]
    for row in rows:
        asin = (row.get("ASIN") or "").strip()
        image_url = (row.get("Image URL") or "").strip()
        if not asin or not image_url:
            continue
        con.execute(
            "INSERT INTO book_images (asin, image_url, source_file) VALUES (?, ?, ?)",
            [asin, image_url, source],
        )


def _import_reading_sessions(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> int:
    path = _find_csv_in_zip(zf, "Kindle.Devices.ReadingSession.csv")
    if path is None:
        return 0

    rows = _read_csv(zf, path)
    source = path.split("/")[-1]
    count = 0
    for row in rows:
        asin = (row.get("ASIN") or "").strip()
        if not asin:
            continue
        con.execute(
            """INSERT INTO reading_sessions
               (asin, start_timestamp, end_timestamp, content_type,
                total_reading_millis, number_of_page_flips, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                asin,
                _parse_timestamp(row.get("Start Timestamp")),
                _parse_timestamp(row.get("End Timestamp")),
                row.get("Content Type"),
                _parse_bigint(row.get("Total Reading Millis")),
                _parse_int(row.get("Number Of Page Flips")),
                source,
            ],
        )
        count += 1
    return count


def _import_reading_insight_sessions(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> None:
    path = _find_csv_in_zip(zf, "sessions_with_adjustments.csv")
    if path is None:
        return

    rows = _read_csv(zf, path)
    source = path.split("/")[-1]
    for row in rows:
        asin = (row.get("ASIN") or "").strip()
        if not asin:
            continue
        con.execute(
            """INSERT INTO reading_insight_sessions
               (asin, product_name, start_time, end_time,
                total_reading_milliseconds, source_file)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                asin,
                row.get("Product Name"),
                _parse_timestamp(row.get("Start Time")),
                _parse_timestamp(row.get("End Time")),
                _parse_bigint(row.get("Total Reading Milliseconds")),
                source,
            ],
        )


def _import_personal_documents(con: duckdb.DuckDBPyConnection, zf: zipfile.ZipFile) -> None:
    path = _find_csv_in_zip(zf, "DocumentMetadata.csv")
    if path is None:
        return

    rows = _read_csv(zf, path)
    source = path.split("/")[-1]
    for row in rows:
        deleted = (row.get("HasBeenDeleted") or "").strip()
        if deleted == "Yes":
            continue
        doc_id = (row.get("Document ID") or "").strip()
        if not doc_id:
            continue
        con.execute(
            """INSERT INTO personal_documents
               (document_id, title, document_provider, filename,
                document_original_type, document_size_in_bytes,
                entry_creation_date, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                doc_id,
                row.get("Title"),
                row.get("Document Provider"),
                row.get("Filename"),
                row.get("Document Original Type"),
                _parse_bigint(row.get("Document Size In Bytes")),
                _parse_timestamp(row.get("Entry Creation Date")),
                source,
            ],
        )


def import_kindle_zip(zip_path: str | Path, db_path: str | Path) -> dict:
    zip_path = Path(zip_path)
    db_path = Path(db_path)

    if not zip_path.exists():
        raise FileNotFoundError(f"Zip file not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid zip file: {zip_path}")

    tmp_dir = tempfile.mkdtemp()
    tmp_db = Path(tmp_dir) / "kindle_tmp.duckdb"

    try:
        con = connect(tmp_db)
        create_schema(con)

        with zipfile.ZipFile(zip_path, "r") as zf:
            books_count = _import_books(con, zf)
            _import_authors(con, zf)
            _import_genres(con, zf)
            _import_images(con, zf)
            reading_sessions_count = _import_reading_sessions(con, zf)
            _import_reading_insight_sessions(con, zf)
            _import_personal_documents(con, zf)

        # Singleton: keep exactly one row keyed on import_id=1
        con.execute("DELETE FROM import_metadata")
        con.execute(
            """INSERT INTO import_metadata
               (import_id, source_path, source_type, imported_at, books_count, reading_sessions_count)
               VALUES (1, ?, ?, current_timestamp, ?, ?)""",
            [str(zip_path), "kindle_zip", books_count, reading_sessions_count],
        )
        con.close()

        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_db), str(db_path))

        # Remove any WAL sitting next to the previous db_path. It was
        # written for the old DB and has no relation to the just-swapped
        # file; leaving it would confuse DuckDB on the next open.
        # (tmp-side WAL, if any, is cleaned up by the tmp_dir rmtree in
        # the finally block below.)
        orphan_wal = wal_path(db_path)
        orphan_wal.unlink(missing_ok=True)

        return {
            "books_count": books_count,
            "reading_sessions_count": reading_sessions_count,
            "db_path": str(db_path),
        }
    except Exception:
        # On failure, leave existing DB intact
        if tmp_db.exists():
            tmp_db.unlink()
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
