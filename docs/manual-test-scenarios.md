# kindb 手動テストシナリオ

kindb v0.4 を実際のターミナルで目視確認するためのシナリオ。主入力は `kindle.json`。任意で公式 `Kindle.zip` を追加取り込みし、ジャンル・シリーズ・Amazon 著者 ID を補完する。§8.5 と §8.6 は NDL サーチに実際に問い合わせる(合わせて数十件)。

## 0. 準備

```bash
cd /path/to/kindb
uv sync
# kindb をグローバルコマンドとして使う(依存版を uv.lock に揃える)
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --format requirements.txt -o constraints.txt \
  && uv tool install --editable . --reinstall --python 3.13 --constraints constraints.txt

export TEST_DB=/tmp/kindb_manual/test.duckdb
rm -rf /tmp/kindb_manual && mkdir -p /tmp/kindb_manual

uv run python -m tests.create_fixture
uv run python - <<'PY'
from pathlib import Path
from tests.create_official_fixture import create_official_zip
create_official_zip(Path("/tmp/kindb_manual/Kindle.zip"))
PY
ls tests/fixtures/kindle.json
ls /tmp/kindb_manual/Kindle.zip

uv run ruff check . && uv run pytest -q
kindb --help
kindb status --db "$KINDB_UNSET"; echo "exit=$?"
```

期待:
- `kindb --help` に `import`, `import-official`, `status`, `search`, `query`, `authors`, `recent`, `enrich`, `rematch`, `fix`, `delete` が表示される。
- `genres`, `series`, `reading` は表示されない。
- 設定していない変数を `--db` に渡すと、`Invalid value for '--db': must not be empty …` で `exit=2`。既定の `~/.kindb/kindle.duckdb` は開かない。以下の節を別の端末で続けるときに `TEST_DB` や `BIB_DB` を設定し忘れても、実際の蔵書 DB に対して動かない。

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

期待: エラーなく成功し、`books` が全件置き換わる。`import_metadata` は 1 行のまま増えない。

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

WAL の残留:

import は `COMMIT` 後の `CHECKPOINT` で WAL を DB 本体に書き出す。壊れた WAL が置かれていても import が成功し、WAL が残らないことを確かめる。

```bash
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
echo "fake-wal" > "$TEST_DB.wal"
kindb import tests/fixtures/kindle.json --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待: `$TEST_DB` は存在し、`$TEST_DB.wal` は存在しない。

未知キー警告:

```bash
uv run python - <<'PY'
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
uv run python - <<'PY'
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
uv run python - <<'PY'
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
- エラーメッセージに `missing Genre` と、zip 内のパス `Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerGenres_FE/part-000.csv` が含まれる(展開先の一時ディレクトリのパスは出ない)。
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
KINDB_DB_PATH="$TEST_DB" kindb status
```

期待:
- 2 つのコマンドが同じ DB の内容を表示する(`KINDB_DB_PATH` でも DB を指定できる)。
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
kindb search B000TEST -n 2 --db "$TEST_DB"
kindb search B000TEST -n 0 --db "$TEST_DB"
```

期待:
- title / authors_text / asin / read_status で検索できる。
- ヒットなしは `No results found.` で終了コード 0。
- 表示順は `title ASC, asin ASC` で安定している。
- 表に表紙 URL の列はない。
- `-n 2` では 2 件だけ表示され、最後に `Showing 2 of 5 results. Use -n 0 to show all.` と出る。
- `-n 0` では全件が表示され、最後に `Showing 5 of 5 results.` と出る。

ワイルドカードエスケープ:

```bash
kindb search "50%" --db "$TEST_DB"
kindb search "OFF_" --db "$TEST_DB"
kindb search "A\\B" --db "$TEST_DB"
```

期待: `%`, `_`, `\` は ILIKE ワイルドカードではなく文字として扱われる。

パイプ出力(Claude Code の Bash と同じ 80 桁)での長い書名と、角括弧を含む書名:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
Path("/tmp/kindb_manual/long_title.json").write_text(json.dumps([{
  "title": "ソフトウェアアーキテクチャの基礎 ―エンジニアリングに基づく体系的アプローチ",
  "authors": "Author",
  "acquiredTime": 1704067200000,
  "readStatus": "UNKNOWN",
  "asin": "B000LONG01"
}, {
  "title": "Clean Code [Paperback] [/i] :thumbs_up: ソフトウェア",
  "authors": "Author",
  "acquiredTime": 1704067300000,
  "readStatus": "UNKNOWN",
  "asin": "B000LONG02"
}], ensure_ascii=False), encoding="utf-8")
PY
kindb import /tmp/kindb_manual/long_title.json --db /tmp/kindb_manual/long_title.duckdb
env -u COLUMNS kindb search ソフトウェア --db /tmp/kindb_manual/long_title.duckdb | cat
env -u COLUMNS kindb recent --db /tmp/kindb_manual/long_title.duckdb | cat
```

