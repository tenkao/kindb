# kindb 手動テストシナリオ

kindb v0.1 を手で一通り叩いて動作確認するための網羅シナリオ。`pytest` で自動化済みの項目も含め、**実際のターミナルで目視確認したい観点**を網羅している。

全シナリオの実行時間は、実データでのフル import を含めて 30–40 分程度。fixture のみなら 10 分程度。

---

## 0. 準備

### 0.1 環境セットアップ

```bash
cd /Users/tenkao/Documents/Projects/codex/kindb
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

kindb --help      # サブコマンド一覧が表示されること
kindb --version || true   # （--version は未実装なので出ない想定）
```

期待: `import`, `status`, `search`, `query`, `authors`, `genres`, `series`, `recent`, `reading`, `delete` が列挙される。

### 0.2 テスト用 DB パスを用意

本番 `~/.kindb/kindle.duckdb` を汚したくないので、手動テストでは `--db` で別パスを指定する。

```bash
export TEST_DB=/tmp/kindb_manual/test.duckdb
rm -rf /tmp/kindb_manual && mkdir -p /tmp/kindb_manual
```

以降、各コマンドで `--db "$TEST_DB"` を付ける。

### 0.3 fixture zip の生成

テスト用の匿名化された最小 `Kindle.zip` を生成する:

```bash
python -m tests.create_fixture
ls tests/fixtures/Kindle.zip
```

期待: `tests/fixtures/Kindle.zip` が作られる。中身は 3 冊の有効本 + フィルタ対象外行 + 読書セッション + 個人文書。

### 0.4 ruff + pytest の前段確認

手動テストに入る前に自動テストが緑であることを確認:

```bash
ruff check . && pytest -q
```

期待: ruff no issues / pytest 全件 pass。

---

## 1. `kindb import`

### 1.1 fixture zip の取り込み（初回）

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
```

期待:
- `Import complete: 3 books, 3 reading sessions` が出る。
- `Database: /tmp/kindb_manual/test.duckdb` が表示される。
- `$TEST_DB` ファイルが生成されている (`ls -l "$TEST_DB"`)。
- 一時ディレクトリ残骸が無い: `ls /tmp/kindb_manual/` で `test.duckdb` 以外の `tmp*` ディレクトリが無いこと。

### 1.2 再 import（置換）

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
```

期待: 再び `3 books, 3 reading sessions`。エラーなく成功。DB ファイルが上書きされる。

### 1.3 不正な zip（テキストファイル）

```bash
echo "not a zip" > /tmp/kindb_manual/bad.zip
kindb import /tmp/kindb_manual/bad.zip --db "$TEST_DB"
echo "exit=$?"
```

期待:
- 赤字で `Error: Not a valid zip file: ...` が出る。
- 終了コードが 1。
- 既存の `$TEST_DB` は無傷 (`kindb status --db "$TEST_DB"` が以前と同じ内容)。

### 1.4 存在しない zip

```bash
kindb import /tmp/does_not_exist.zip --db "$TEST_DB"
echo "exit=$?"
```

期待: `Error: Zip file not found: ...`、終了コード 1、既存 DB は無傷。

### 1.5 必須列が欠落した books CSV

```bash
python - <<'PY'
import csv, io, zipfile
with zipfile.ZipFile("/tmp/kindb_manual/missing_cols.zip", "w") as zf:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["ASIN", "Product Name"])  # 必須列欠落
    w.writeheader()
    w.writerow({"ASIN": "B000X", "Product Name": "X"})
    zf.writestr(
        "Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv",
        buf.getvalue(),
    )
PY
kindb import /tmp/kindb_manual/missing_cols.zip --db "$TEST_DB"
echo "exit=$?"
ls /tmp/kindb_manual/   # tmp ディレクトリが残っていないこと
```

期待:
- `Error: ... is missing required columns: ['Deleted By Customer', 'Ownership Type', 'Relationship Creation Date', 'Resource Type']` が出る。
- 終了コード 1。
- `$TEST_DB` は無傷。
- `/tmp/kindb_manual/` 配下に `tmp*` 残骸が無い。

### 1.6 蔵書本体 CSV が zip に無い

```bash
python - <<'PY'
import zipfile
with zipfile.ZipFile("/tmp/kindb_manual/no_books.zip", "w") as zf:
    zf.writestr("dummy.txt", "hi")
PY
kindb import /tmp/kindb_manual/no_books.zip --db "$TEST_DB"
echo "exit=$?"
```

