# kindb query pagination plan

## Summary

巨大な MCP / CLI クエリ結果を避けるため、`LIMIT/OFFSET` を使ったページング運用を強化する。

強制力の及ぶ範囲を先に整理する。

- **CLI 経由 (`kindb query`) のみ強制**: CLI に `LIMIT` 必須チェックを追加する。
- **MCP 直結経路 (`mcp-server-motherduck` → DuckDB) は強制対象外**: CLI を通らないため、ドキュメントと Skill による運用ルールで期待値を上げる。
- 独自 MCP サーバを追加して MCP 経路を強制する案は、今回の実装規模を抑えるため対象外とする。

なお CLI 経由は stdout 直結で MCP の 50KB 制限を受けないため、CLI 強制の主目的は「巨大出力そのものの防止」ではなく、**`SKILL.md` で示すページング流儀に CLI 利用も従わせる**ことにある。本丸はドキュメント整備で、CLI 強制はその補強と位置づける。

具体策は以下の2点。

- `README.md` / `SKILL.md` に `LIMIT/OFFSET` ページング運用と「総数確認 → 反復取得」フローを明記する。
- `kindb query` CLI に `LIMIT` 必須チェックを追加する。

## Background

- `mcp-server-motherduck` の 50KB 出力制限により、書籍一覧取得が途中で打ち切られることがある。
- `--max-chars` は返却時の打ち切り設定の緩和であり、構造的にはページングが本来の対処。
- `product_image_url` を含む一覧は1行あたりの文字数が大きく、文字数制限に早く到達しやすい。
- 独自 MCP サーバを作れば強制力は上がるが、今回は実装規模を抑えるため対象外とする。

### MCP 経路で観測される失敗モード

ドキュメント整備で誘導したい失敗は具体的には次の 2 種類。

- **打ち切り無視**: `LIMIT 20` の結果だけを見て全件回答した気になる。返却サイズが `--max-rows` / `--max-chars` を超えたときの切り詰めも同様に見落とす。
- **ページ境界での取りこぼし/重複**: タイブレーカー欠落の `ORDER BY acquired_at DESC` で `OFFSET` ページングすると、同 `acquired_at` の行がページ境界をまたいだ際に重複または欠落が発生しうる。

## Policy

- 一覧取得は `LIMIT/OFFSET` でページングする。
- 全件が必要な場合も、まず `count(*)` で総数を確認し、必要な範囲だけページ単位で取得する。
- `product_image_url` は必要なときだけ選択する。通常の一覧では `asin`, `title`, `authors_text`, `read_status`, `acquired_at` 程度に絞る。
- `mcp-server-motherduck` の `--max-rows` / `--max-chars` は返却時の打ち切り設定であり、ページングの代替とは扱わない。
- **`ORDER BY` には一意キーを含めて決定的順序にする**。`v_books` の標準タイブレーカーは `asin`、`v_author_counts` は `author_name`。主ソートが `DESC` なら `, asin DESC`、`ASC` なら `, asin ASC` と方向を揃える。これが欠けると `OFFSET` ページング中に同値行がページ境界をまたいで取りこぼし/重複の原因になる。

## CLI Specification

`kindb query` に `LIMIT` 必須チェックを追加する。

### 対象判定

- `SELECT` / `WITH` の行返却クエリは `LIMIT` 必須にする。
- `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` は対象外。
- 集計クエリは下記の「集計クエリの定義」を満たすもののみ `LIMIT` 不要として許可する。
- 例外用に `--allow-unlimited` を追加し、明示した場合だけ `LIMIT` なしを許可する。
- 拒否時のエラーメッセージには、`LIMIT/OFFSET` 付きの修正例と `--allow-unlimited` の案内を**両方**含める。

### `LIMIT` の位置判定

「クエリ文字列中に `LIMIT` が含まれる」だけでは、CTE 内やサブクエリ内の `LIMIT` で簡単にすり抜けられるため、判定は次の通り保守的に行う。

