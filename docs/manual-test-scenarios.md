# kindb 手動テストシナリオ

kindb v0.3 を実際のターミナルで目視確認するためのシナリオ。主入力は `kindle.json`。任意で公式 `Kindle.zip` を追加取り込みし、ジャンル・シリーズ・Amazon 著者 ID を補完する。

## 0. 準備

```bash
cd /path/to/kindb
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

export TEST_DB=/tmp/kindb_manual/test.duckdb
rm -rf /tmp/kindb_manual && mkdir -p /tmp/kindb_manual

python -m tests.create_fixture
python - <<'PY'
from pathlib import Path
from tests.create_official_fixture import create_official_zip
create_official_zip(Path("/tmp/kindb_manual/Kindle.zip"))
PY
ls tests/fixtures/kindle.json
ls /tmp/kindb_manual/Kindle.zip

ruff check . && pytest -q
```

期待:
- `kindb --help` に `import`, `import-official`, `status`, `search`, `query`, `authors`, `recent`, `delete` が表示される。
- `genres`, `series`, `reading` は表示されない。

## 1. import

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待:
- `Import complete: 5 books`
- `$TEST_DB` が作成される。
- `$TEST_DB.wal` は残らない。

再 import:

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
```

期待: エラーなく成功し、既存 DB が置換される。

異常系:

```bash
echo "{bad" > /tmp/kindb_manual/bad.json
kindb import /tmp/kindb_manual/bad.json --db "$TEST_DB"; echo "exit=$?"

kindb import /tmp/does_not_exist.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/missing_title.json <<'JSON'
[{"title":"","authors":"Author","acquiredTime":1704067200000,"readStatus":"UNKNOWN","asin":"B000BAD01"}]
JSON
kindb import /tmp/kindb_manual/missing_title.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/duplicate_asin.json <<'JSON'
[
  {"title":"One","authors":"Author","acquiredTime":1704067200000,"readStatus":"UNKNOWN","asin":"B000DUP01"},
  {"title":"Two","authors":"Author","acquiredTime":1704067200000,"readStatus":"UNKNOWN","asin":"B000DUP01"}
]
JSON
kindb import /tmp/kindb_manual/duplicate_asin.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/bad_type_float.json <<'JSON'
[{"title":"Bad Time","authors":"Author","acquiredTime":1.5,"readStatus":"UNKNOWN","asin":"B000BAD02"}]
JSON
kindb import /tmp/kindb_manual/bad_type_float.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/bad_type_bool.json <<'JSON'
[{"title":"Bad Time","authors":"Author","acquiredTime":true,"readStatus":"UNKNOWN","asin":"B000BAD03"}]
JSON
kindb import /tmp/kindb_manual/bad_type_bool.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/not_array.json <<'JSON'
{"not":"array"}
JSON
kindb import /tmp/kindb_manual/not_array.json --db "$TEST_DB"; echo "exit=$?"

cat > /tmp/kindb_manual/empty.json <<'JSON'
[]
JSON
kindb import /tmp/kindb_manual/empty.json --db /tmp/kindb_manual/empty.duckdb
kindb status --db /tmp/kindb_manual/empty.duckdb

kindb status --db "$TEST_DB"
```

期待:
- 不正 JSON、存在しないファイル、必須キー欠落、ASIN 重複、型不正、ルート非配列は終了コード 1。
- エラーメッセージに該当 ASIN または index と原因が表示される。
- 空配列は `Import complete: 0 books` で成功する。
- 異常系の後も `kindb status --db "$TEST_DB"` が成功し、既存 DB は無傷。

WAL 除去:

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
echo "fake-wal" > "$TEST_DB.wal"
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待: `$TEST_DB` は存在し、`$TEST_DB.wal` は存在しない。

未知キー警告:

```bash
python - <<'PY'
import json
from pathlib import Path
p = Path("/tmp/kindb_manual/unknown.json")
p.write_text(json.dumps([{
  "title": "Unknown Key",
  "authors": "Author",
  "acquiredTime": 1704067200000,
  "readStatus": "UNKNOWN",
  "asin": "B000WARN1",
  "extra": "ignored"
}], ensure_ascii=False), encoding="utf-8")
PY
kindb import /tmp/kindb_manual/unknown.json --db /tmp/kindb_manual/unknown.duckdb
```

期待: stderr に `Warning:` が出るが import は成功する。

## 1.5 official zip import

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
kindb import-official /tmp/kindb_manual/Kindle.zip --db "$TEST_DB"
kindb status --db "$TEST_DB"
```

