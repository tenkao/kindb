# kindb

Chrome 拡張「[Kindle bookshelf exporter](https://chromewebstore.google.com/detail/kindle-bookshelf-exporter/olimpmeljimffgjonlpmiaebaonnegdp)」で取得した Kindle 蔵書データ `kindle.json` を DuckDB に取り込み、Claude Code や Claude Desktop から検索・集計するツール。Amazon 公式の `Kindle.zip` を追加で取り込むと、ジャンル、シリーズ、Amazon 著者 ID も使える。

- ローカルで完結する(外部 API への通信なし)
- Claude Code からは CLI(`kindb query`)で、Claude Desktop からは MCP サーバ(`mcp-server-motherduck`)で DB を問い合わせる
- 問い合わせ方を Claude に教える Skill(`SKILL.md`)を同梱

## 使用データ

主入力は `kindle.json`。ルート配列の各要素が 1 冊に対応し、以下のキーを想定する。

- `title`
- `authors`
- `acquiredTime`
- `readStatus`
- `asin`
- `productImage`(任意)

Amazon のアカウントサービスから取得した公式 `Kindle.zip` は任意。`kindle.json` にないジャンル、シリーズ、Amazon 著者 ID、公式著者名を補う。

入力形式と検証規則の詳細は [`docs/spec.md`](docs/spec.md) を参照。

## インストール

Python >= 3.10。開発環境は [uv](https://docs.astral.sh/uv/) で管理し、`.python-version` で Python 3.13 に固定している。

```bash
uv sync                          # .venv を作成し、開発依存込みでインストール

# kindb をグローバルコマンドとして ~/.local/bin に入れる(依存版を uv.lock に揃える)
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --format requirements.txt -o constraints.txt \
  && uv tool install --editable . --reinstall --python 3.13 --constraints constraints.txt
```

`uv tool install` は `uv.lock` を読まないため、lock から生成した `constraints.txt` で tool 環境の依存版をテスト済みの版に揃える。`constraints.txt` は毎回生成するファイルで、コミットしない。

依存を更新するときは「[開発](#開発)」の「依存更新の手順」に従う。`uv tool upgrade kindb` は初回インストール時の制約をそのまま使い、lock の更新を反映しないので使わない。

uv を使わない場合は、任意の仮想環境で `pip install -e .` を実行する(ランタイム依存のみ)。

## 使い方

DB は既定で `~/.kindb/kindle.duckdb` に作られる。各コマンドの `--db PATH` か、環境変数 `KINDB_DB_PATH` で変えられる。

### 取り込み

```bash
kindb import path/to/kindle.json            # 初回も更新も同じ。毎回全件を置き換える
kindb import-official path/to/Kindle.zip    # 任意。公式データを追加する
```

どちらも 1 つのトランザクションで置き換えるので、失敗したときは取り込み前のデータが残る。2 つの取り込みは互いのデータに触れないため、`kindle.json` を取り込み直しても公式データは消えない。

### 確認と検索

```bash
kindb status          # 取り込み日時、冊数、著者数、読了マーク別の冊数など
kindb search 検索語    # 書名、著者、ASIN、読了マークの部分一致(既定 50 件、-n で変更、-n 0 で全件)
kindb authors         # 著者別の冊数(既定 50 人、-n で変更、-n 0 で全員)
kindb recent          # 最近ライブラリに入った本(既定 20 冊、-n で変更)
```

### SQL で問い合わせる

```bash
kindb query "SELECT count(*) AS n FROM v_books"
kindb query --table "SELECT author_name, book_count FROM v_author_counts ORDER BY book_count DESC, author_name LIMIT 10"
```

読み取り専用で、`SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` の単一文だけを実行できる。出力は既定で JSON、`--table` で表になる。

行を返す `SELECT` / `WITH` には、末尾に `LIMIT` が必要(集計関数だけの SELECT は不要)。件数を数えてからページングで取得させるための制約で、意図して全件を取るときは `--allow-unlimited` を付ける。

ビューの一覧と代表クエリは [`SKILL.md`](SKILL.md)、定義の詳細は [`docs/spec.md`](docs/spec.md) を参照。

### 削除

```bash
kindb delete          # 確認あり
kindb delete --yes    # 確認なし
```

## データの読み方

- `read_status = 'READ'` は、Kindle で付けた読了マーク。`UNKNOWN` は読了マークがないことだけを表し、未読とは限らない。
- `acquired_at` はライブラリに入った日時(UTC)。再ダウンロードなどで更新されるため、購入日とは限らない。
- 発売日、出版社、価格、Kindle Unlimited かどうか、購入経路、マンガかどうかは保存しない。公式 zip に価格の列はあるが取り込まない。

## Claude から使う

### Claude Code

`SKILL.md` を Skill として登録すると、蔵書について聞いたときに Claude Code が `kindb query` で問い合わせる。シンボリックリンクにしておくと、リポジトリの更新がそのまま反映される。リポジトリのルートで実行する。

```bash
mkdir -p ~/.claude/skills/kindb
ln -s "$(pwd)/SKILL.md" ~/.claude/skills/kindb/SKILL.md
```

下の手順で Claude アプリにも Skill をアップロードすると、同じ Skill が Claude Code に `anthropic-skills:kindb` として同期され、ローカル版と二重に並ぶ。ローカル版だけを使うには、`~/.claude/settings.json` に次を加える(キーを `kindb` にすると両方が止まる)。

```json
{
  "skillOverrides": {
    "anthropic-skills:kindb": "off"
  }
}
```

### Claude Desktop

1. **MCP サーバを設定する。** Claude Desktop の設定ファイルに以下を追加する。`<HOME>` は自分のホームディレクトリの絶対パスに置き換える。サーバ名は `kindb` のままにする(`SKILL.md` がこの名前でサーバを見分けるため)。

   ```json
   {
     "mcpServers": {
       "kindb": {
         "command": "uvx",
         "args": [
           "mcp-server-motherduck",
           "--db-path", "<HOME>/.kindb/kindle.duckdb",
           "--max-rows", "1000",
           "--max-chars", "150000"
         ]
       }
     }
   }
   ```

   `mcp-server-motherduck` は既定で読み取り専用で、問い合わせごとに接続を開き直す。このため Claude Desktop を起動したままでも `kindb import` を実行できる。問い合わせの実行中にたまたま重なると `Database is in use by another process` で失敗するので、少し待って再実行する。`--max-rows` / `--max-chars` は結果を切り詰める上限で、既定の 1,024 行 / 50,000 文字から上げている。上げすぎると Claude のコンテキストを圧迫するので、用途に合わせて調整する。

2. **Skill をアップロードする。** `SKILL.md` は MCP での使い方も含む。フォルダに入れて ZIP にし、Claude の Customize > Skills からアップロードする(Settings > Capabilities で Code execution を有効にしておく必要がある)。`SKILL.md` を更新したら ZIP を作り直してアップロードし直す。

   ```bash
   mkdir -p /tmp/kindb-skill/kindb && cp SKILL.md /tmp/kindb-skill/kindb/ \
     && (cd /tmp/kindb-skill && zip -r kindb-skill.zip kindb)
   ```

   Skill を使えない環境では、会話の最初に次の文を貼る。

   ```
   kindb(Kindle 蔵書 DB)を使う。本の一覧は v_books、著者別の冊数は v_author_counts を使う。一覧は count(*) で総数を確かめてから LIMIT/OFFSET でページングし、ORDER BY の最後に asin などの一意な列を置く。read_status = 'UNKNOWN' は「読了マークが付いていない本」と表現する。
   ```

## 開発

```bash
uv run ruff check . && uv run pytest
```

テスト用の最小 `kindle.json` と `Kindle.zip` は `tests/create_fixture.py` と `tests/create_official_fixture.py` が動的に生成する。実機での確認手順は [`docs/manual-test-scenarios.md`](docs/manual-test-scenarios.md) にある。

### 依存更新の手順

```bash
uv lock --upgrade \
  && uv sync \
  && uv run ruff check . \
  && uv run pytest \
  && uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --format requirements.txt -o constraints.txt \
  && uv tool install --editable . --reinstall --python 3.13 --constraints constraints.txt \
  && uv run python -m tests.create_fixture \
  && kindb import tests/fixtures/kindle.json --db /tmp/kindb_check.duckdb \
  && kindb status --db /tmp/kindb_check.duckdb
```

全体を `&&` で 1 本につなぎ、lint やテストが失敗した時点で止める。未検証の依存で tool を入れ直す経路を残さないため(対話シェルに `set -e` を貼るとシェル自体が終了するので使わない)。最後の 2 行は tool 環境で import(書き込み経路)と status(読み取り経路)を通す確認で、実 DB ではなく fixture から作る一時 DB を使う。

特定パッケージだけ上げるなら先頭を `uv lock --upgrade-package <name>` に置き換える。テストが失敗したら `git restore uv.lock` で更新前の lock に戻し、`--upgrade-package` で通るパッケージだけ個別に上げて再実行する。`uv lock --upgrade` は lock 全体を書き換えているため、戻さずに `--upgrade-package` を重ねても問題のパッケージは戻らない。

## 関連ドキュメント

- [`docs/spec.md`](docs/spec.md): 現行仕様と設計判断
- [`SKILL.md`](SKILL.md): Claude 向けの問い合わせ手順、ビュー、代表クエリ
- [`docs/manual-test-scenarios.md`](docs/manual-test-scenarios.md): 実機での確認手順

## ライセンス

MIT
