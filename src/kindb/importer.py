"""Import kindle.json into DuckDB."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from kindb.db import connect, create_schema, wal_path

REQUIRED_KEYS = {"title", "authors", "acquiredTime", "readStatus", "asin"}
KNOWN_KEYS = REQUIRED_KEYS | {"productImage"}
MAX_ACQUIRED_TIME_MS = 4102444800000

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
        errors.append(
            f"{label}: 'acquiredTime' must satisfy 0 <= acquiredTime < {MAX_ACQUIRED_TIME_MS}"
        )


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
        book_values.append((
            asin,
            row["title"],
            authors_text,
            _acquired_time_to_datetime(row["acquiredTime"]),
            row["readStatus"],
            _normalize_product_image(row.get("productImage")),
            imported_at,
        ))
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

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=db_path.parent))
    tmp_db = tmp_dir / "kindle_tmp.duckdb"
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        with closing(connect(tmp_db)) as con:
            create_schema(con)
            _insert_rows(con, rows, imported_at)
            con.execute(
                """INSERT INTO import_metadata
                   (source_path, source_type, books_count, imported_at)
                   VALUES (?, ?, ?, ?)""",
                [str(resolved_source), "kindle_json", len(rows), imported_at],
            )

        os.replace(tmp_db, db_path)
        wal_path(db_path).unlink(missing_ok=True)

        return {
            "books_count": len(rows),
            "db_path": str(db_path),
            "source_path": str(resolved_source),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
