---
name: kindb
description: Kindle 蔵書 DB(kindb)の検索と集計。Kindle の本や蔵書について、書名や著者での検索、著者別・ジャンル別・シリーズ別の冊数、読了マーク、最近取得した本を聞かれたときに使う。
---

# kindb

kindb は、Kindle 蔵書を DuckDB に取り込んだローカルの蔵書 DB。主データは `kindle.json`(書名、著者、取得日時、読了マーク、表紙 URL)で、公式 `Kindle.zip` を取り込んでいればジャンル、シリーズ、Amazon 著者 ID も使える。問い合わせは読み取りだけを行う。

## 問い合わせの経路

| 環境 | 実行方法 |
|---|---|
| CLI(`kindb` コマンドがある。Claude Code など) | `kindb query "<SQL>"` で実行し、JSON で受け取る |
| MCP(`execute_query` ツールがある。Claude Desktop など) | `execute_query` に SQL を渡す。列名の確認は `list_columns` |

- CLI の `kindb query` は、`SELECT` / `WITH` の末尾に `LIMIT` がないと拒否する(集計関数だけの SELECT は除く)。拒否されたら `LIMIT` を足して再実行する。
- CLI の DB は既定で `~/.kindb/kindle.duckdb`。`--db <path>` か環境変数 `KINDB_DB_PATH` で変えられる。
- `kindb search <語>` は書名、著者、ASIN の部分一致を表で返す。件数の上限がないため、ヒットが多そうな語では下の検索クエリを使う。

## 手順

1. **状態を確認する。** CLI では `kindb status` を実行する。DB がなければ `kindb import <kindle.json>` が必要だと伝えて終える。MCP では次のクエリを使う。冊数と、公式データ(`has_official`)の有無が分かれば完了。

   ```sql
   SELECT
     (SELECT count(*) FROM books) AS books,
     (SELECT max(imported_at) FROM import_metadata) AS imported_at,
     (SELECT count(*) FROM import_metadata_official) > 0 AS has_official
   LIMIT 1;
   ```

2. **ビューを選ぶ。** 下の「ビュー」から選ぶ。ジャンル、シリーズ、著者 ID が必要なのに公式データがない場合は、`kindb import-official <Kindle.zip>` での取り込みが必要だと伝える。
3. **件数を数えてから取得する。** 一覧は `count(*)` で総数を確かめ、`LIMIT n OFFSET m` でページごとに取る。`ORDER BY` の最後には一意になる列(`v_books` なら `asin`)を置く。すべてを答えるときは、取得した行数が総数に一致するまで `OFFSET` を進める。
4. **回答する。** 下の「回答の表現」に従う。

手順 3 が必要な理由: MCP サーバ(既定で 1,024 行 / 50,000 文字)も Claude Code の Bash ツール(既定 30,000 文字)も、長い結果を切り詰める。また、並び順が一意でないと、同じ値の行がページの境界で重複したり抜けたりする。`product_image_url` と LIST 列(`genres` / `author_ids` / `author_names_official`)は 1 行が長くなるので、必要なときだけ選ぶ。

## 回答の表現

- `read_status = 'READ'` は、利用者が Kindle で付けた読了マーク。`'UNKNOWN'` は「読了マークが付いていない本」と表現する。読みかけでもマークを付けなければ `UNKNOWN` のままなので、未読とは限らない。「未読の本」を聞かれたら `UNKNOWN` で引き、この表現で答える。
- `acquired_at` は「ライブラリに入った日時」と表現する。再ダウンロードなどで更新されるため、購入日を聞かれたら目安として示す。値は UTC なので、日本時間の日付や年で集計するときは `acquired_at + INTERVAL 9 HOUR` を使う。
- 発売日、出版社、価格、Kindle Unlimited かどうか、購入経路、マンガや固定レイアウトかどうかを聞かれたら、kindb のデータには含まれないと答える。
- ジャンルとシリーズは Amazon 公式データ由来と添える。

## ビュー

### `v_books`(1 冊 1 行。通常はこれを使う)

| 列 | 内容 |
|---|---|
| `asin` | Amazon ASIN(一意) |
| `title` | 書名 |
| `authors` | 著者名の配列(`VARCHAR[]`)。特定の著者は `list_contains(authors, '名前')` で引く |
| `authors_text` | 著者の元の文字列(`", "` 区切り)。部分一致は `ILIKE` で引く |
| `read_status` | `READ` / `UNKNOWN` |
| `product_image_url` | 表紙画像 URL。ない本は NULL |
| `acquired_at` | ライブラリに入った日時(UTC) |
| `genres` | ジャンルの配列。公式データがない本は `[]` |
| `series_title` / `series_asin` / `series_position` | シリーズ名、シリーズ ASIN、巻番号。ない本は NULL |
| `author_ids` | Amazon 著者 ID の配列。ない本は `[]` |
| `author_names_official` | 公式の著者名の配列(翻訳者などを含む)。ない本は `[]` |

