"""Create a minimal Kindle.zip fixture for tests."""

import csv
import io
import zipfile
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def create_kindle_zip(path: Path | None = None) -> Path:
    if path is None:
        path = FIXTURE_DIR / "Kindle.zip"
    path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "w") as zf:
        # --- CustomerRelationshipIndex_FE.csv ---
        # Contains: valid items, sample, recommendation, deleted, author entry, missing ASIN
        books_rows = [
            {
                "ASIN": "B000TEST01",
                "Product Name": "テストの本",
                "Sortable Title": "テストノホン",
                "Sortable Author Name": "山田太郎",
                "Series Title": "テストシリーズ",
                "Series Author": "山田太郎",
                "Position In Collection": "1",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-15T10:30:00.000Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            },
            {
                "ASIN": "B000TEST02",
                "Product Name": "Another Book",
                "Sortable Title": "Another Book",
                "Sortable Author Name": "John Smith",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "US",
                "Relationship Creation Date": "2024-03-20T08:00:00Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "No",
            },
            {
                "ASIN": "B000TEST03",
                "Product Name": "シリーズ第2巻",
                "Sortable Title": "シリーズダイ2カン",
                "Sortable Author Name": "山田太郎",
                "Series Title": "テストシリーズ",
                "Series Author": "山田太郎",
                "Position In Collection": "2",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-02-10T12:00:00.000Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            },
            # --- Filtered out rows ---
            {
                "ASIN": "B000SAMPLE",
                "Product Name": "Sample Book",
                "Sortable Title": "Sample Book",
                "Sortable Author Name": "Author",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "SAMPLE",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            },
            {
                "ASIN": "B000RECOM1",
                "Product Name": "Recommended",
                "Sortable Title": "Recommended",
                "Sortable Author Name": "Author",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Recommendation",
                "Deleted By Customer": "",
            },
            {
                "ASIN": "B000DELETE",
                "Product Name": "Deleted Book",
                "Sortable Title": "Deleted Book",
                "Sortable Author Name": "Author",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "JP",
                "Relationship Creation Date": "2024-01-01T00:00:00Z",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "Yes",
            },
            {
                "ASIN": "",
                "Product Name": "No ASIN",
                "Sortable Title": "",
                "Sortable Author Name": "",
                "Series Title": "",
                "Series Author": "",
                "Position In Collection": "",
                "Marketplace": "",
                "Relationship Creation Date": "",
                "Resource Type": "ITEM",
                "Ownership Type": "Item Owner",
                "Deleted By Customer": "",
            },
        ]
        _write_csv(zf, "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv", books_rows)

        # --- CustomerAuthorNameRelationship_FE.csv ---
        # B000TEST01 has 2 authors, B000TEST02 has 1
        author_rows = [
            {"ASIN": "B000TEST01", "Author Name": "山田太郎"},
            {"ASIN": "B000TEST01", "Author Name": "佐藤花子"},
            {"ASIN": "B000TEST02", "Author Name": "John Smith"},
            {"ASIN": "B000TEST03", "Author Name": "山田太郎"},
            {"ASIN": "", "Author Name": "No ASIN Author"},
        ]
        _write_csv(zf, "Kindle.UnifiedLibraryIndex/CustomerAuthorNameRelationship_FE.csv", author_rows)

        # --- CustomerGenres_FE.csv ---
        genre_rows = [
            {"ASIN": "B000TEST01", "Genre": "文学・評論"},
            {"ASIN": "B000TEST01", "Genre": "コミック"},
            {"ASIN": "B000TEST02", "Genre": "Science Fiction"},
            {"ASIN": "B000TEST03", "Genre": "文学・評論"},
        ]
        _write_csv(zf, "Kindle.UnifiedLibraryIndex/CustomerGenres_FE.csv", genre_rows)

        # --- CustomerTags_FE.csv ---
        # Image URL extraction; includes duplicate for same ASIN
        tag_rows = [
            {"ASIN": "B000TEST01", "Image URL": "https://images.example.com/B000TEST01_b.jpg", "Tag Name": "tag1"},
            {"ASIN": "B000TEST01", "Image URL": "https://images.example.com/B000TEST01_a.jpg", "Tag Name": "tag2"},
            {"ASIN": "B000TEST02", "Image URL": "https://images.example.com/B000TEST02.jpg", "Tag Name": "tag1"},
            {"ASIN": "B000TEST03", "Image URL": "", "Tag Name": "tag1"},
        ]
        _write_csv(zf, "Kindle.UnifiedLibraryIndex/CustomerTags_FE.csv", tag_rows)

        # --- Kindle.Devices.ReadingSession.csv ---
        # 実データは snake_case のヘッダで出力される
        session_rows = [
            {
                "ASIN": "B000TEST01",
                "start_timestamp": "2024-06-01T09:00:00.000Z",
                "end_timestamp": "2024-06-01T09:30:00.000Z",
                "content_type": "E-Book",
                "total_reading_millis": "1800000",
                "number_of_page_flips": "45",
            },
            {
                "ASIN": "B000TEST01",
                "start_timestamp": "2024-06-02T20:00:00.000Z",
                "end_timestamp": "2024-06-02T20:45:00.000Z",
                "content_type": "E-Book",
                "total_reading_millis": "2700000",
                "number_of_page_flips": "60",
            },
            {
                "ASIN": "B000TEST02",
                "start_timestamp": "2024-07-01T12:00:00Z",
                "end_timestamp": "2024-07-01T12:15:00Z",
                "content_type": "E-Book",
                "total_reading_millis": "900000",
                "number_of_page_flips": "20",
            },
        ]
        _write_csv(zf, "Kindle.Devices.ReadingSession/Kindle.Devices.ReadingSession.csv", session_rows)

        # --- sessions_with_adjustments.csv ---
        # 実データは snake_case のヘッダで出力される
        insight_rows = [
            {
                "ASIN": "B000TEST01",
                "product_name": "テストの本",
                "start_time": "2024-06-01T09:00:00.000Z",
                "end_time": "2024-06-01T09:30:00.000Z",
                "total_reading_milliseconds": "1800000",
            },
        ]
        _write_csv(zf, "Kindle.ReadingInsights/sessions_with_adjustments.csv", insight_rows)

        # --- DocumentMetadata.csv ---
        # 実データは PascalCase(空白なし)のヘッダで出力される
        doc_rows = [
            {
                "DocumentId": "DOC001",
                "Title": "My Notes",
                "DocumentProvider": "User",
                "Filename": "notes.pdf",
                "DocumentOriginalType": "application/pdf",
                "DocumentSizeInBytes": "102400",
                "EntryCreationDate": "2024-05-01T10:00:00Z",
                "HasBeenDeleted": "",
            },
            {
                "DocumentId": "DOC002",
                "Title": "Deleted Doc",
                "DocumentProvider": "User",
                "Filename": "deleted.pdf",
                "DocumentOriginalType": "application/pdf",
                "DocumentSizeInBytes": "51200",
                "EntryCreationDate": "2024-04-01T10:00:00Z",
                "HasBeenDeleted": "Yes",
            },
        ]
        _write_csv(zf, "Kindle.KindleDocs/DocumentMetadata.csv", doc_rows)

    return path


def _write_csv(zf: zipfile.ZipFile, name: str, rows: list[dict]) -> None:
    if not rows:
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(name, buf.getvalue())


if __name__ == "__main__":
    p = create_kindle_zip()
    print(f"Created: {p}")