期待: `Error: CustomerRelationshipIndex_FE.csv not found in zip`。終了コード 1。

### 1.7 stale WAL の除去

```bash
# 一旦削除して、WAL だけ残っている状態を再現
rm -f "$TEST_DB" "$TEST_DB.wal"
echo "fake-wal" > "$TEST_DB.wal"
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
ls -la "$TEST_DB"*
```

期待: `$TEST_DB` は作られ、`$TEST_DB.wal` が消えている。

### 1.8 実データ（任意）

本物の `Kindle.zip` を持っていれば、別 DB に取り込む:

```bash
export REAL_DB=/tmp/kindb_manual/real.duckdb
kindb import ~/Downloads/Kindle.zip --db "$REAL_DB"
```

期待:
- エラーなく完了する。
- books_count と reading_sessions_count が妥当（蔵書数と近いオーダー）。
- Claude Desktop / `kindb status` の表示と整合している。

---

## 2. `kindb status`

### 2.1 DB 不存在

```bash
kindb status --db /tmp/does_not_exist.duckdb
echo "exit=$?"
```

期待: `No database found. Run 'kindb import' first.`、終了コード 1。

### 2.2 取り込み済み DB

```bash
kindb status --db "$TEST_DB"
```

期待: テーブル表示で以下の行が出る。
- Last import: 直近の import 日時（UTC）
- Source: `tests/fixtures/Kindle.zip` の絶対パス
- Books: 3
- Authors: 3（山田太郎 / 佐藤花子 / John Smith）
- Genres: 3（文学・評論 / コミック / Science Fiction）
- Reading sessions: 3
- Personal documents: 1
- Database: `/tmp/kindb_manual/test.duckdb`

---

## 3. `kindb search`

fixture 前提で以下を実行。

### 3.1 書名ヒット（日本語）

```bash
kindb search テスト --db "$TEST_DB"
```

期待: `B000TEST01 テストの本` と `B000TEST03 シリーズ第2巻` が両方ヒット（シリーズタイトルが「テストシリーズ」のため）。

### 3.2 著者ヒット

```bash
kindb search 山田 --db "$TEST_DB"
```

期待: `B000TEST01` と `B000TEST03` がヒット。`B000TEST02`（John Smith）は出ない。

### 3.3 ジャンルヒット（英語）

```bash
kindb search "Science Fiction" --db "$TEST_DB"
```

期待: `B000TEST02 Another Book` がヒット。

### 3.4 シリーズヒット

```bash
kindb search テストシリーズ --db "$TEST_DB"
```

期待: `B000TEST01`, `B000TEST03` がヒット。

### 3.5 ASIN ヒット

```bash
kindb search B000TEST02 --db "$TEST_DB"
```

期待: `Another Book` の 1 行。

### 3.6 ヒットなし

```bash
kindb search ZZZZZ --db "$TEST_DB"
```

期待: `No results found.`（終了コード 0）。

### 3.7 ワイルドカード文字のエスケープ（`%`）

ユーザー入力中の `%` が ILIKE のワイルドカード扱いされず、リテラル `%` として検索されることを確認する。fixture には `%` を含む本は無いので、臨時 zip を作って試す:

```bash
python - <<'PY'
import csv, io, zipfile
with zipfile.ZipFile("/tmp/kindb_manual/percent.zip", "w") as zf:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[
        "ASIN","Product Name","Sortable Title","Sortable Author Name",
        "Series Title","Series Author","Position In Collection","Marketplace",
        "Relationship Creation Date","Resource Type","Ownership Type","Deleted By Customer",
    ])
    w.writeheader()
    for asin, name in [("B000PERC01","50% OFF Sale Book"), ("B000PERC02","Regular Book")]:
        w.writerow({"ASIN":asin,"Product Name":name,"Sortable Title":name,
                    "Sortable Author Name":"A","Series Title":"","Series Author":"",
                    "Position In Collection":"","Marketplace":"JP",
                    "Relationship Creation Date":"2024-01-01T00:00:00Z",
                    "Resource Type":"ITEM","Ownership Type":"Item Owner",
                    "Deleted By Customer":""})
    zf.writestr("Kindle.UnifiedLibraryIndex/CustomerRelationshipIndex_FE.csv", buf.getvalue())
PY
kindb import /tmp/kindb_manual/percent.zip --db /tmp/kindb_manual/percent.duckdb
kindb search "50%" --db /tmp/kindb_manual/percent.duckdb
```