期待:
- `Official import complete`
- `Genres: 4`
- `Series: 2`
- `Author IDs: 3`
- `Author names: 4`
- `Official ASIN: 4`
- `status` に `Official import`, `Official source`, `Genres (rows)`, `Series (rows)`, `Author IDs (rows)`, `Author names (rows)`, `Official ASIN (uniq)` が表示される。

zip だけを先に取り込めること:

```bash
kindb delete --yes --db /tmp/kindb_manual/official_only.duckdb 2>/dev/null || true
kindb import-official /tmp/kindb_manual/Kindle.zip --db /tmp/kindb_manual/official_only.duckdb
kindb query "SELECT count(*) AS n FROM book_genres" --db /tmp/kindb_manual/official_only.duckdb
kindb query "SELECT count(*) AS n FROM v_book_genres" --db /tmp/kindb_manual/official_only.duckdb
```

期待:
- `book_genres` は 4 行。
- `books` が空なので `v_book_genres` は 0 行。

必須ファイル欠落:

```bash
python - <<'PY'
from pathlib import Path
import zipfile

src = Path("/tmp/kindb_manual/Kindle.zip")
dst = Path("/tmp/kindb_manual/Kindle_missing_author_names.zip")
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
    for name in zin.namelist():
        if "CustomerAuthorNameRelationship_FE" not in name:
            zout.writestr(name, zin.read(name))
PY
kindb import-official /tmp/kindb_manual/Kindle_missing_author_names.zip --db "$TEST_DB"; echo "exit=$?"
```

期待:
- `exit=1`
- エラーメッセージに `CustomerAuthorNameRelationship_FE` が含まれる。
- 既存の official import データは残る。

ヘッダ不一致:

```bash
python - <<'PY'
from pathlib import Path
import zipfile

src = Path("/tmp/kindb_manual/Kindle.zip")
dst = Path("/tmp/kindb_manual/Kindle_bad_header.zip")
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
    for name in zin.namelist():
        if "CustomerGenres_FE" in name:
            zout.writestr(name, "ASIN,Bad\nB000TEST01,Fiction\n")
        else:
            zout.writestr(name, zin.read(name))
PY
kindb import-official /tmp/kindb_manual/Kindle_bad_header.zip --db "$TEST_DB"; echo "exit=$?"
kindb query "SELECT count(*) AS n FROM book_genres" --db "$TEST_DB"
```

期待:
- `exit=1`
- エラーメッセージに `Genre` が含まれる。
- `book_genres` は壊れず 4 行のまま。

import の独立性:

```bash
kindb query "SELECT count(*) AS n FROM book_genres" --db "$TEST_DB"
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
kindb query "SELECT count(*) AS n FROM book_genres" --db "$TEST_DB"

kindb query "SELECT count(*) AS n FROM books" --db "$TEST_DB"
kindb import-official /tmp/kindb_manual/Kindle.zip --db "$TEST_DB"
kindb query "SELECT count(*) AS n FROM books" --db "$TEST_DB"
```

期待:
- `kindle.json` 再 import 後も `book_genres` は 4 行。
- `import-official` 後も `books` は 5 行。

## 2. status

```bash
kindb status --db "$TEST_DB"
```

期待:
- Books: 5
- Authors: 分割後 unique 件数
- `Read status: READ`, `Read status: READING`, `Read status: UNKNOWN`
- With image URL
- Source は `kindle.json` の絶対パス
- official zip 取り込み済みなら `Official import` 以降の行が表示される。

## 3. search

```bash
kindb search テスト --db "$TEST_DB"
kindb search 山田 --db "$TEST_DB"
kindb search B000TEST02 --db "$TEST_DB"
kindb search READING --db "$TEST_DB"
kindb search ZZZZZ --db "$TEST_DB"
```

期待:
- title / authors_text / asin / read_status で検索できる。
- ヒットなしは `No results found.` で終了コード 0。
- 表示順は `title ASC, asin ASC` で安定している。

ワイルドカードエスケープ:

```bash
kindb search "50%" --db "$TEST_DB"
kindb search "OFF_" --db "$TEST_DB"
kindb search "A\\B" --db "$TEST_DB"
```

