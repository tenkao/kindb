# kindb

Chrome 拡張などで取得した `kindle.json` を DuckDB に取り込み、Claude Desktop(MCP 経由) や Claude Code / CLI から Kindle 蔵書を検索・集計できるローカルツール。

- ローカル完結（外部 API への通信なし）
- DuckDB の列指向エンジンで高速クエリ
- Claude Desktop / Claude Code からは MCP サーバ (`mcp-server-motherduck`) 経由で DB を直接参照
- 生成 AI 向けクエリガイド (`SKILL.md`) 同梱
- 読了マークは自己申告フラグとして保存し、未読とは断定しない

## 使用データ

入力は `kindle.json` のみ。ルート配列の各要素が 1 冊に対応し、以下のキーを想定する。

- `title`
- `authors`
- `acquiredTime`
- `readStatus`
- `asin`
- `productImage`（任意）

`kindle.json` の取得元 Chrome 拡張は固定しない。上記フォーマットに合う JSON を `kindb import` に渡す。

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
kindb import ~/Downloads/kindle.json
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
kindb search マンガ
```

`v_books` に対し、書名・著者文字列・ASIN・読書状態を `ILIKE` で検索する。検索語中の `%` / `_` / `\` はリテラルとして扱う。

### SQL クエリ

```bash
kindb query "SELECT title, read_status FROM v_books ORDER BY acquired_at DESC LIMIT 10"
kindb query --table "SELECT * FROM v_author_counts LIMIT 10"
```

`SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ実行できる読み取り専用接続。書き込み系 SQL は拒否される。

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

`kindle.json` からは確定できないため、kindb では保存せず、AI からの問い合わせでも断定しない:

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
- [`CLAUDE.md`](CLAUDE.md): Claude Code 向け作業指示

## ライセンス

MIT