期待: `B000PERC01` のみヒット、`B000PERC02` はヒットしない。

### 3.8 ワイルドカード文字のエスケープ（`_`）

```bash
kindb search "50_" --db /tmp/kindb_manual/percent.duckdb
```

期待: ヒット 0 件（`_` がリテラル扱い）。もしエスケープ漏れがあると `50%` のタイトル（"50% OFF..."）が誤ヒットする。

### 3.9 大小文字無視（ILIKE）

```bash
kindb search another --db "$TEST_DB"
```

期待: `Another Book` がヒット（ILIKE なので case-insensitive）。

---

## 4. `kindb query`

### 4.1 SELECT（JSON 出力、デフォルト）

```bash
kindb query "SELECT count(*) AS n FROM books" --db "$TEST_DB"
```

期待: `[{"n": 3}]` が JSON で出る。

### 4.2 SELECT（テーブル出力）

```bash
kindb query --table "SELECT asin, product_name FROM books ORDER BY asin" --db "$TEST_DB"
```

期待: rich Table で 3 行。

### 4.3 WITH 句（CTE）

```bash
kindb query "WITH c AS (SELECT count(*) AS n FROM books) SELECT * FROM c" --db "$TEST_DB"
```

期待: `[{"n": 3}]`。

### 4.4 DESCRIBE / SHOW / PRAGMA / EXPLAIN

```bash
kindb query "DESCRIBE books" --db "$TEST_DB"
kindb query "SHOW TABLES" --db "$TEST_DB"
kindb query "PRAGMA table_info('books')" --db "$TEST_DB"
kindb query "EXPLAIN SELECT * FROM books" --db "$TEST_DB"
```

期待: いずれも成功。

### 4.5 書き込み系 SQL の拒否（regex 層）

```bash
kindb query "DELETE FROM books" --db "$TEST_DB"; echo "exit=$?"
kindb query "INSERT INTO books (asin) VALUES ('X')" --db "$TEST_DB"; echo "exit=$?"
kindb query "UPDATE books SET product_name='x'" --db "$TEST_DB"; echo "exit=$?"
kindb query "DROP TABLE books" --db "$TEST_DB"; echo "exit=$?"
kindb query "CREATE TABLE t (a INT)" --db "$TEST_DB"; echo "exit=$?"
kindb query "ATTACH ':memory:' AS m" --db "$TEST_DB"; echo "exit=$?"
```

期待: 全て `Error: Only SELECT, ... allowed.` / 終了コード 1。

### 4.6 複文の拒否（regex を通過するパターン）

```bash
# 先頭 SELECT なので regex は通る。read-only 接続層で拒否される。
kindb query "SELECT 1; UPDATE books SET product_name='hacked' WHERE asin='B000TEST01'" --db "$TEST_DB"
echo "exit=$?"
kindb query "SELECT 1; DROP TABLE books" --db "$TEST_DB"
echo "exit=$?"

# 書き換わっていないことの確認
kindb query "SELECT product_name FROM books WHERE asin='B000TEST01'" --db "$TEST_DB"
kindb query "SELECT count(*) FROM books" --db "$TEST_DB"
```

期待:
- どちらも終了コード非 0（DuckDB の read-only 接続が書き込みを拒否）。
- product_name は `テストの本` のまま。
- `books` 件数は 3 のまま。

### 4.7 DB 不存在時

```bash
kindb query "SELECT 1" --db /tmp/does_not_exist.duckdb; echo "exit=$?"
```

期待: `No database found.` / 終了コード 1。

---

## 5. `kindb authors` / `genres` / `series`

### 5.1 authors

```bash
kindb authors --db "$TEST_DB"
```

期待: 3 行。`山田太郎` が Books 2（TEST01, TEST03）、`佐藤花子` と `John Smith` が Books 1。降順 + 著者名昇順。

### 5.2 genres

```bash
kindb genres --db "$TEST_DB"
```

期待: 3 行。`文学・評論` が 2 冊、`コミック` と `Science Fiction` が 1 冊ずつ。

### 5.3 series

```bash
kindb series --db "$TEST_DB"
```

期待: `テストシリーズ` が 1 行だけ出る（Books 2, Positions "1, 2", Author "山田太郎"）。空シリーズは除外されている。

