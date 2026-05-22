"""Import kindle.json and official Kindle.zip data into DuckDB."""

from __future__ import annotations

import csv
import json
import re
import tempfile
import zipfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from kindb.db import connect, create_schema

REQUIRED_KEYS = {"title", "authors", "acquiredTime", "readStatus", "asin"}
KNOWN_KEYS = REQUIRED_KEYS | {"productImage"}
MAX_ACQUIRED_TIME_MS = 4102444800000
SERIES_SUFFIX = re.compile(r"^(.+) (B[0-9A-Z]{9})$")

OFFICIAL_DATASETS = {
    "genres": "Kindle.UnifiedLibraryIndex.CustomerGenres_FE",
    "relationships": "Kindle.UnifiedLibraryIndex.CustomerRelationshipIndex_FE",
    "author_ids": "Kindle.UnifiedLibraryIndex.CustomerAuthorIdRelationship_FE",
    "author_names": "Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship_FE",
}
OFFICIAL_REQUIRED_COLUMNS = {
    "genres": {"ASIN", "Genre"},
    "relationships": {
        "ASIN",
        "Series Title",
        "Series Author",
        "Position In Collection",
        "Relation Type",
        "Deleted By Customer",
    },
    "author_ids": {"ASIN", "Author ID"},
    "author_names": {"ASIN", "Author Name"},
}

WarningHandler = Callable[[str], None]


def _acquired_time_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(tzinfo=None)


def _item_label(item: Any, index: int) -> str:
    if isinstance(item, dict):
        asin = item.get("asin")
        if isinstance(asin, str) and asin.strip():
            return f"ASIN {asin}"
    return f"index {index}"


def _is_blank_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() == ""


def _validate_required(item: dict[str, Any], index: int, errors: list[str]) -> None:
    label = _item_label(item, index)
    for key in sorted(REQUIRED_KEYS):
        if key not in item or item[key] is None or _is_blank_string(item[key]):
            errors.append(f"{label}: missing or empty required key '{key}'")


def _validate_types(item: dict[str, Any], index: int, errors: list[str]) -> None:
    label = _item_label(item, index)
    for key in ("title", "authors", "readStatus", "asin"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            errors.append(f"{label}: '{key}' must be str")

    if "productImage" in item and item["productImage"] is not None and not isinstance(item["productImage"], str):
        errors.append(f"{label}: 'productImage' must be str or null")

    if "acquiredTime" not in item or item["acquiredTime"] is None:
        return
    acquired_time = item["acquiredTime"]
    if isinstance(acquired_time, bool) or not isinstance(acquired_time, int):
        errors.append(f"{label}: 'acquiredTime' must be int epoch milliseconds")
        return
    if not 0 <= acquired_time < MAX_ACQUIRED_TIME_MS:
        errors.append(f"{label}: 'acquiredTime' must satisfy 0 <= acquiredTime < {MAX_ACQUIRED_TIME_MS}")


def _validate_payload(data: Any, warn: WarningHandler | None = None) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError("kindle.json root must be an array")

    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    asins: list[str] = []

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"index {index}: each item must be an object")
            continue

        unknown_keys = sorted(set(item) - KNOWN_KEYS)
        if unknown_keys and warn is not None:
            warn(f"{_item_label(item, index)}: ignoring unknown keys: {', '.join(unknown_keys)}")

        _validate_required(item, index, errors)
        _validate_types(item, index, errors)

        asin = item.get("asin")
        if isinstance(asin, str) and asin.strip():
            asins.append(asin)
        rows.append(item)

    duplicates = sorted(asin for asin, count in Counter(asins).items() if count > 1)
    if duplicates:
        errors.append(f"Duplicate ASIN values: {', '.join(duplicates)}")

    if errors:
        raise ValueError("; ".join(errors))

    return rows


def _normalize_product_image(value: Any) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _author_values(asin: str, authors_text: str) -> list[tuple[str, str, int]]:
    authors = [part.strip() for part in authors_text.split(", ")]
    return [
        (asin, author, order)
        for order, author in enumerate((author for author in authors if author), start=1)
    ]


def _load_json(json_path: Path) -> Any:
    try:
        with json_path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e