期待:
- 書名が `…` で切れず、Title 列の中で複数行に折り返されて全文が表示される。
- `[Paperback]`、`[/i]`、`:thumbs_up:` が消えたり絵文字に置き換わったりせず、そのまま表示される。コマンドもエラーにならない。
- `kindb import` の `Database:` の行は、パスが長くても途中で改行されない。

## 4. query

```bash
kindb query "SELECT count(*) AS n FROM books" --db "$TEST_DB"
kindb query --table "SELECT asin, title, read_status FROM v_books ORDER BY asin LIMIT 20 OFFSET 0" --db "$TEST_DB"
kindb query --table "SELECT asin, title, read_status FROM v_books ORDER BY asin" --db "$TEST_DB"; echo "exit=$?"
kindb query --allow-unlimited --table "SELECT asin, title, read_status FROM v_books ORDER BY asin" --db "$TEST_DB"
kindb query "DELETE FROM books" --db "$TEST_DB"; echo "exit=$?"
kindb query "SELECT 1; UPDATE books SET title='hacked' WHERE asin='B000TEST01'" --db "$TEST_DB"; echo "exit=$?"
kindb query "SELECT repeat('長い書名', 40) || ' [bold]x[/bold]' AS t LIMIT 1" --db "$TEST_DB" | python3 -m json.tool
for i in 1 2 3 4; do (kindb query "SELECT count(*) AS n FROM v_books" --db "$TEST_DB" > /dev/null; echo "parallel$i exit=$?") & done; wait
rm -f /tmp/kindb_manual/ready
(uv run python -c "import duckdb, pathlib, sys, time; c = duckdb.connect(sys.argv[1], read_only=True); pathlib.Path(sys.argv[2]).touch(); time.sleep(10)" "$TEST_DB" /tmp/kindb_manual/ready &)
while [ ! -f /tmp/kindb_manual/ready ]; do sleep 0.1; done
env -u COLUMNS kindb import tests/fixtures/kindle.json --db "$TEST_DB"; echo "exit=$?"
```

期待:
- `count(*)` と `LIMIT/OFFSET` 付き SELECT は JSON または table で表示される。
- 行返却 SELECT は `LIMIT` なしでは拒否されて `exit=1` になり、`--allow-unlimited` 付きなら実行できる。
- 書き込み系 SQL は拒否される。
- 先頭 SELECT の複文書き込みも単一文チェックで拒否されて `exit=1` になり、DB は変わらない。
- 並列に実行した 4 本がすべて `exit=0` になる(スキーマが最新なら読み取り系コマンドは書き込み接続を開かない)。
- 別プロセスが DB を開いている間の import は `exit=1` になり、トレースバックではなく `Database is in use by another process` の 1 行が出る。
- パイプに流した JSON が `json.tool` で読め、値の末尾に `[bold]x[/bold]` がそのまま残る(端末幅での改行やマークアップ解釈が入らない)。

## 5. authors

```bash
kindb authors --db "$TEST_DB"
kindb authors -n 2 --db "$TEST_DB"
```

期待:
- `book_count DESC, author_name ASC` で表示される。
- 同冊数時の並びが安定している。
- 既定では最後に `Showing 7 of 7 authors.` と出る。
- `-n 2` では 2 人だけ表示され、最後に `Showing 2 of 7 authors. Use -n 0 to show all.` と出る。

## 6. recent

```bash
kindb recent --db "$TEST_DB"
kindb recent -n 1 --db "$TEST_DB"
```

期待:
- `acquired_at DESC, asin DESC`。
- `read_status` が表示され、表に表紙 URL の列はない。
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
- `SHOW TABLES`: `books`, `book_authors`, `import_metadata` に加え、`book_genres`, `book_series`, `book_author_ids`, `book_author_names`, `import_metadata_official`, `schema_meta`, `bib_*` の 9 テーブルと、`v_ndc_labels` を含む view 群が表示される。
- `DESCRIBE v_books`: `genres`, `series_title`, `series_asin`, `series_position`, `author_ids`, `author_names_official` に加え、`isbn`, `paper_issued`, `publisher`, `pages`, `bib_series`, `ndc`, `ndc_label`, `subjects`, `bib_notes`, `bib_match`, `bib_status` が表示される。
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