期待: `%`, `_`, `\` は ILIKE ワイルドカードではなく文字として扱われる。

## 4. query

```bash
kindb query "SELECT count(*) AS n FROM books" --db "$TEST_DB"
kindb query --table "SELECT asin, title, read_status FROM v_books ORDER BY asin LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT asin, title, read_status FROM v_books ORDER BY asin" --db "$TEST_DB"; echo "exit=$?"
kindb query --allow-unlimited --table "SELECT asin, title, read_status FROM v_books ORDER BY asin" --db "$TEST_DB"
kindb query "DELETE FROM books" --db "$TEST_DB"; echo "exit=$?"
kindb query "SELECT 1; UPDATE books SET title='hacked' WHERE asin='B000TEST01'" --db "$TEST_DB"; echo "exit=$?"
```

期待:
- `count(*)` と `LIMIT/OFFSET` 付き SELECT は JSON または table で表示される。
- 行返却 SELECT は `LIMIT` なしでは拒否されて `exit=1` になり、`--allow-unlimited` 付きなら実行できる。
- 書き込み系 SQL は拒否される。
- 先頭 SELECT の複文書き込みも単一文チェックで拒否されて `exit=1` になり、DB は変わらない。

## 5. authors

```bash
kindb authors --db "$TEST_DB"
```

期待:
- `book_count DESC, author_name ASC` で表示される。
- 同冊数時の並びが安定している。

## 6. recent

```bash
kindb recent --db "$TEST_DB"
kindb recent -n 1 --db "$TEST_DB"
```

期待:
- `acquired_at DESC, asin DESC`。
- 表紙 URL と `read_status` が表示される。
- `-n 1` では 1 冊だけ表示される。

## 7. delete

確認プロンプト (`--yes` なし):

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
echo "n" | kindb delete --db "$TEST_DB"; echo "exit=$?"
ls -la "$TEST_DB"

echo "y" | kindb delete --db "$TEST_DB"; echo "exit=$?"
ls -la "$TEST_DB"* 2>/dev/null; echo "ls exit=$?"
```

期待:
- `n` 入力ではキャンセルされ、DB は残る。
- `y` 入力で削除される。

`--yes` スキップ + WAL 除去:

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
echo "fake-wal" > "$TEST_DB.wal"
kindb delete --yes --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待:
- 確認プロンプトが出ずに削除される。
- DB 本体と `<db_path>.wal` が両方削除される。

## 8. ビュー確認

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
kindb import-official /tmp/kindb_manual/Kindle.zip --db "$TEST_DB"
kindb query --table "SHOW TABLES" --db "$TEST_DB"
kindb query --table "DESCRIBE v_books" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_books ORDER BY asin LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_author_counts ORDER BY book_count DESC, author_name ASC LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_genre_counts ORDER BY book_count DESC, genre ASC LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_series_counts ORDER BY book_count DESC, series_title ASC LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_author_id_counts ORDER BY book_count DESC, author_name ASC, author_id ASC LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_book_authors_official ORDER BY asin ASC, author_order ASC LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "
  SELECT b.asin, b.authors AS v_books_authors,
         list(ba.author_name ORDER BY ba.author_order) AS expected_authors,
         b.authors_text
  FROM v_books b
  JOIN book_authors ba USING (asin)
  GROUP BY b.asin, b.authors, b.authors_text
  HAVING len(b.authors) >= 2
  ORDER BY b.asin
  LIMIT 20 OFFSET 0
" --db "$TEST_DB"
```

期待:
- `SHOW TABLES`: `books`, `book_authors`, `import_metadata` に加え、`book_genres`, `book_series`, `book_author_ids`, `book_author_names`, `import_metadata_official` と v0.3 の view 群が表示される。
- `DESCRIBE v_books`: `genres`, `series_title`, `series_asin`, `series_position`, `author_ids`, `author_names_official` が表示される。
- `SELECT * FROM v_books`: 1 ASIN 1 行で並び、`authors` 配列・`authors_text`・`product_image_url`・`read_status`・`acquired_at` に加え、`genres`, `series_title`, `series_asin`, `series_position`, `author_ids`, `author_names_official` が表示される。
- `SELECT * FROM v_author_counts`: `book_count DESC, author_name ASC` で並ぶ。
- `SELECT * FROM v_genre_counts`: `Fiction` が 2 冊、`Fantasy` が 1 冊で表示される。
- `SELECT * FROM v_series_counts`: `Series Alpha` と `Series Without Asin` が表示される。
- `SELECT * FROM v_author_id_counts`: `Same Name` が別 `author_id` で別行として表示される。
- `SELECT * FROM v_book_authors_official`: `B000TEST04` の `Name Only` 行は `author_id` が NULL。
- 著者順検証クエリ: 全行で `v_books_authors = expected_authors`、かつ `authors_text` を `, ` で分割した順と一致する。

zip 未取り込み時の `v_books`:

```bash
kindb import tests/fixtures/kindle.json --db /tmp/kindb_manual/no_official.duckdb
kindb query --table "
  SELECT asin, genres, series_title, series_asin, series_position, author_ids, author_names_official
  FROM v_books
  ORDER BY asin
  LIMIT 20 OFFSET 0