### その他のビュー

`ORDER BY` 列は、ページングで結果が一意に並ぶ並び順。

| ビュー | 列 | `ORDER BY` | 用途 |
|---|---|---|---|
| `v_author_counts` | `author_name`, `book_count` | `book_count DESC, author_name` | 著者別の冊数(`kindle.json` 由来。公式データ不要) |
| `v_genre_counts` | `genre`, `book_count` | `book_count DESC, genre` | ジャンル別の冊数 |
| `v_series_counts` | `series_asin`, `series_title`, `book_count` | `book_count DESC, series_title, series_asin` | シリーズ別の冊数 |
| `v_book_genres` | `asin`, `title`, `genre` | `genre, asin` | 本とジャンルの 1:N 展開 |
| `v_book_series` | `series_asin`, `series_title`, `series_position`, `asin`, `title`, `relation_type` | `series_title, series_position NULLS LAST, asin, relation_type` | シリーズ内の本を巻順に見る |
| `v_author_id_counts` | `author_id`, `author_name`, `book_count` | `book_count DESC, author_name, author_id` | 同名で別人の著者を区別した冊数 |
| `v_book_authors_official` | `asin`, `author_order`, `author_id`, `author_name` | `asin, author_order` | 本ごとの公式著者 ID と著者名 |

`v_author_counts` 以外の上表のビューは公式データ由来で、未取り込みなら 0 行になる。

## 代表クエリ

書名や著者の部分一致:

```sql
SELECT asin, title, authors_text, read_status, acquired_at
FROM v_books
WHERE title ILIKE '%検索語%' OR authors_text ILIKE '%検索語%'
ORDER BY title, asin
LIMIT 50 OFFSET 0;
```

特定の著者の本:

```sql
SELECT asin, title, read_status, acquired_at
FROM v_books
WHERE list_contains(authors, '著者名')
ORDER BY acquired_at DESC, asin
LIMIT 50 OFFSET 0;
```

最近ライブラリに入った本(`WHERE read_status = 'READ'` などを足せば読了マークで絞れる):

```sql
SELECT asin, title, authors_text, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC, asin
LIMIT 20 OFFSET 0;
```

著者別の冊数:

```sql
SELECT author_name, book_count
FROM v_author_counts
ORDER BY book_count DESC, author_name
LIMIT 10;
```

年ごとの取得冊数(日本時間):

```sql
SELECT year(acquired_at + INTERVAL 9 HOUR) AS year, count(*) AS books
FROM v_books
GROUP BY year
ORDER BY year
LIMIT 100;
```

ジャンル別とシリーズ別の冊数:

```sql
SELECT genre, book_count
FROM v_genre_counts
ORDER BY book_count DESC, genre
LIMIT 10;

SELECT series_title, book_count
FROM v_series_counts
ORDER BY book_count DESC, series_title, series_asin
LIMIT 10;
```

シリーズの巻順:

```sql
SELECT series_position, asin, title, relation_type
FROM v_book_series
WHERE series_title ILIKE '%シリーズ名%'
ORDER BY series_title, series_position NULLS LAST, asin, relation_type
LIMIT 100;
```

ジャンルと読了マークのクロス集計:

```sql
SELECT g.genre, b.read_status, count(*) AS books
FROM v_book_genres g
JOIN v_books b ON b.asin = g.asin
GROUP BY g.genre, b.read_status
ORDER BY g.genre, b.read_status
LIMIT 100;
```

同名で別人の著者を区別した冊数と、特定の著者 ID の本:

```sql
SELECT author_id, author_name, book_count
FROM v_author_id_counts
ORDER BY book_count DESC, author_name, author_id
LIMIT 10;

SELECT b.asin, b.title, b.acquired_at
FROM v_books b
JOIN v_book_authors_official a ON a.asin = b.asin
WHERE a.author_id = 'B000000000'
ORDER BY b.acquired_at DESC, b.asin
LIMIT 50 OFFSET 0;
```

1 冊の詳細(表紙 URL を含む):

```sql
SELECT asin, title, authors, read_status, acquired_at, product_image_url,
       genres, series_title, series_position
FROM v_books
WHERE asin = 'B000000000'
LIMIT 1;
```
