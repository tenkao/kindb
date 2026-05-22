---
name: kindb
description: Kindle 蔵書の検索・集計。「Kindle の本」「蔵書」「読了マーク」「著者別冊数」「最近取得した本」に言及された場合や、Kindle ライブラリのキーワード検索を行う場合にこの Skill を使う。
---

# kindb SKILL

kindb は `kindle.json` を DuckDB に取り込み、Kindle 蔵書をローカルで検索・集計するツール。Claude Desktop(MCP 経由) や Claude Code / CLI からこの DB を参照するときの、生成 AI 向け指針をまとめる。

## 前提条件

まず DB の状態を確認する:

```bash
kindb status
```

DB が存在しない場合は `kindb import <kindle.json>` で取り込みが必要。

公式 `Kindle.zip` を取り込み済みかどうかは `kindb status` の `Official import` 行で確認する。未取り込みでも通常の `v_books` / `v_author_counts` は使える。

## 使い方

```bash
kindb query "<SQL>"          # JSON 出力(デフォルト)
kindb query --table "<SQL>"  # テーブル形式出力
```

許可されるのは `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ。書き込み系 SQL は拒否される。

DB ファイル: `~/.kindb/kindle.duckdb`

## 基本ルール

- **通常は `v_books` を使う**。正規化テーブル(`books`, `book_authors`)を直接参照するのは集計やデバッグ用途に限る。
- **`ORDER BY` には一意キーを含めて決定的順序にする**。`v_books` は `asin`、`v_author_counts` は `author_name`、`v_author_id_counts` は `author_id` を標準のタイブレーカーにする。主ソートが `DESC` ならタイブレーカーも `DESC`、`ASC` なら `ASC` と方向を揃える。タイブレーカーがないと `OFFSET` ページング中に同値行がページ境界をまたいで取りこぼし/重複の原因になる。
- 一覧取得は必ず `LIMIT/OFFSET` を付ける。全件が必要な場合も一度に取得しない。
- 全件回答が必要な場合は、まず `SELECT count(*)` で総数を確認し、必要な範囲を `LIMIT N OFFSET M` で反復取得する。取得件数が総数と一致してから回答する。
- `product_image_url` は出力サイズが大きくなりやすいため、表紙画像が必要なときだけ選択する。
- `genres` / `author_ids` / `author_names_official` も LIST 列で出力サイズが増えるため、軽い一覧では選択しない。
- `READ` はユーザーが Kindle 上で付けた読了マークの自己申告フラグ。集計に使ってよい。
- `UNKNOWN` は「読了マークなし」。**未読とは断定しない**。読み始めていても READ マークを付けていない場合は `UNKNOWN` のまま。
- 「未読書籍」を聞かれた場合は `WHERE read_status = 'UNKNOWN'` を使ってよいが、回答文では「読了マークが付いていない本」と言い換える。
- 断定してはいけない項目:
  - 発売日 / 出版社 / 購入価格
  - Kindle Unlimited 判定 / 購入経路
  - マンガ / 固定レイアウト判定

## ページング標準フロー

```sql
SELECT count(*) AS n
FROM v_books;