## 8.5 書誌情報(enrich / rematch)

NDL サーチに実際に問い合わせる。送るのは下の 4 冊の書名と著者だけで、問い合わせは 3 秒間隔で 10 件程度(1 分弱)。

```bash
cat > /tmp/kindb_manual/bib.json <<'JSON'
[
  {"title": "HARD THINGS　答えがない難問と困難にきみはどう立ち向かうか", "authors": "ベン・ホロウィッツ", "acquiredTime": 1704067200000, "readStatus": "READ", "asin": "B00W535LOU"},
  {"title": "つげ義春日記 (講談社文芸文庫)", "authors": "つげ義春", "acquiredTime": 1704067200000, "readStatus": "UNKNOWN", "asin": "B088GZFB9Z"},
  {"title": "理想のヒモ生活(25) (角川コミックス・エース)", "authors": "日月 ネコ", "acquiredTime": 1704067200000, "readStatus": "UNKNOWN", "asin": "B0GMYR661F"},
  {"title": "存在しない本のための架空の書名", "authors": "架空の著者", "acquiredTime": 1704067200000, "readStatus": "UNKNOWN", "asin": "B000NOBOOK"}
]
JSON
export BIB_DB=/tmp/kindb_manual/bib.duckdb
kindb import /tmp/kindb_manual/bib.json --db "$BIB_DB"
kindb status --db "$BIB_DB"
kindb enrich --limit 2 --db "$BIB_DB"
kindb enrich --db "$BIB_DB"
kindb status --db "$BIB_DB"
kindb query --table "SELECT asin, bib_match, isbn, paper_issued, publisher, pages, ndc, ndc_label, subjects FROM v_books ORDER BY asin LIMIT 10" --db "$BIB_DB"
kindb search 経営管理 --db "$BIB_DB"
```

期待:
- 取得前の `status` に `Bib not fetched: 4` が出て、`Bib status` の行は出ない。
- 1 回目の `enrich` は `Fetching 2 books` で始まり、`[1/2] B000NOBOOK not_found …` と `[2/2] B00W535LOU found (edition) …` の 2 行を出す(ASIN の順)。
- 2 回目の `enrich` は残りの 2 冊だけを引く(`Fetching 2 books`)。取得済みの本を引き直さない。
- 取得後の `status` に `Bib status: found: 3`、`Bib status: not_found: 1`、`Bib match: edition: 3`、`Last enrich` が出る。
- `v_books` の `B00W535LOU` は `isbn = 9784822250850`、`paper_issued = 2015-04`、`ndc = 336`、`ndc_label = 経営管理`、`subjects = [経営管理]`。`B088GZFB9Z` は講談社文芸文庫の紙版(`isbn = 9784065190678`)に照合され、1983 年の単行本ではない。`B000NOBOOK` の書誌情報の列は NULL か `[]`。
- `search 経営管理` は、書名に「経営管理」を含まない `B00W535LOU` を件名で拾う。表に件名の列は出ない。

中断と再開(Ctrl-C):

```bash
kindb enrich --refresh --db "$BIB_DB"
# [2/4] の行が出たところで Ctrl-C
echo "exit=$?"
kindb enrich --refresh --db "$BIB_DB"
kindb enrich --refresh --db "$BIB_DB"
```

期待:
- 1 回目: `Interrupted. Saved the books fetched so far; rerun to resume.` が stderr に出て、`exit=130`。トレースバックは出ない。
- 1 回目のあと `kindb status --db "$BIB_DB"` に `Bib refetch unfinished` の行が出る。
- 2 回目: `Resuming the refetch started at …; skipping N books already refetched.` の行が出て、1 回目に引き直した本を飛ばし、残りの本だけを引く(`Fetching 2 books` 前後。中断の瞬間によって 2 か 3)。
- 3 回目: 引き直しを終えたので、再開の行は出ず、4 冊すべてを引き直す(`Fetching 4 books`)。

同時実行:

```bash
kindb enrich --refresh --db "$BIB_DB" &
sleep 1
kindb rematch --db "$BIB_DB"; echo "exit=$?"
wait
```