---

## 6. `kindb recent`

### 6.1 デフォルト

```bash
kindb recent --db "$TEST_DB"
```

期待: 3 冊（fixture は 3 冊しかないため）。順序は `relationship_creation_date` DESC:
1. B000TEST02 (2024-03-20)
2. B000TEST03 (2024-02-10)
3. B000TEST01 (2024-01-15)

### 6.2 --limit

```bash
kindb recent --limit 1 --db "$TEST_DB"
kindb recent -n 1 --db "$TEST_DB"
```

期待: どちらも `B000TEST02` のみの 1 行。`B000TEST01` / `B000TEST03` は出ない。

### 6.3 --limit 0

```bash
kindb recent --limit 0 --db "$TEST_DB"
```

期待: `No data.`（0 行なので agg query の分岐に入る）。

---

## 7. `kindb reading`

### 7.1 基本

```bash
kindb reading --db "$TEST_DB"
```

期待: 2 行（TEST01, TEST02）。
- `B000TEST01`: Sessions 2, Total ms 4500000, Page Flips 105
- `B000TEST02`: Sessions 1, Total ms 900000, Page Flips 20
- `B000TEST03` は読書セッションが無いので出ない。

### 7.2 reading_insight_sessions の二重計上ガード

reading_sessions には TEST01 が 2 件、reading_insight_sessions にも同 ASIN が 1 件あるが、sessions 列は **2**（= 3 にならない）。

```bash
kindb query "SELECT reading_session_count FROM v_reading_summary WHERE asin = 'B000TEST01'" --db "$TEST_DB"
kindb query "SELECT count(*) FROM reading_insight_sessions WHERE asin = 'B000TEST01'" --db "$TEST_DB"
```

期待: 前者 = 2、後者 = 1。両者が別個に保持されていることと、集計ビューは reading_sessions のみ参照していることを確認。

---

## 8. `kindb delete`

### 8.1 確認プロンプト（キャンセル）

```bash
kindb delete --db "$TEST_DB"
# プロンプトで n
```

期待: `Cancelled.` と表示、DB は残る。

### 8.2 確認プロンプト（承諾）

```bash
kindb delete --db "$TEST_DB"
# プロンプトで y
```

期待: `Deleted: /tmp/kindb_manual/test.duckdb`。DB が消える。

### 8.3 --yes でスキップ

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
kindb delete --db "$TEST_DB" --yes
ls "$TEST_DB" 2>&1 || echo "gone"
```

期待: プロンプトなしで削除、`gone`。

### 8.4 DB 不存在時

```bash
kindb delete --db /tmp/does_not_exist.duckdb
```

期待: `No database to delete.`（終了コード 0）。

### 8.5 WAL も一緒に消える（拡張子 .duckdb）

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
echo "fake-wal" > "$TEST_DB.wal"
kindb delete --db "$TEST_DB" --yes
ls "$TEST_DB"* 2>&1 || echo "both gone"
```

期待: `$TEST_DB` と `$TEST_DB.wal` の両方が消える。

### 8.6 WAL も一緒に消える（拡張子 .db）

```bash
DB2=/tmp/kindb_manual/store.db
kindb import tests/fixtures/Kindle.zip --db "$DB2"
echo "fake-wal" > "$DB2.wal"
kindb delete --db "$DB2" --yes
ls "$DB2"* 2>&1 || echo "both gone"
```

期待: `$DB2` と `$DB2.wal` の両方が消える（旧実装だと `.wal` のサイドカー判定を誤るリグレッションを踏んだので回帰確認）。

---

## 9. DB パス解決

### 9.1 `--db` オプション（絶対パス）

既に各節で検証済み。