- **トップレベルクエリ末尾に `LIMIT n [OFFSET m]` がある場合のみ「`LIMIT` あり」と判定する**。具体的には、文末セミコロンと末尾空白を取り除いた後の末尾トークン列が `LIMIT <整数> (OFFSET <整数>)?` であること。
- 文字列リテラル (`'LIMIT'`) と `--` / `/* */` コメントは判定前に除去する。
- 大文字小文字は無視する(`LIMIT`, `Limit`, `limit` を同一視)。
- `FETCH FIRST n ROWS ONLY` は今回の判定では「`LIMIT` あり」と扱わない(拒否)。利用したい場合は `--allow-unlimited` を使う。
- サブクエリ内 / CTE 内の `LIMIT` のみでトップレベル `LIMIT` がないクエリは拒否する(誤判定で安全側に倒す)。

この判定は構文木に依らない文字列ベースの近似であり、複雑な SQL での誤拒否は `--allow-unlimited` を退路とする。

### 集計クエリの定義

`LIMIT` なしでも許可する「集計クエリ」は、以下の両方を満たすものに限定する。

- `SELECT` 句が集計関数(`count` / `sum` / `avg` / `min` / `max` 等)のみで構成され、非集計列を含まない。
- クエリに `GROUP BY` を含まない。

これにより `SELECT count(*) FROM v_books` や `SELECT count(*) AS n FROM v_books` は許可されるが、`SELECT read_status, count(*) FROM books GROUP BY read_status` のような **`GROUP BY` で行数が増えうるクエリは `LIMIT` 必須**になる。判定はトップレベル `SELECT` 句に対してのみ行う(サブクエリ内の集計関数は対象外)。

### 想定する挙動

```bash
kindb query "SELECT title FROM v_books ORDER BY title LIMIT 100 OFFSET 0"
kindb query "SELECT count(*) AS n FROM v_books"
kindb query --allow-unlimited "SELECT title FROM v_books ORDER BY title"
```

上記は許可する。

```bash
kindb query "SELECT title FROM v_books ORDER BY title"
kindb query "SELECT * FROM v_books"
kindb query "WITH rows AS (SELECT * FROM v_books) SELECT * FROM rows"
kindb query "WITH t AS (SELECT * FROM v_books LIMIT 10) SELECT b.* FROM v_books b JOIN t USING(asin)"
kindb query "SELECT read_status, count(*) FROM books GROUP BY read_status"
kindb query "SELECT title FROM v_books FETCH FIRST 10 ROWS ONLY"
```

上記は拒否する。

### スコープ外

`LIMIT 10000` のような大きすぎる値の上限チェックは、この変更では入れない。理由は CLI 経由は stdout 直結で MCP の 50KB 制限を受けないため、CLI で巨大 `LIMIT` 自体は問題にならないこと。巨大出力が顕在化した時点で別途検討する。まずは `LIMIT` 必須化とドキュメント整備に絞る。

## Documentation Updates

`SKILL.md`:

- 基本ルールに「一覧取得は必ず `LIMIT/OFFSET` を付ける」を追加する。
- 基本ルールに「`ORDER BY` には一意キーを含める(`v_books` は `asin`、`v_author_counts` は `author_name`)」を追加する。
- **ページング標準フロー**を明示する。これは AI が「`LIMIT 20` の結果だけ見て全件回答した気になる」誤動作を防ぐためのもの。
  1. `SELECT count(*)` で総数を確認する。
  2. 必要な範囲だけ `LIMIT N OFFSET M` で取得する。
  3. 全件回答が必要な場合は `OFFSET` を進めて反復取得し、取得結果が `count(*)` の総数と一致してから回答する。
- 代表クエリを `LIMIT/OFFSET` 付きに変更し、`ORDER BY` に `asin` などのタイブレーカーを反映する。
- 表紙付き蔵書リストは全件取得ではなく、ページング前提の例にする。
- `product_image_url` は出力サイズが大きくなりやすいため、必要時のみ選択する方針を明記する。
- 集計クエリの代表例として、`GROUP BY` を使う年別取得冊数を追加する。

`README.md`:

- Claude Desktop MCP 設定セクションに、`--max-rows` / `--max-chars` とページングの違いを追記する。デフォルト値(1024 行 / 50000 文字)では蔵書規模で打ち切られやすいため、推奨値(`--max-rows 1000` / `--max-chars 150000`)を含む設定例を、現状の値なし設定例と併記する。
- **会話冒頭プロンプト**サブ節を新設し、Claude Desktop 向けに 3〜5 行の貼り付け用テンプレ(`v_books` を主に使う / `ORDER BY` に一意キー必須 / 一覧は `LIMIT/OFFSET` で、`count(*)` で先に規模確認)を置く。SKILL.md は Claude Desktop には配布されないことへの代替。
- 同節末尾に「結果が途中で切れる場合は `LIMIT/OFFSET` でページング」「同じ本が複数回出る/抜ける場合はタイブレーカーを `ORDER BY` に追加」の脚注 2 行を吸収する(独立 Q&A 節は作らない)。
- MCP 経由の推奨クエリとして、`count(*)`、`LIMIT/OFFSET` 付き一覧、必要時のみ詳細取得の流れを載せる。
- 代表 SQL 例を `LIMIT/OFFSET` 付き・タイブレーカー付きに更新し、2 ページ目の例(`OFFSET 50`)を 1 行追加する。
- MCP 章末尾に、詳細な失敗モード解説として本ドキュメント (`docs/kindb-query-pagination-plan.md`) への参照リンク 1 行を置く。

## Test Plan

基本系:

- `LIMIT` なしの通常 `SELECT` が拒否される。
- `LIMIT/OFFSET` 付きの `SELECT` が通る。
- トップレベル末尾に `LIMIT n` のみ(`OFFSET` なし)の `SELECT` が通る。
- `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` が通る。
- `--allow-unlimited` 付きなら `LIMIT` なしでも通る。
- `WITH` クエリの行返却も `LIMIT` なしなら拒否される。
- 拒否時のエラーメッセージに `LIMIT/OFFSET` の修正例と `--allow-unlimited` の案内が両方含まれる。
- `--allow-unlimited` 通過時はエラーメッセージが表示されない。
- `--table` モードでも上記拒否ロジックが同じく適用される。

集計クエリ:

- `SELECT count(*) FROM v_books` が通る。
- `SELECT count(*) AS n FROM v_books` が通る。
- `SELECT sum(book_count) FROM v_author_counts` が通る。
- `SELECT read_status, count(*) FROM books GROUP BY read_status` は **`LIMIT` なしでは拒否される**(`GROUP BY` を含むため)。
- `SELECT author_name, count(*) FROM book_authors GROUP BY author_name` は `LIMIT` なしでは拒否される。

`LIMIT` 位置判定の境界:

- 大文字小文字混在(`Limit`, `LIMIT`, `limit`)がすべて同等に「`LIMIT` あり」と判定される。
- 末尾セミコロン・末尾空白付きの `LIMIT n;` / `LIMIT n  ` が通る。
- 文字列リテラル内の `LIMIT`(例: `SELECT 'LIMIT' AS x FROM v_books`)はトップレベル `LIMIT` と誤判定されず、`LIMIT` なしとして拒否される。
- 行コメント `-- LIMIT 10` / ブロックコメント `/* LIMIT 10 */` 内の `LIMIT` も同様に誤判定されない。
- サブクエリ内のみに `LIMIT` がある `SELECT * FROM (SELECT * FROM v_books LIMIT 10) t` はトップレベル `LIMIT` がないため拒否される。
- CTE 内のみに `LIMIT` がある `WITH t AS (SELECT * FROM v_books LIMIT 10) SELECT * FROM t` も拒否される。
- `OFFSET` 単独(`SELECT * FROM v_books OFFSET 10`)は `LIMIT` がないため拒否される。
- `FETCH FIRST n ROWS ONLY` は今回の判定対象外として拒否される(`--allow-unlimited` で許可)。

検証コマンド:

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

## Assumptions

- SQL 解析には新規依存を追加せず、保守的な文字列判定で進める。
- 複雑な SQL が誤拒否される場合は、`LIMIT` 追加または `--allow-unlimited` を使う設計にする。