" --db /tmp/kindb_manual/no_official.duckdb
```

期待:
- `genres`, `author_ids`, `author_names_official` は空配列 `[]`。
- `series_title`, `series_asin`, `series_position` は NULL。

sentinel / 除外確認:

```bash
kindb query --table "
  SELECT asin, series_title, series_asin, series_position
  FROM v_books
  WHERE asin IN ('B000TEST01', 'B000TEST02')
  ORDER BY asin
  LIMIT 20 OFFSET 0
" --db "$TEST_DB"

kindb query "SELECT count(*) AS n FROM book_genres WHERE asin = 'B000DEL001'" --db "$TEST_DB"
kindb query "SELECT count(*) AS n FROM book_genres WHERE asin = 'Not Available'" --db "$TEST_DB"
kindb query "SELECT count(*) AS n FROM v_book_genres WHERE asin = 'B000ZIP001'" --db "$TEST_DB"
```

期待:
- `B000TEST01`: `series_title = Series Alpha`, `series_asin = B07D4FP6XQ`, `series_position = 1`
- `B000TEST02`: `series_title = Series Without Asin`, `series_asin = NULL`, `series_position = NULL`
- `Deleted By Customer = Yes` の `B000DEL001` は 0 行。
- `ASIN = Not Available` は 0 行。
- zip にしかない `B000ZIP001` は raw table には残るが、`v_book_genres` では 0 行。

## 9. v0.2 DB マイグレーション

新テーブル/view が無い DB を作って、読み取り CLI が自動で schema を更新することを確認する。

```bash
python - <<'PY'
from pathlib import Path
from kindb.db import connect

db = Path("/tmp/kindb_manual/v02.duckdb")
db.unlink(missing_ok=True)
con = connect(db)
try:
    con.execute("""
        CREATE TABLE books (
            asin VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            authors_text VARCHAR NOT NULL,
            acquired_at TIMESTAMP NOT NULL,
            read_status VARCHAR NOT NULL,
            product_image_url VARCHAR,
            imported_at TIMESTAMP NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE book_authors (
            asin VARCHAR NOT NULL,
            author_name VARCHAR NOT NULL,
            author_order INTEGER NOT NULL,
            PRIMARY KEY (asin, author_order)
        )
    """)
    con.execute("""
        CREATE TABLE import_metadata (
            source_path VARCHAR,
            source_type VARCHAR,
            books_count INTEGER,
            imported_at TIMESTAMP
        )
    """)
finally:
    con.close()
PY

kindb status --db /tmp/kindb_manual/v02.duckdb
kindb query --table "SHOW TABLES" --db /tmp/kindb_manual/v02.duckdb
kindb query --table "DESCRIBE v_books" --db /tmp/kindb_manual/v02.duckdb
```

期待:
- `status` がエラーにならない。
- `SHOW TABLES` に v0.3 の新テーブル/view が追加される。
- `DESCRIBE v_books` に `genres`, `series_title`, `series_asin`, `series_position`, `author_ids`, `author_names_official` が含まれる。

## 10. 後片付け

```bash
rm -rf /tmp/kindb_manual
unset TEST_DB
```

期待:
- `/tmp/kindb_manual` 配下のテスト用 DB / fixture / WAL が全て削除される。
- `$TEST_DB` 環境変数が解除され、以降のうっかり操作で本番 DB に流れない。
- 本番 DB (`~/.kindb/kindle.duckdb`) には一切触れていないこと（シナリオ中は常に `--db "$TEST_DB"` を指定する前提）。