### 9.2 `KINDB_DB_PATH` 環境変数

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
KINDB_DB_PATH="$TEST_DB" kindb status
KINDB_DB_PATH="$TEST_DB" kindb query "SELECT count(*) FROM books"
```

期待: `--db` を付けなくても `$KINDB_DB_PATH` 経由で解決される。

### 9.3 デフォルトパス（`~/.kindb/kindle.duckdb`）

（※本番 DB を汚す可能性があるので、空のホームディレクトリ扱いしたい場合のみ）

```bash
# 事前に ~/.kindb/kindle.duckdb のバックアップを取ってから
HOME=/tmp/kindb_fake_home kindb status
```

期待: `No database found.`。

---

## 10. ビュー層

### 10.1 v_books 形状

```bash
kindb import tests/fixtures/Kindle.zip --db "$TEST_DB"
kindb query --table "DESCRIBE v_books" --db "$TEST_DB"
kindb query --table "SELECT asin, authors, genres, image_url FROM v_books ORDER BY asin" --db "$TEST_DB"
```

期待:
- カラム: `asin, product_name, sortable_title, sortable_author_name, authors, genres, image_url, series_title, series_author, position_in_collection, marketplace, relationship_creation_date`。
- `authors` は `VARCHAR[]`、ソート済み（TEST01 は `[佐藤花子, 山田太郎]`）。
- `genres` も同様にソート済み。
- `image_url` は ASIN あたり決定的に 1 件（TEST01 は `..._a.jpg` 側。`min()` で `_a.jpg < _b.jpg`）。
- TEST03 は `image_url` が NULL（CSV に空文字のみだったため）。

### 10.2 v_reading_summary

```bash
kindb query --table "SELECT * FROM v_reading_summary ORDER BY asin" --db "$TEST_DB"
```

期待: 2 行。TEST01 と TEST02 のみ。reading_insight_sessions の行は集計に入らない。

### 10.3 v_books_with_reading の LEFT JOIN

```bash
kindb query --table "SELECT asin, reading_session_count FROM v_books_with_reading ORDER BY asin" --db "$TEST_DB"
```

期待: 3 行。TEST03 の `reading_session_count` が NULL（= 未読とは限らない、という SKILL.md の注意点を目視確認）。

---

## 11. MCP 連携（任意）

Claude Desktop / Claude Code から `mcp-server-motherduck` 経由で読み取れることを確認する。

### 11.1 Claude Desktop 設定

`~/Library/Application Support/Claude/claude_desktop_config.json` に README の通り追記。`<HOME>` を実ホームに置換し、`--db-path` を手動テスト用の `$TEST_DB` か本番 `~/.kindb/kindle.duckdb` に合わせる。

### 11.2 動作確認クエリ（Claude Desktop で実行）

- 「蔵書は何冊？」
- 「最近読んだ本 Top 5 を教えて」
- 「著者別の冊数 Top 10」

期待:
- MCP 経由で `v_books` / `v_books_with_reading` が参照され、回答が返る。
- 「読了本は？」のような公式データにない問いには、SKILL.md の指針に従って「判定できない」と返る。

---

## 12. クリーンアップ

```bash
rm -rf /tmp/kindb_manual
```

---

## チェックリスト（実行用）

- [ ] 0. 準備（環境、fixture、ruff/pytest）
- [ ] 1.1 fixture import 成功
- [ ] 1.2 再 import で置換
- [ ] 1.3 不正 zip 拒否 + 既存 DB 無傷
- [ ] 1.4 存在しない zip 拒否 + 既存 DB 無傷
- [ ] 1.5 必須列欠落拒否 + tmp 残骸なし
- [ ] 1.6 books CSV 欠落拒否
- [ ] 1.7 stale WAL が import 後に消える
- [ ] 1.8 （任意）実データ import
- [ ] 2.1 status: DB 無し
- [ ] 2.2 status: DB あり
- [ ] 3.1 search: 書名
- [ ] 3.2 search: 著者
- [ ] 3.3 search: ジャンル
- [ ] 3.4 search: シリーズ
- [ ] 3.5 search: ASIN
- [ ] 3.6 search: ヒットなし
- [ ] 3.7 search: `%` エスケープ
- [ ] 3.8 search: `_` エスケープ
- [ ] 3.9 search: case-insensitive
- [ ] 4.1 query: JSON
- [ ] 4.2 query: --table
- [ ] 4.3 query: WITH
- [ ] 4.4 query: DESCRIBE/SHOW/PRAGMA/EXPLAIN
- [ ] 4.5 query: 書き込み系拒否
- [ ] 4.6 query: 複文を read-only 層が拒否
- [ ] 4.7 query: DB 無し
- [ ] 5.1–5.3 authors / genres / series
- [ ] 6.1–6.3 recent + --limit
- [ ] 7.1–7.2 reading + 二重計上ガード
- [ ] 8.1–8.6 delete 全分岐 + WAL 削除
- [ ] 9.1–9.3 DB パス解決
- [ ] 10.1–10.3 ビュー層
- [ ] 11 （任意）MCP 連携
- [ ] 12 クリーンアップ
