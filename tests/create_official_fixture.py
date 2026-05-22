"""Create a minimal official Kindle.zip fixture for tests."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

BASE = "Kindle.UnifiedLibraryIndex/datasets"
DATASETS = {
    "genres": "Kindle.UnifiedLibraryIndex.CustomerGenres_FE",
    "relationships": "Kindle.UnifiedLibraryIndex.CustomerRelationshipIndex_FE",
    "author_ids": "Kindle.UnifiedLibraryIndex.CustomerAuthorIdRelationship_FE",
    "author_names": "Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship_FE",
}


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, str]]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def create_official_zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{BASE}/{DATASETS['genres']}/part-000.csv",
            _csv_bytes(
                ["Product Name", "ASIN", "Genre"],
                [
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Genre": "Fiction"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Genre": "Fiction"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Genre": "Fantasy"},
                    {"Product Name": "Book 2", "ASIN": "B000TEST02", "Genre": "Fiction"},
                    {"Product Name": "Deleted", "ASIN": "B000DEL001", "Genre": "Hidden"},
                    {"Product Name": "Missing", "ASIN": "Not Available", "Genre": "Hidden"},
                    {"Product Name": "Zip Only", "ASIN": "B000ZIP001", "Genre": "ZipOnly"},
                ],
            ),
        )
        zf.writestr(
            f"{BASE}/{DATASETS['relationships']}/part-000.csv",
            _csv_bytes(
                [
                    "ASIN",
                    "Series Title",
                    "Series Author",
                    "Position In Collection",
                    "Relation Type",
                    "Deleted By Customer",
                ],
                [
                    {
                        "ASIN": "B000TEST01",
                        "Series Title": "Series Alpha B07D4FP6XQ",
                        "Series Author": "Author Alpha B012345678",
                        "Position In Collection": "1",
                        "Relation Type": "PRIMARY",
                        "Deleted By Customer": "No",
                    },
                    {
                        "ASIN": "B000TEST02",
                        "Series Title": "Series Without Asin",
                        "Series Author": "Not Available",
                        "Position In Collection": "Not Available",
                        "Relation Type": "PRIMARY",
                        "Deleted By Customer": "No",
                    },
                    {
                        "ASIN": "B000DEL001",
                        "Series Title": "Deleted Series B07D4FP6XQ",
                        "Series Author": "Author Alpha B012345678",
                        "Position In Collection": "2",
                        "Relation Type": "PRIMARY",
                        "Deleted By Customer": "Yes",
                    },
                ],
            ),
        )
        zf.writestr(
            f"{BASE}/{DATASETS['author_ids']}/part-000.csv",
            _csv_bytes(
                ["Product Name", "ASIN", "Author ID"],
                [
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author ID": "AID001"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author ID": "AID001"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author ID": "AID002"},
                    {"Product Name": "Book 2", "ASIN": "B000TEST02", "Author ID": "AID003"},
                    {"Product Name": "Deleted", "ASIN": "B000DEL001", "Author ID": "AID999"},
                    {"Product Name": "Missing", "ASIN": "B000TEST03", "Author ID": "Not Available"},
                ],
            ),
        )
        zf.writestr(
            f"{BASE}/{DATASETS['author_names']}/part-000.csv",
            _csv_bytes(
                ["Product Name", "ASIN", "Author Name"],
                [
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author Name": "Same Name"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author Name": "Same Name"},
                    {"Product Name": "Book 1", "ASIN": "B000TEST01", "Author Name": "Translator"},
                    {"Product Name": "Book 2", "ASIN": "B000TEST02", "Author Name": "Same Name"},
                    {"Product Name": "Book 4", "ASIN": "B000TEST04", "Author Name": "Name Only"},
                    {"Product Name": "Deleted", "ASIN": "B000DEL001", "Author Name": "Deleted"},
                ],
            ),
        )
    return path
