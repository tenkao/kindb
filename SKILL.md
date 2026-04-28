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

## 使い方

```bash
kindb query "<SQL>"          # JSON 出力(デフォルト)
kindb query --table "<SQL>"  # テーブル形式出力
```

許可されるのは `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ。書き込み系 SQL は拒否される。

DB ファイル: `~/.kindb/kindle.duckdb`

## 基本ルール

- **通常は `v_books` を使う**。正規化テーブル(`books`, `book_authors`)を直接参照するのは集計やデバッグ用途に限る。
- `READ` はユーザーが Kindle 上で付けた読了マークの自己申告フラグ。集計に使ってよい。
- `UNKNOWN` は「読了マークなし」。**未読とは断定しない**。読み始めていても READ マークを付けていない場合は `UNKNOWN` のまま。
- 「未読書籍」を聞かれた場合は `WHERE read_status = 'UNKNOWN'` を使ってよいが、回答文では「読了マークが付いていない本」と言い換える。
- 断定してはいけない項目:
  - 発売日 / 出版社 / 購入価格
  - Kindle Unlimited 判定 / 購入経路
  - マンガ / 固定レイアウト判定

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

### `v_author_counts`

| 列 | 説明 |
|---|---|
| `author_name` | 著者名 |
| `book_count` | その著者の冊数 |

`ORDER BY book_count DESC, author_name ASC` で決定的に並ぶ。

## 代表クエリ

### 著者別の冊数 Top 10

```sql
SELECT author_name, book_count
FROM v_author_counts
ORDER BY book_count DESC, author_name ASC
LIMIT 10;
```

### 最近取得した本

```sql
SELECT title, authors, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC
LIMIT 20;
```

### 読了マーク済みの本

```sql
SELECT title, authors, acquired_at
FROM v_books
WHERE read_status = 'READ'
ORDER BY acquired_at DESC;
```

### 読了マークが付いていない本

```sql
SELECT title, authors, acquired_at
FROM v_books
WHERE read_status = 'UNKNOWN'
ORDER BY acquired_at DESC;
```

回答では「未読」ではなく「読了マークが付いていない本」と表現する。

### 表紙付き蔵書リスト

```sql
SELECT title, authors, read_status, product_image_url
FROM v_books
ORDER BY title;
```

## 注意

- `acquired_at` は Kindle ライブラリへの取得日時。購入日の代理指標として弱く扱うのは可だが、断定はしない。
- `product_image_url` が `NULL` の本は、表紙画像が JSON に含まれていなかった本。
- v0.2 では読書セッション、ジャンル、シリーズ、個人文書は扱わない。