期待: `rematch` は `Error: Another kindb enrich or rematch is running on …` の 1 行を出して `exit=1`。バックグラウンドの `enrich` はそのまま最後まで進む。

手動訂正と再照合:

```bash
printf 'asin,isbn\nB088GZFB9Z,\nB000NOBOOK,978-4-8222-5085-0\n' > /tmp/kindb_manual/overrides.csv
kindb enrich --overrides /tmp/kindb_manual/overrides.csv --db "$BIB_DB"
kindb query --table "SELECT asin, status, source FROM bib_fetches ORDER BY asin LIMIT 10" --db "$BIB_DB"
kindb rematch --db "$BIB_DB"
printf 'asin,isbn\nB000NOBOOK,978-4-8222-5085-1\n' > /tmp/kindb_manual/bad_overrides.csv
kindb enrich --overrides /tmp/kindb_manual/bad_overrides.csv --db "$BIB_DB"; echo "exit=$?"
```

期待:
- `Overrides: 2 rows saved; 1 books reset, 1 books excluded.` が出て、`B000NOBOOK` だけを ISBN で引く(`Fetching 1 books`)。
- `bib_fetches` で `B088GZFB9Z` は `excluded`、`B000NOBOOK` は `found` で `source = isbn`。
- `rematch` は `Rematched 3 books: 0 changed, 0 without a match.` を出し、通信しない。
- 検査数字の誤った ISBN は `Error: line 2: invalid ISBN 978-4-8222-5085-1 for B000NOBOOK` で `exit=1`。保存済みの訂正は変わらない。

`kindb import` が書誌情報に触れないこと:

```bash
kindb import /tmp/kindb_manual/bib.json --db "$BIB_DB"
kindb query "SELECT count(*) AS n FROM bib_matches" --db "$BIB_DB"
```

期待: 取り込み直しても `bib_matches` の件数は変わらない。

詳細の表示とログファイル(`-v`、`--log-file`):

```bash
kindb enrich --refresh --where "asin = 'B00W535LOU'" -v --log-file /tmp/kindb_manual/enrich.log --db "$BIB_DB"
kindb enrich --refresh --where "asin = 'B00W535LOU'" -v --db "$BIB_DB" 2>/dev/null
cat /tmp/kindb_manual/enrich.log
kindb enrich --log-file "" --db "$BIB_DB"; echo "exit=$?"
kindb enrich --log-file /tmp/kindb_manual/missing/enrich.log --db "$BIB_DB"; echo "exit=$?"
```

期待:
- 1 回目: `[1/1] B00W535LOU found (edition) …` の前に、`  NDL Search title="…" creator="…": N hits (0.4s)` のような問い合わせの行が 1 行以上、灰色で出る。最後の `Fetched 1 of 1 books` の前に `  Saved 1 books to the database` が出る。
- 2 回目: 標準エラーを捨てると、問い合わせの行と保存の行は消え、`Fetching …`、`[1/1] …`、`Fetched …` だけが残る(詳細は標準出力に混ざらない)。
- ログファイルの各行は `2026-09-30 12:34:56,789 INFO ` のような日時と重要度で始まり、1 回目の端末の行と詳細の行がすべて入っている。2 回目の行は入らない。
- 空の `--log-file` は `Invalid value for '--log-file': must not be empty …` で `exit=2`。
- ないディレクトリのパスは `Error: Cannot open the log file: …` で `exit=1`。取得は始まらない。
- NDL サーチの 429 で待つときの `Warning: NDL Search returned HTTP 429; retrying in 60s (1/3)` は、手元では起こせないので自動テストで確かめる。

## 8.6 手動訂正の画面(fix)

8.5 の `$BIB_DB` を続けて使う。反映すると NDL サーチに問い合わせる(下の手順で 2〜7 回)。

```bash
kindb fix -v --db "$BIB_DB" --overrides /tmp/kindb_manual/overrides.csv --log-file /tmp/kindb_manual/fix.log
```

期待(起動):
- `Serving the fix page at http://127.0.0.1:<port>/` と `Overrides CSV: /tmp/kindb_manual/overrides.csv` が出て、ブラウザが開く。
- 画面が開くと、端末に `  GET / HTTP/1.1 200`、`  GET /api/state HTTP/1.1 200`、`  GET /api/books?view=review&q=&staged= HTTP/1.1 200` の 3 行が灰色で出る(`-v` の効果)。`/favicon.ico` の 404 の行は出ない。
- 画面の上に DB と CSV のパスが出る。「要確認」の一覧は `0 冊`(8.5 の本は、照合できたか訂正済み)。
- 「訂正済み」を押すと、8.5 で訂正した `B000NOBOOK`(「ISBN で特定」)と `B088GZFB9Z`(「除外」)が出る。