SELECT asin, title, authors_text, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC, asin DESC
LIMIT 50 OFFSET 0;
```

必要な場合だけ `OFFSET` を `50`, `100`, ... と進めて取得する。`mcp-server-motherduck` の `--max-rows` / `--max-chars` は返却時の打ち切り設定であり、ページングの代替ではない。

## 主要ビュー

### `v_books`(1 冊 1 行の主ビュー)

| 列 | 説明 |
|---|---|
| `asin` | Amazon ASIN |
| `title` | 書名 |
| `authors` | 著者名の配列(`LIST<VARCHAR>`)。`author_order` 昇順 |
| `authors_text` | `kindle.json.authors` の元文字列 |
| `read_status` | JSON 原値 |
| `product_image_url` | 表紙画像 URL。欠落時は `NULL` |
| `acquired_at` | ライブラリ取得日時。購入日とは限らない |
| `genres` | 公式 zip 由来のジャンル配列。未取り込み/該当なしは空配列 `[]` |
| `series_title` | 公式 zip 由来のシリーズ名。該当なしは `NULL` |
| `series_asin` | 公式 zip 由来のシリーズ ASIN。該当なしは `NULL` |
| `series_position` | 公式 zip 由来の巻番号。該当なしは `NULL` |
| `author_ids` | 公式 zip 由来の Amazon 著者 ID 配列。未取り込み/該当なしは空配列 `[]` |
| `author_names_official` | 公式 zip 由来の著者名配列。翻訳者等を含む。未取り込み/該当なしは空配列 `[]` |

zip 未取り込み時、LIST 列(`genres` / `author_ids` / `author_names_official`)は `NULL` ではなく空配列 `[]`。scalar 列(`series_title` / `series_asin` / `series_position`)は `NULL`。

### `v_author_counts`

| 列 | 説明 |
|---|---|
| `author_name` | 著者名 |
| `book_count` | その著者の冊数 |

`ORDER BY book_count DESC, author_name ASC` で決定的に並ぶ。

### 公式 zip 由来のビュー

| ビュー | 用途 |
|---|---|
| `v_book_genres` | 本とジャンルの 1:N 展開 |
| `v_book_series` | シリーズ内の蔵書を巻順で見る |
| `v_series_counts` | シリーズ別の所有冊数 |
| `v_genre_counts` | ジャンル別の所有冊数 |
| `v_author_id_counts` | Amazon 著者 ID ベースの著者別冊数 |
| `v_book_authors_official` | ASIN ごとの公式著者 ID と公式著者名 |

`v_author_counts` は `kindle.json` ベースの著者名集計で、同名・別 ID の区別が不要な通常用途向け。`v_author_id_counts` / `v_book_authors_official` は公式 zip 取り込みが前提で、同名・別 ID を区別したい場合や特定 `author_id` から本を引く場合に使う。

## 代表クエリ

### 著者別の冊数 Top 10

```sql
SELECT author_name, book_count
FROM v_author_counts
ORDER BY book_count DESC, author_name ASC
LIMIT 10;
```

### Amazon 著者 ID ベースの冊数 Top 10

```sql
SELECT author_id, author_name, book_count
FROM v_author_id_counts
ORDER BY book_count DESC, author_name ASC, author_id ASC
LIMIT 10;
```

### 特定 Amazon 著者 ID の本

```sql
SELECT b.asin, b.title, b.authors_text, b.acquired_at
FROM v_books b
INNER JOIN v_book_authors_official a ON a.asin = b.asin
WHERE a.author_id = 'B000000000'
ORDER BY b.acquired_at DESC, b.asin DESC
LIMIT 50 OFFSET 0;
```

### ジャンル別冊数 Top 10

```sql
SELECT genre, book_count
FROM v_genre_counts
ORDER BY book_count DESC, genre ASC
LIMIT 10;
```

### シリーズ別冊数 Top 10

```sql
SELECT series_asin, series_title, book_count
FROM v_series_counts
ORDER BY book_count DESC, series_title ASC
LIMIT 10;
```

### 特定シリーズの巻順一覧

```sql
SELECT series_position, asin, title, relation_type
FROM v_book_series
WHERE series_title = 'シリーズ名'
ORDER BY series_title ASC, series_position ASC NULLS LAST, asin ASC
LIMIT 100;
```

### ジャンルと読了マークのクロス集計

```sql
SELECT g.genre, b.read_status, count(*) AS books
FROM v_book_genres g
INNER JOIN v_books b ON b.asin = g.asin
GROUP BY g.genre, b.read_status
ORDER BY g.genre ASC, b.read_status ASC
LIMIT 100;
```

### 最近取得した本

```sql
SELECT asin, title, authors, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC, asin DESC
LIMIT 20 OFFSET 0;
```

### 読了マーク済みの本

```sql
SELECT asin, title, authors, acquired_at
FROM v_books
WHERE read_status = 'READ'
ORDER BY acquired_at DESC, asin DESC
LIMIT 50 OFFSET 0;
```

### 読了マークが付いていない本

```sql
SELECT asin, title, authors, acquired_at
FROM v_books
WHERE read_status = 'UNKNOWN'
ORDER BY acquired_at DESC, asin DESC
LIMIT 50 OFFSET 0;
```

回答では「未読」ではなく「読了マークが付いていない本」と表現する。

### 年別取得冊数

```sql
SELECT EXTRACT(YEAR FROM acquired_at) AS year, count(*) AS books
FROM v_books
GROUP BY year
ORDER BY year
LIMIT 100;
```

`acquired_at` はライブラリ取得日時であり、購入日とは限らない点に注意する。

### 表紙付き蔵書リスト

```sql
SELECT asin, title, authors, read_status, product_image_url
FROM v_books
ORDER BY title ASC, asin ASC
LIMIT 20 OFFSET 0;
```

## 注意

- `acquired_at` は Kindle ライブラリへの取得日時。購入日の代理指標として弱く扱うのは可だが、断定はしない。
- `product_image_url` が `NULL` の本は、表紙画像が JSON に含まれていなかった本。
- v0.3 では公式 zip からジャンル、シリーズ、Amazon 著者 ID を任意で扱う。価格、marketplace、ownership_type、注文情報、読書セッション、個人文書は扱わない。
