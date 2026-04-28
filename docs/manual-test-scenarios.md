# kindb 手動テストシナリオ

kindb v0.2 を実際のターミナルで目視確認するためのシナリオ。入力は `kindle.json` のみ。

## 0. 準備

```bash
cd /Users/tenkao/Documents/Projects/codex/kindb
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

export TEST_DB=/tmp/kindb_manual/test.duckdb
rm -rf /tmp/kindb_manual && mkdir -p /tmp/kindb_manual

python -m tests.create_fixture
ls tests/fixtures/kindle.json

ruff check . && pytest -q
```

期待:
- `kindb --help` に `import`, `status`, `search`, `query`, `authors`, `recent`, `delete` が表示される。
- `genres`, `series`, `reading` は表示されない。

## 1. import

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
find /tmp/kindb_manual -maxdepth 1 -type d -name 'tmp*' -print
```

期待:
- `Import complete: 5 books`
- `$TEST_DB` が作成される。
- `/tmp/kindb_manual/` に一時ディレクトリが残らない。

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
kindb import /tmp/kindb_manual/unknown.json --db "$TEST_DB"
```

期待: stderr に `Warning:` が出るが import は成功する。

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
kindb query --table "SELECT asin, title, read_status FROM v_books ORDER BY asin" --db "$TEST_DB"
kindb query "DELETE FROM books" --db "$TEST_DB"; echo "exit=$?"
kindb query "SELECT 1; UPDATE books SET title='hacked' WHERE asin='B000TEST01'" --db "$TEST_DB"; echo "exit=$?"
```

期待:
- SELECT は JSON または table で表示される。
- 書き込み系 SQL は拒否される。
- 先頭 SELECT の複文書き込みも read-only 接続で失敗し、DB は変わらない。

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
- `acquired_at DESC`。
- 表紙 URL と `read_status` が表示される。
- `-n 1` では 1 冊だけ表示される。

## 7. delete

```bash
echo "fake-wal" > "$TEST_DB.wal"
kindb delete --yes --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待:
- DB 本体と `<db_path>.wal` が両方削除される。

## 8. ビュー確認

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
kindb query --table "SHOW TABLES" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_books ORDER BY asin" --db "$TEST_DB"
kindb query --table "SELECT * FROM v_author_counts" --db "$TEST_DB"
```

期待:
- テーブル/ビューは `books`, `book_authors`, `import_metadata`, `v_books`, `v_author_counts` のみ。
- `v_books.authors` は `author_order` 順。
- `v_author_counts` は `book_count DESC, author_name ASC`。