def _insert_rows(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]], imported_at: datetime) -> None:
    book_values = []
    author_values = []
    for row in rows:
        asin = row["asin"]
        authors_text = row["authors"]
        book_values.append(
            (
                asin,
                row["title"],
                authors_text,
                _acquired_time_to_datetime(row["acquiredTime"]),
                row["readStatus"],
                _normalize_product_image(row.get("productImage")),
                imported_at,
            )
        )
        author_values.extend(_author_values(asin, authors_text))

    if book_values:
        con.executemany(
            """INSERT INTO books
               (asin, title, authors_text, acquired_at, read_status, product_image_url, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            book_values,
        )
    if author_values:
        con.executemany(
            """INSERT INTO book_authors (asin, author_name, author_order)
               VALUES (?, ?, ?)""",
            author_values,
        )


def _rollback_quietly(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("ROLLBACK")
    except duckdb.Error:
        pass


def import_kindle_json(
    json_path: str | Path,
    db_path: str | Path,
    *,
    warn: WarningHandler | None = None,
) -> dict[str, Any]:
    json_path = Path(json_path)
    db_path = Path(db_path)

    if not json_path.exists():
        raise FileNotFoundError(f"JSON file not found: {json_path}")
    if not json_path.is_file():
        raise ValueError(f"Not a file: {json_path}")

    resolved_source = json_path.resolve()
    data = _load_json(json_path)
    rows = _validate_payload(data, warn=warn)
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)

    with closing(connect(db_path)) as con:
        create_schema(con)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM book_authors")
            con.execute("DELETE FROM import_metadata")
            _insert_rows(con, rows, imported_at)
            con.execute(
                """INSERT INTO import_metadata
                   (source_path, source_type, books_count, imported_at)
                   VALUES (?, ?, ?, ?)""",
                [str(resolved_source), "kindle_json", len(rows), imported_at],
            )
            con.execute("COMMIT")
        except Exception:
            _rollback_quietly(con)
            raise
        con.execute("CHECKPOINT")

    return {
        "books_count": len(rows),
        "db_path": str(db_path),
        "source_path": str(resolved_source),
    }


def _official_files(root: Path) -> dict[str, list[Path]]:
    files: dict[str, list[Path]] = {}
    base = root / "Kindle.UnifiedLibraryIndex" / "datasets"
    for key, dataset in OFFICIAL_DATASETS.items():
        paths = sorted((base / dataset).glob("*.csv"))
        if not paths:
            raise ValueError(f"Required official Kindle.zip file is missing: {dataset}/*.csv")
        files[key] = paths
    return files


def _read_csv_rows(path: Path, required_columns: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = sorted(required_columns - set(fieldnames))
        if missing:
            raise ValueError(
                f"CSV header mismatch in {path}: missing {', '.join(missing)}; "
                f"actual columns: {', '.join(fieldnames)}"
            )
        return list(reader)


def _valid_asin(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == "Not Available":
        return None
    return stripped


def _valid_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == "Not Available":
        return None
    return stripped


def _split_suffix(value: str | None) -> tuple[str | None, str | None]:
    text = _valid_text(value)
    if text is None:
        return None, None
    match = SERIES_SUFFIX.match(text)
    if match:
        return match.group(1), match.group(2)
    return text, None


def _parse_position(value: str | None) -> int | None:
    text = _valid_text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _deleted_asins(paths: list[Path]) -> set[str]:
    deleted: set[str] = set()
    for path in paths:
        for row in _read_csv_rows(path, OFFICIAL_REQUIRED_COLUMNS["relationships"]):
            asin = _valid_asin(row.get("ASIN"))
            if asin and row.get("Deleted By Customer", "").strip() == "Yes":
                deleted.add(asin)
    return deleted


def _parse_official_data(files: dict[str, list[Path]]) -> dict[str, Any]:
    deleted = _deleted_asins(files["relationships"])

    genres: set[tuple[str, str]] = set()
    for path in files["genres"]:
        for row in _read_csv_rows(path, OFFICIAL_REQUIRED_COLUMNS["genres"]):
            asin = _valid_asin(row.get("ASIN"))
            genre = _valid_text(row.get("Genre"))
            if asin and genre and asin not in deleted:
                genres.add((asin, genre))

    series: dict[tuple[str, str, str], tuple[str, str, str, str | None, str | None, int | None, str]] = {}
    for path in files["relationships"]:
        for row in _read_csv_rows(path, OFFICIAL_REQUIRED_COLUMNS["relationships"]):
            asin = _valid_asin(row.get("ASIN"))
            if not asin or asin in deleted or row.get("Deleted By Customer", "").strip() == "Yes":
                continue
            series_title, series_asin = _split_suffix(row.get("Series Title"))
            if series_title is None:
                continue
            series_author, series_author_id = _split_suffix(row.get("Series Author"))
            relation_type = _valid_text(row.get("Relation Type")) or ""
            if not relation_type:
                continue
            stored_series_asin = series_asin or ""
            key = (asin, stored_series_asin, relation_type)
            series.setdefault(
                key,
                (
                    asin,
                    stored_series_asin,
                    series_title,
                    series_author,
                    series_author_id,
                    _parse_position(row.get("Position In Collection")),
                    relation_type,
                ),
            )

    author_ids_seen: dict[str, set[str]] = {}
    author_ids: dict[str, list[str]] = {}
    for path in files["author_ids"]:
        for row in _read_csv_rows(path, OFFICIAL_REQUIRED_COLUMNS["author_ids"]):
            asin = _valid_asin(row.get("ASIN"))
            author_id = _valid_text(row.get("Author ID"))
            if not asin or not author_id or asin in deleted:
                continue
            seen = author_ids_seen.setdefault(asin, set())
            if author_id in seen:
                continue
            seen.add(author_id)
            author_ids.setdefault(asin, []).append(author_id)

    author_names_seen: dict[str, set[str]] = {}
    author_names: dict[str, list[str]] = {}
    for path in files["author_names"]:
        for row in _read_csv_rows(path, OFFICIAL_REQUIRED_COLUMNS["author_names"]):
            asin = _valid_asin(row.get("ASIN"))
            author_name = _valid_text(row.get("Author Name"))
            if not asin or not author_name or asin in deleted:
                continue
            seen = author_names_seen.setdefault(asin, set())
            if author_name in seen:
                continue
            seen.add(author_name)
            author_names.setdefault(asin, []).append(author_name)

    author_id_rows = [
        (asin, author_id, order)
        for asin, values in author_ids.items()
        for order, author_id in enumerate(values, start=1)
    ]
    author_name_rows = [
        (asin, author_name, order)
        for asin, values in author_names.items()
        for order, author_name in enumerate(values, start=1)
    ]
    all_asins = (
        {asin for asin, _ in genres}
        | {row[0] for row in series.values()}
        | {asin for asin, _, _ in author_id_rows}
        | {asin for asin, _, _ in author_name_rows}
    )

    return {
        "genres": sorted(genres),
        "series": list(series.values()),
        "author_ids": author_id_rows,
        "author_names": author_name_rows,
        "distinct_asin_count": len(all_asins),
    }


def _insert_official_data(
    con: duckdb.DuckDBPyConnection,
    data: dict[str, Any],
    source_path: Path,
    imported_at: datetime,
) -> None:
    con.execute("DELETE FROM book_genres")
    con.execute("DELETE FROM book_series")
    con.execute("DELETE FROM book_author_ids")
    con.execute("DELETE FROM book_author_names")
    con.execute("DELETE FROM import_metadata_official")

    if data["genres"]:
        con.executemany("INSERT INTO book_genres (asin, genre) VALUES (?, ?)", data["genres"])
    if data["series"]:
        con.executemany(
            """INSERT INTO book_series
               (asin, series_asin, series_title, series_author, series_author_id,
                position_in_collection, relation_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            data["series"],
        )
    if data["author_ids"]:
        con.executemany(
            "INSERT INTO book_author_ids (asin, author_id, author_order) VALUES (?, ?, ?)",
            data["author_ids"],
        )
    if data["author_names"]:
        con.executemany(
            "INSERT INTO book_author_names (asin, author_name, author_order) VALUES (?, ?, ?)",
            data["author_names"],
        )

    con.execute(
        """INSERT INTO import_metadata_official
           (source_path, source_type, genres_count, series_count, author_ids_count,
            author_names_count, distinct_asin_count, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            str(source_path),
            "kindle_zip",
            len(data["genres"]),
            len(data["series"]),
            len(data["author_ids"]),
            len(data["author_names"]),
            data["distinct_asin_count"],
            imported_at,
        ],
    )


def import_official_zip(zip_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    zip_path = Path(zip_path)
    db_path = Path(db_path)

    if not zip_path.exists():
        raise FileNotFoundError(f"Kindle.zip file not found: {zip_path}")
    if not zip_path.is_file():
        raise ValueError(f"Not a file: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a zip file: {zip_path}")

    resolved_source = zip_path.resolve()
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(root)
        data = _parse_official_data(_official_files(root))

    with closing(connect(db_path)) as con:
        create_schema(con)
        con.execute("BEGIN TRANSACTION")
        try:
            _insert_official_data(con, data, resolved_source, imported_at)
            con.execute("COMMIT")
        except Exception:
            _rollback_quietly(con)
            raise
        con.execute("CHECKPOINT")

    return {
        "db_path": str(db_path),
        "source_path": str(resolved_source),
        "genres_count": len(data["genres"]),
        "series_count": len(data["series"]),
        "author_ids_count": len(data["author_ids"]),
        "author_names_count": len(data["author_names"]),
        "distinct_asin_count": data["distinct_asin_count"],
    }
