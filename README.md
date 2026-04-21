# kindb

公式 `Kindle.zip` を DuckDB に取り込み、CLI や Claude Desktop / Claude Code から Kindle 蔵書と読書セッションを検索・集計・分析できるローカルツール。

- ローカル完結（外部 API への通信なし）
- DuckDB の列指向エンジンで高速クエリ
- Claude Desktop / Claude Code からは MCP サーバ (`mcp-server-motherduck`) 経由で DB を直接参照
- 生成 AI 向けクエリガイド (`SKILL.md`) 同梱
- 意味が明確な公式データだけを保存（価格・発売日・出版社・読了状態・Kindle Unlimited 判定などの不確かな項目は含めない）

## 使用データ

Amazon アカウントサービスの「データをリクエストする」から Kindle データを請求し、ダウンロードした `Kindle.zip` を使用する。

## インストール

Python >= 3.10。

```bash
# 開発込み（このリポジトリで作業する場合）
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# 利用のみ
pipx install .
```

## 使い方

### データの取り込み

```bash
kindb import ~/Downloads/Kindle.zip
```

一時 DB に全件取り込み、成功後に既存 DB を置換する。初回・更新とも同じコマンド。差分更新ではなく最新 zip を毎回フルインポートする。失敗時は既存 DB が残る。

デフォルト DB パスは `~/.kindb/kindle.duckdb`。`--db PATH` で上書き可能。

### DB 状態確認

```bash
kindb status
```

最終インポート日時、蔵書数、著者数、ジャンル数、読書セッション数、個人文書数を表示する。

### 検索

```bash
kindb search マンガ
```

`v_books_with_reading` に対し、書名・著者・ジャンル・シリーズ・ASIN を `ILIKE` で検索する。

### SQL クエリ

```bash
# JSON 出力（デフォルト）
kindb query "SELECT product_name, last_read_at FROM v_books_with_reading WHERE last_read_at IS NOT NULL ORDER BY last_read_at DESC LIMIT 10"

# テーブル形式出力
kindb query --table "SELECT count(*) FROM v_books"
```

`SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ実行できる読み取り専用接続。書き込み系 SQL は拒否される。

### 集計

```bash
kindb authors         # 著者別の所有冊数
kindb genres          # ジャンル別の所有冊数
kindb series          # シリーズ別の所有冊数と巻位置
kindb recent          # 最近ライブラリに追加された本（デフォルト 20 冊）
kindb recent -n 50    # 件数指定
kindb reading         # ASIN 別の読書セッション集計
```

### DB 削除

```bash
kindb delete          # 確認あり
kindb delete --yes    # 確認スキップ
```

## 主要ビュー

生成 AI からの利用も含め、通常は以下のビューを使う。テーブル定義の詳細は [`SKILL.md`](SKILL.md) と [`docs/kindb-v0.1-plan.md`](docs/kindb-v0.1-plan.md) を参照。

- `v_books`: 1 冊 1 行の基本ビュー。著者・ジャンル・画像 URL を ASIN 単位で正規化してまとめる。
- `v_books_with_reading`: `v_books` に読書セッション集計（セッション数、最終読書日時、累計読書時間、累計ページめくり数）を結合した主ビュー。
- `v_reading_summary`: ASIN 単位の読書集計。`reading_sessions` のみを集計元とし、`reading_insight_sessions` は二重計上回避のため含めない。

## 扱わない項目

公式 `Kindle.zip` からは確定できないため、kindb では保存せず、AI からの問い合わせでも断定しない:

- 読了・未読ステータス（読書セッションの有無は「読み始めたかどうか」の弱い手がかりにしかならない）
- マンガ / 固定レイアウト判定
- Kindle Unlimited 判定、購入経路
- 発売日、出版社、購入価格

`relationship_creation_date` はライブラリ追加日/取得日であり、購入日とは限らない（再ダウンロード等で更新されうる）。

## Claude Desktop MCP 設定

[`mcp-server-motherduck`](https://github.com/motherduckdb/mcp-server-motherduck) を使うと Claude Desktop から kindb の DuckDB に直接クエリできる。

Claude Desktop の設定ファイルに以下を追加する:

| OS | 設定ファイルのパス |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

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

- `<HOME>` は自分のホームディレクトリの絶対パスに置き換えること（例: `/Users/alice`, Windows では `C:\\Users\\alice` のように `\` をエスケープする）
- `uvx` のフルパスを指定すると確実（例: macOS では `/opt/homebrew/bin/uvx`）
- DB への書き込みは `kindb import` に限定する。Claude Desktop からは読み取り用途のみを想定。

## Claude Code からの利用（SKILL.md）

`SKILL.md` を Claude Code の skills 配置場所にコピーすると、Claude Code が Kindle 蔵書関連の質問に対して `v_books` / `v_books_with_reading` を使った適切なクエリを自動生成できるようになる。

```bash
mkdir -p ~/.claude/skills/kindb
cp SKILL.md ~/.claude/skills/kindb/SKILL.md
```

Claude Code からは `kindb query` か、Claude Desktop と同じく `mcp-server-motherduck` 経由で DB を参照する。

## 開発

```bash
ruff check . && pytest
```

テスト用の最小 `Kindle.zip` は `tests/create_fixture.py` が動的に生成する。匿名化済みの日本語書名・複数著者・複数ジャンル・フィルタ対象外行・削除済み行・読書セッション・個人文書を含む。

## 関連ドキュメント

- [`docs/kindb-v0.1-plan.md`](docs/kindb-v0.1-plan.md): 実装計画、スキーマ・スコープの公式記述
- [`SKILL.md`](SKILL.md): 生成 AI 向けクエリガイド（代表クエリ集含む）
- [`CLAUDE.md`](CLAUDE.md): Claude Code 向け作業指示

## ライセンス

MIT
