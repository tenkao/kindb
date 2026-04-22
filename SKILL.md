---
name: kindb
description: Kindle 蔵書と読書セッションの検索・集計・分析。「Kindle の本」「蔵書」「読書記録」「読んだ本」「読書時間」「著者別冊数」「ジャンル別冊数」に言及された場合や、Kindle ライブラリのキーワード検索、読書傾向分析、読書セッション集計を行う場合にこの Skill を使う。
---

# kindb SKILL

kindb は公式 `Kindle.zip` を DuckDB に取り込み、Kindle 蔵書と読書セッションをローカルで検索・集計するツール。Claude Desktop(MCP 経由) や Claude Code / CLI からこの DB を参照するときの、生成 AI 向け指針をまとめる。

## 前提条件

まず DB の状態を確認する:

```bash
kindb status
```

DB が存在しない場合は `kindb import <Kindle.zip>` で取り込みが必要。

## 使い方

```bash
kindb query "<SQL>"          # JSON 出力(デフォルト)
kindb query --table "<SQL>"  # テーブル形式出力
```

許可されるのは `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ。書き込み系 SQL は拒否される。

DB ファイル: `~/.kindb/kindle.duckdb`

## 基本ルール

- **通常は `v_books` / `v_books_with_reading` を使う**。正規化テーブル(`books`, `book_authors`, `book_genres` など)を直接参照するのは集計やデバッグ用途に限る。
- **断定してはいけない項目**:
  - 読了・未読(公式データに存在しない。読書セッションの有無は「読み始めたかどうか」の弱い手がかりにしかならない)
  - マンガ / 固定レイアウト判定(公式データに確定フラグがない)
  - Kindle Unlimited / 購入経路(サンプルは import 時のフィルタで除外されるため DB に存在しない)
  - 発売日・出版社・購入価格
  - これらが問われた場合は「公式 Kindle.zip からは判定できない」と明示する。

## 主要ビュー

### `v_books`(1 冊 1 行の基本ビュー)

| 列 | 説明 |
|---|---|
| `asin` | Amazon ASIN |
| `product_name` | 書名 |
| `sortable_title` / `sortable_author_name` | ソート用 |
| `authors` | 著者名の配列(`LIST<VARCHAR>`)。該当 ASIN に `book_authors` 行が無ければ `NULL` |
| `genres` | ジャンルの配列(`LIST<VARCHAR>`)。該当 ASIN に `book_genres` 行が無ければ `NULL` |
| `image_url` | 代表カバー画像 URL(ASIN ごとに決定的に 1 件) |
| `series_title` / `series_author` / `position_in_collection` | シリーズ情報 |
| `marketplace` | 購入マーケット(`JP` / `US` など) |
| `relationship_creation_date` | ライブラリ追加日/取得日。**購入日とは限らない**(再ダウンロード等で更新される) |

### `v_books_with_reading`(`v_books` + 読書集計)

`v_books` の全列に加えて:

| 列 | 説明 |
|---|---|
| `reading_session_count` | `reading_sessions` のセッション数 |
| `first_read_at` / `last_read_at` | 最初/最後の読書開始時刻 |
| `total_reading_millis` | 累計読書時間(ミリ秒) |
| `total_page_flips` | 累計ページめくり数 |

`reading_sessions` に該当 ASIN の行が無い場合、これら 5 列はすべて `NULL`。**ただし「NULL = 未読」ではない**(記録が残っていない古い読書や別デバイス・別アカウントの読書はこの DB に入らない)。

### `v_reading_summary`(ASIN 単位の読書集計)

`reading_sessions` のみを集計元とする。`reading_insight_sessions` は重複の可能性があるため含まない(二重計上を避ける)。

## 代表クエリ

### 著者別の冊数 Top 10

```sql
SELECT t.author, count(*) AS books
FROM v_books, unnest(v_books.authors) AS t(author)
GROUP BY t.author
ORDER BY books DESC
LIMIT 10;
```

### 最近読んだ本

```sql
SELECT product_name, last_read_at, total_reading_millis
FROM v_books_with_reading
WHERE last_read_at IS NOT NULL
ORDER BY last_read_at DESC
LIMIT 20;
```

### シリーズの所有巻一覧

```sql
SELECT series_title,
       list(DISTINCT position_in_collection ORDER BY position_in_collection) AS positions,
       count(*) AS books
FROM v_books
WHERE series_title IS NOT NULL AND series_title <> ''
GROUP BY series_title
ORDER BY books DESC;
```

### 読書時間の長い本 Top 20

```sql
SELECT product_name,
       total_reading_millis / 1000 / 60 AS minutes,
       reading_session_count
FROM v_books_with_reading
WHERE total_reading_millis IS NOT NULL
ORDER BY total_reading_millis DESC
LIMIT 20;
```

### ジャンル別の冊数

```sql
SELECT t.genre, count(*) AS books
FROM v_books, unnest(v_books.genres) AS t(genre)
GROUP BY t.genre
ORDER BY books DESC;
```

## 注意

- `relationship_creation_date` は Kindle ライブラリへの追加/更新日。購入日の代理指標として弱く扱うのは可だが、断定はしない。
- `image_url` は `CustomerTags_FE.csv` 由来で、ASIN あたり複数ある場合は `min()` で決定的に 1 件を選んでいる。最新版・高解像度の保証はない。
- `reading_sessions.content_type` は `EBOOK` 以外(個人文書など)が混ざりうるが、`v_books_with_reading` では `books` と JOIN されるため個人文書 ASIN は自然に除外される。
- 個人文書(PDF 等)は `personal_documents` テーブルで別管理。`books` / `v_books` には含まれない。
- 読書セッションが存在しない本が「未読」であるとは限らない(古い読書、別デバイス、別アカウントの読書は記録されないことがある)。
