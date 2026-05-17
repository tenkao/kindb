# kindb

Chrome 拡張「[Kindle bookshelf exporter](https://chromewebstore.google.com/detail/kindle-bookshelf-exporter/olimpmeljimffgjonlpmiaebaonnegdp)」で取得した Kindle 蔵書データ `kindle.json` を DuckDB に取り込み、Claude Desktop や Claude Code / CLI から検索・集計・分析するツール。

- ローカル完結（外部 API への通信なし）
- DuckDB の列指向エンジンで高速クエリ
- Claude Desktop からは MCP サーバ（`mcp-server-motherduck`）経由で DB を直接参照
- Claude Code 向け `SKILL.md` 同梱

## 使用データ

入力は `kindle.json` のみ。ルート配列の各要素が 1 冊に対応し、以下のキーを想定する。

- `title`
- `authors`
- `acquiredTime`
- `readStatus`
- `asin`
- `productImage`（任意）

上記フォーマットに合う JSON を `kindb import` に渡す。

## インストール

Python >= 3.10。

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

## 使い方

### データの取り込み

```bash
kindb import path/to/kindle.json
```

一時 DB に全件取り込み、成功後に既存 DB を置換する。初回・更新とも同じコマンド。差分更新ではなく最新 JSON を毎回フルインポートする。失敗時は既存 DB が残る。

デフォルト DB パスは `~/.kindb/kindle.duckdb`。`--db PATH` で上書き可能。

### DB 状態確認

```bash
kindb status
```

最終インポート日時、蔵書数、著者数、`read_status` 別内訳、画像 URL 保有冊数を表示する。

### 検索

```bash
kindb search ○○○
```

`v_books` に対し、書名・著者文字列・ASIN・読書状態を `ILIKE` で検索する。検索語中の `%` / `_` / `\` はリテラルとして扱う。

### SQL クエリ

```bash
kindb query "SELECT count(*) AS n FROM v_books"
kindb query "SELECT asin, title, authors_text, read_status, acquired_at FROM v_books ORDER BY acquired_at DESC, asin DESC LIMIT 50 OFFSET 0"
kindb query --table "SELECT author_name, book_count FROM v_author_counts ORDER BY book_count DESC, author_name ASC LIMIT 10"
```

`SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ実行できる読み取り専用接続。書き込み系 SQL は拒否される。

`SELECT` / `WITH` で行を返すクエリは、トップレベル末尾の `LIMIT` が必須。全件が必要な場合も、まず `count(*)` で総数を確認し、`LIMIT/OFFSET` でページングして取得する。例外的に制限なしで実行する場合のみ `--allow-unlimited` を明示する。

`ORDER BY` には一意なタイブレーカー(`v_books` は `asin`、`v_author_counts` は `author_name`)を必ず含める。主ソートに `DESC` を使うときはタイブレーカーも `DESC` で揃える。タイブレーカーがないと、同 `acquired_at` の行がページ境界をまたいだ際に `OFFSET` ページングで取りこぼし/重複が起こりうる。

### 集計

```bash
kindb authors         # 著者別の所有冊数
kindb recent          # 最近取得した本（デフォルト 20 冊）
kindb recent -n 50    # 件数指定
```

### DB 削除

```bash
kindb delete          # 確認あり
kindb delete --yes    # 確認スキップ
```

## 主要ビュー

通常は以下のビューを使う。テーブル定義の詳細は [`SKILL.md`](SKILL.md) と [`docs/kindb-v0.2-plan.md`](docs/kindb-v0.2-plan.md) を参照。

- `v_books`: 1 冊 1 行の主ビュー。分割済み著者配列、元の著者文字列、読書状態、表紙 URL、取得日時を含む。
- `v_author_counts`: 著者別冊数。`book_count DESC, author_name ASC` で決定的に並ぶ。

## 扱わない項目

`kindle.json` からは確定できないため、kindb では保存せず、AI からの問い合わせでも断定しない。

- 発売日、出版社、購入価格
- Kindle Unlimited 判定、購入経路
- マンガ / 固定レイアウト判定

`read_status = 'READ'` はユーザーが Kindle 上で付けた読了マークの自己申告フラグ。`UNKNOWN` は「読了マークなし」であり、未読とは断定しない。

`acquired_at` はライブラリ取得日時であり、購入日とは限らない（再ダウンロード等で更新されうる）。

## Claude Desktop MCP 設定

[`mcp-server-motherduck`](https://github.com/motherduckdb/mcp-server-motherduck) を使うと Claude Desktop から kindb の DuckDB に直接クエリできる。

Claude Desktop の設定ファイルに以下を追加する:

```json
{
  "mcpServers": {
    "kindb": {
      "command": "uvx",
      "args": [
        "mcp-server-motherduck",
        "--db-path",
        "<HOME>/.kindb/kindle.duckdb"
      ]
    }
  }
}
```

`<HOME>` は自分のホームディレクトリの絶対パスに置き換える。DB への書き込みは `kindb import` に限定する。

`mcp-server-motherduck` のデフォルト返却上限(1024 行 / 50000 文字)では Kindle 蔵書規模のリスト取得が頻繁に打ち切られる。蔵書ビューア用途では `--max-rows` / `--max-chars` を以下の推奨値まで引き上げた設定を使うとよい:

```json
{
  "mcpServers": {
    "kindb": {
      "command": "uvx",
      "args": [
        "mcp-server-motherduck",
        "--db-path",
        "<HOME>/.kindb/kindle.duckdb",
        "--max-rows",
        "1000",
        "--max-chars",
        "150000"
      ]
    }
  }
}
```

上げすぎは LLM のコンテキストを圧迫するため、ユースケースに応じて調整する。なお `--max-rows` / `--max-chars` はあくまで返却時の打ち切り設定であり、ページングの代替ではない。

### 会話冒頭プロンプト

`SKILL.md` は Claude Code 向けに配布されるが、Claude Desktop には自動で届かないため、Claude Desktop から kindb を使う会話の冒頭に以下を貼り付けると LLM の挙動が安定する:

```
kindb (Kindle 蔵書 DB) を使う。
- 通常は v_books を主に使う(集計は v_author_counts)。
- ORDER BY には一意なタイブレーカー(v_books は asin、v_author_counts は author_name)を必ず含める。主ソートが DESC ならタイブレーカーも DESC で揃える。
- 一覧取得は LIMIT/OFFSET でページングする。先に count(*) で総数を確認し、LIMIT N OFFSET M で反復取得する。--max-rows / --max-chars はページングの代替ではない。
- product_image_url は出力サイズが大きいため、通常の一覧では選択せず、表紙画像が必要な詳細取得時だけ含める。
```

> 補足: 結果が途中で切れる場合は `LIMIT/OFFSET` で次のページを取得する。同じ本が複数回出る/抜ける場合は `ORDER BY` にタイブレーカー(`asin` 等)を必ず追加する。

### MCP 経由の代表クエリ

MCP 経由では `kindb query` CLI を通らないため、CLI の `LIMIT` 必須チェックは適用されない。Claude Desktop から一覧を取得するときも、まず総数を確認してからページングする。

```sql
SELECT count(*) AS n
FROM v_books;

SELECT asin, title, authors_text, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC, asin DESC
LIMIT 50 OFFSET 0;

SELECT asin, title, authors_text, read_status, acquired_at
FROM v_books
ORDER BY acquired_at DESC, asin DESC
LIMIT 50 OFFSET 50;

SELECT asin, title, authors_text, read_status, product_image_url, acquired_at
FROM v_books
WHERE asin = 'B000000000';
```

`product_image_url` は出力サイズが大きいため、通常の一覧では選択せず、表紙画像が必要な詳細取得時だけ含める。

MCP 経由で観測される具体的な失敗モード(打ち切り無視、ページ境界での取りこぼし/重複)とドキュメント整備方針は [`docs/kindb-query-pagination-plan.md`](docs/kindb-query-pagination-plan.md) を参照。

## Claude Code からの利用（SKILL.md）

`SKILL.md` を Claude Code の skills 配置場所にコピーすると、Claude Code が Kindle 蔵書関連の質問に対して `v_books` / `v_author_counts` を使った適切なクエリを自動生成できるようになる。

```bash
mkdir -p ~/.claude/skills/kindb
cp SKILL.md ~/.claude/skills/kindb/SKILL.md
```

## 開発

```bash
ruff check . && pytest
```

テスト用の最小 `kindle.json` は `tests/create_fixture.py` が動的に生成する。

## 関連ドキュメント

- [`docs/kindb-v0.2-plan.md`](docs/kindb-v0.2-plan.md): 実装計画、スキーマ・スコープの公式記述
- [`SKILL.md`](SKILL.md): 生成 AI 向けクエリガイド（代表クエリ集含む）

## ライセンス

MIT
