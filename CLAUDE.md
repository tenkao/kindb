# CLAUDE.md

kindb は、Chrome 拡張で取得した `kindle.json`(主データ)と、任意の公式 `Kindle.zip`(補完データ)を DuckDB に取り込み、Kindle 蔵書を検索・集計するローカルの Python ツール。利用者は Claude Code からは CLI で、Claude Desktop などからは MCP サーバ経由で DB を問い合わせる。

## 文書の地図

- `docs/spec.md`: 入力形式、スキーマ、ビュー、import、query の制約の現行仕様と、その設計判断の理由。これらを変更する前に読む。
- `SKILL.md`: 蔵書を問い合わせる AI 向けの手順と代表クエリ。`~/.claude/skills/kindb/SKILL.md` にシンボリックリンクされ、Claude アプリにも単体でアップロードされる。リポジトリの外で読まれるため、この 1 ファイルだけで完結させる。
- `README.md`: 利用者向けのインストール、CLI、MCP の設定。
- `docs/manual-test-scenarios.md`: 実機での確認手順。CLI の出力や import の挙動を変えたら通す。

## コード構成

- `src/kindb/cli.py`: Typer の CLI。`kindb query` の検証(単一文、トップレベルの LIMIT)もここにある。
- `src/kindb/db.py`: `TABLES_SQL` / `VIEWS_SQL`、`create_schema()` / `ensure_schema()`、DB パスの解決。
- `src/kindb/importer.py`: `import_kindle_json()` / `import_official_zip()`。検証してから、トランザクションで全件置換する。
- `tests/create_fixture.py` / `tests/create_official_fixture.py`: テストと手動確認で使う fixture の生成。期待値の件数はこの内容に依存する。

## 不変条件

理由は `docs/spec.md` にある。

- import は `create_schema()` → `BEGIN` → `DELETE`/`INSERT` → `COMMIT` → `CHECKPOINT` の順に実行する。
- `kindb import` と `kindb import-official` は、それぞれ自分の担当テーブルだけを書き換える。
- スキーマは `create_schema()` の冪等な DDL で作る。既存テーブルの列を変える場合は、移行処理を別に書く。
- `kindb query` の JSON 出力は rich を通さずに標準出力へ書く。

## 変更したときに一緒に更新する先

| 変更内容 | 一緒に更新する先 |
|---|---|
| テーブル、ビュー、列 | `db.py` → `docs/spec.md` → `SKILL.md` → `tests/` → 手動テスト §8 |
| CLI の引数や出力 | `cli.py` → `docs/spec.md` の CLI 節 → `README.md` → 手動テスト |
| import の検証や変換 | `importer.py` → `docs/spec.md` → `tests/` |
| AI 向けの問い合わせ規則 | `SKILL.md` のみ(README は SKILL.md へのリンクに留める) |

## 実行時の注意

`~/.kindb/kindle.duckdb` は利用者の実際の蔵書 DB なので、動作確認は `--db` か `KINDB_DB_PATH` で指定した一時 DB に対して行う。

## Commands

```bash
# Create / update .venv (Python 3.13 pinned by .python-version, dev group included)
# Do not place the project under iCloud-synced dirs (~/Documents etc.): .pth files created there
# get the macOS hidden flag and Python 3.13+ site.py skips them, breaking the editable install.
uv sync

# Run CLI (dev)
uv run kindb <subcommand>

# Global command (editable; rerun after dependency changes or moving the project)
# uv tool install ignores uv.lock, so pin versions via constraints exported from it.
# Never use `uv tool upgrade kindb`: it keeps the constraints stored at install time.
# --python is required: uv tool install does not read the project's .python-version.
# Dependency update procedure: see README.md「依存更新の手順」
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --format requirements.txt -o constraints.txt \
  && uv tool install --editable . --reinstall --python 3.13 --constraints constraints.txt

# Check .venv and tool env drift (same output = in sync)
uv run python -c "import sys, duckdb; print(sys.version.split()[0], duckdb.__version__)"
~/.local/share/uv/tools/kindb/bin/python -c "import sys, duckdb; print(sys.version.split()[0], duckdb.__version__)"

# Lint + Test
uv run ruff check . && uv run pytest
```
