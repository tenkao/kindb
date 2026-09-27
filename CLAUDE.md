# CLAUDE.md

kindb は、Chrome 拡張で取得した `kindle.json`(主データ)と、任意の公式 `Kindle.zip`(補完データ)を DuckDB に取り込み、Kindle 蔵書を検索・集計するローカルの Python ツール。任意で NDL サーチから紙版の書誌情報を取得して付ける(`kindb enrich`)。利用者は Claude Code からは CLI で、Claude Desktop などからは MCP サーバ経由で DB を問い合わせる。

## 文書の地図

- `docs/spec.md`: 入力形式、スキーマ、ビュー、import、query の制約、書誌情報の取得と照合の現行仕様と、その設計判断の理由。これらを変更する前に読む。
- `SKILL.md`: 蔵書を問い合わせる AI 向けの手順と代表クエリ。`~/.claude/skills/kindb/SKILL.md` にシンボリックリンクされ、Claude アプリにも単体でアップロードされる。リポジトリの外で読まれるため、この 1 ファイルだけで完結させる。
- `README.md`: 利用者向けのインストール、CLI、MCP の設定。
- `docs/manual-test-scenarios.md`: 実機での確認手順。CLI の出力や import の挙動を変えたら通す。
- `docs/glossary.md`: 用語集。仕様や会話で語の意味が揺れたら、ここに合わせるか、ここを直す。
- `docs/bibinfo-requirements.md` / `docs/bibinfo-plan.md`: NDL の書誌情報を追加する機能の要件と実装計画。現行仕様は `docs/spec.md` の「書誌情報」に移したので、この 2 つは理由と経緯の記録として読む。

## コード構成

- `src/kindb/cli.py`: Typer の CLI。
- `src/kindb/sqlguard.py`: `kindb query` の検証(単一文、トップレベルの LIMIT)と、`enrich --where` の単一文の検査。
- `src/kindb/db.py`: `TABLES_SQL` / `VIEWS_SQL`、`create_schema()` / `ensure_schema()`、DB パスの解決。`v_ndc_labels` は `src/kindb/data/ndc9_3digit.tsv` から組み立てる。
- `src/kindb/importer.py`: `import_kindle_json()` / `import_official_zip()`。検証してから、トランザクションで全件置換する。
- `src/kindb/ndl.py`: NDL サーチ OpenSearch の呼び出し(間隔、429)と RSS の解析。標準ライブラリだけで書く。
- `src/kindb/matching.py`: 書名の正規化と照合の規則。通信も DB も扱わない純粋関数。
- `src/kindb/enrich.py`: `run_enrich()` / `run_rematch()`。対象の選択、手動訂正、取得の進行、まとめた書き込み。
- `scripts/build_ndc_table.py`: 配布元の `ndc9.ttl` から NDC の分類名の TSV を作る。
- `tests/create_fixture.py` / `tests/create_official_fixture.py`: テストと手動確認で使う fixture の生成。期待値の件数はこの内容に依存する。
- `tests/ndl_fixtures.py`: NDL の応答を組み立てる関数と、通信しない `FakeOpenSearch`。テストは NDL に通信しない。

## 不変条件

理由は `docs/spec.md` にある。

- import は `create_schema()` → `BEGIN` → `DELETE`/`INSERT` → `COMMIT` → `CHECKPOINT` の順に実行する。
- `kindb import` と `kindb import-official` は、それぞれ自分の担当テーブルだけを書き換え、`bib_*` に触れない。`bib_*` を書くのは `enrich` と `rematch` だけで、手動訂正を変えられるのは `enrich` だけ。
- `enrich` は取得中に DB を開かない。一定冊数ごとに、import と同じ順で対象 ASIN の `bib_*` 行だけを置き換える。
- `enrich` と `rematch` は DB ごとに 1 つだけ動く(`<db>.enrich.lock` の flock)。`delete` はこのファイルも消す。
- 照合は精度を優先する。誤った書誌情報を付けるより何も付けない。規則を緩めるときは、標本での誤照合が増えないことを確かめる。
- スキーマは `create_schema()` の冪等な DDL で作る。`TABLES_SQL` / `VIEWS_SQL` のハッシュが変われば読み取り系コマンドが自動で移行するので、版番号の管理は要らない。既存テーブルの列を変える場合は、移行処理を別に書く。
- 読み取り系コマンドは、スキーマが最新なら書き込み接続を開かない。並列実行や MCP サーバと共存させるため。
- `kindb query` の JSON 出力は rich を通さずに標準出力へ書く。

## 変更したときに一緒に更新する先

| 変更内容 | 一緒に更新する先 |
|---|---|
| テーブル、ビュー、列 | `db.py`(`books` などの列を足すなら `importer.py` と既存テーブルの移行処理も) → `docs/spec.md` → `SKILL.md` → `tests/` → 手動テスト §8 |
| CLI の引数や出力 | `cli.py` → `docs/spec.md` の CLI 節 → `README.md` → 手動テスト |
| import の検証や変換 | `importer.py` → `docs/spec.md` → `tests/` |
| AI 向けの問い合わせ規則 | `SKILL.md` → README の会話冒頭文(Skill を使えない環境向けの要約) |
| 照合の規則や検索の段 | `matching.py` → `docs/spec.md` の「書誌情報」 → `tests/test_matching.py`(検索の段なら `tests/test_enrich.py` も)。保存済みの候補で直せる変更なら `kindb rematch`、検索が変わるなら `enrich --refresh` が要ると利用者に伝える |
| NDC の分類名の表 | `scripts/build_ndc_table.py` で TSV を作り直す → `docs/spec.md` の件数 |

## 実行時の注意

`~/.kindb/kindle.duckdb` は利用者の実際の蔵書 DB なので、動作確認は `--db` か `KINDB_DB_PATH` で指定した一時 DB に対して行う。

`kindb enrich` は NDL サーチに書名と著者を送り、3 秒以上の間隔で問い合わせる。動作確認で実際に通信するのは利用者の了承を得たときだけにし、`--limit` で冊数を絞る。

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
