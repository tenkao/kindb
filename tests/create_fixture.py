"""Create a minimal kindle.json fixture for tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def kindle_json_rows() -> list[dict[str, Any]]:
    return [
        {
            "title": "テストの本",
            "authors": "山田太郎, 佐藤花子",
            "acquiredTime": 1705314600000,
            "readStatus": "READ",
            "asin": "B000TEST01",
            "productImage": "https://images.example.com/B000TEST01.jpg",
        },
        {
            "title": "Another Book",
            "authors": "John Smith, Jane Doe, Alice Brown",
            "acquiredTime": 1710921600000,
            "readStatus": "UNKNOWN",
            "asin": "B000TEST02",
            "productImage": "https://images.example.com/B000TEST02.jpg",
        },
        {
            "title": "画像なしの本",
            "authors": "山田太郎",
            "acquiredTime": 1707547200000,
            "readStatus": "UNKNOWN",
            "asin": "B000TEST03",
        },
        {
            "title": "50% OFF_ Book",
            "authors": "Percent Author",
            "acquiredTime": 1709251200000,
            "readStatus": "READING",
            "asin": "B000TEST04",
            "productImage": "",
        },
        {
            "title": "Path A\\B Book",
            "authors": "Backslash Author",
            "acquiredTime": 1709337600000,
            "readStatus": "UNKNOWN",
            "asin": "B000TEST05",
            "productImage": None,
        },
    ]


def create_kindle_json(path: Path | None = None, rows: list[dict[str, Any]] | None = None) -> Path:
    if path is None:
        path = FIXTURE_DIR / "kindle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = kindle_json_rows() if rows is None else rows
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    p = create_kindle_json()
    print(f"Created: {p}")