画面での操作:
1. 「すべて」を押し、検索欄に `ヒモ` と入れて `B0GMYR661F` を選ぶ。ラベルは「紙版を特定」で、その下に次にすることの案内は出ない。ASIN の下に Amazon の商品ページへのリンク(`https://www.amazon.co.jp/dp/B0GMYR661F`)が出る。保存済みの候補の表が出て、今の照合の行に「現在の照合」が付く。書名は NDL サーチへのリンク。
2. ISBN 欄に `9784822250851`(検査数字の誤り)を入れると、欄が赤くなり「チェックデジットが一致しません」と出て、「この ISBN を指定」を押せない。`9784822250850` に直すと押せる。押すと、入力欄のすぐ下に「反映待ち: ISBN 9784822250850 を指定」と「キャンセル」が出て、画面の下に「反映待ち 1 冊」と「ISBN の指定: 1 冊(NDL サーチへの問い合わせ 1 回)」が出る。Enter キーで指定しても同じで、取り消されない。検索欄を空にして「要確認」を押すと、`B0GMYR661F` が「紙版を特定」「反映待ち」で出る。
3. 「すべて」を押して `B000NOBOOK` を選び、「訂正を取り消す」を押す。上の枠が「反映待ち: 訂正の取り消し」と「キャンセル」に変わる。反映待ちが 2 冊になり、「訂正の取り消し: 1 冊(書名で再検索…)」が加わる。
4. 「反映する」を押す。問い合わせ中の表示のあと、「反映しました(再取得 2 冊)」と 1 冊ずつの結果が出る。`B0GMYR661F` は「ISBN で特定 9784822250850」、`B000NOBOOK` は「見つからない」。端末には、`  Applying: fetching 2 books from NDL Search` のあと、本ごとに問い合わせの行とその本の結果の行(`  [1/2] B000NOBOOK not_found …`、`  [2/2] B0GMYR661F found (isbn) …`)が出て、`  Saved 2 books to the database`、`  POST /api/apply HTTP/1.1 200` と続く。
5. 「要確認」に `B000NOBOOK`(「見つからない」)だけが出る。選ぶと、ラベルの下に、ISBN の入力を促す案内が色付きの枠で出る。「訂正済み」には `B0GMYR661F`(「ISBN で特定」)と `B088GZFB9Z`(「除外」)が出る。

期待(反映のあと):

```bash
cat /tmp/kindb_manual/overrides.csv
kindb query --table "SELECT asin, status, source FROM bib_fetches ORDER BY asin LIMIT 10" --db "$BIB_DB"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/api/state
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:<port>/
```

- CSV は `asin,isbn`、`B088GZFB9Z,`、`B0GMYR661F,9784822250850` の 3 行(`B000NOBOOK` の行は消えている)。
- `bib_fetches` で `B0GMYR661F` は `found` で `source = isbn`、`B000NOBOOK` は `not_found` で `source = title`、`B088GZFB9Z` は `excluded` のまま。
- トークンのない API の要求と、`Host` の違う要求は、どちらも `403`。端末の要求の行には、`403 (Missing or wrong token. Reload the page.)` と `403 (Unexpected Host header.)` のように理由が付く。
- 端末で Ctrl-C を押すと `Stopped.` が出て終わる。
- `/tmp/kindb_manual/fix.log` に、起動の 3 行、要求の行、反映の行、`Stopped.` が日時付きで入っている。

## 9. v0.2 DB マイグレーション

新テーブル/view が無い DB を作って、読み取り CLI が自動で schema を更新することを確認する。

```bash
uv run python - <<'PY'
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
unset TEST_DB BIB_DB
```

期待:
- `/tmp/kindb_manual` 配下のテスト用 DB / fixture / WAL が全て削除される。
- `$TEST_DB` 環境変数が解除され、以降のうっかり操作で本番 DB に流れない。
- 本番 DB (`~/.kindb/kindle.duckdb`) には一切触れていないこと（シナリオ中は常に `--db "$TEST_DB"` を指定する前提）。
