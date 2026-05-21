# kindb v0.3 実装計画 (公式 kindle.zip オプション取り込み)

## Summary

- v0.2 の **`kindle.json` を主データソースとする運用はそのまま維持**する(ユーザーから見た full-import semantics: 全件置換 / 失敗時の atomicity / 成功時に最新スナップショットに揃う、は不変)。
- Amazon 公式の「アカウントのダウンロード」アーカイブ (`Kindle.zip`) を**オプションの拡張データソース**として追加し、`kindle.json` では取得できない以下の per-book 情報を補完する:
  - ジャンル (1 ASIN に複数)
  - シリーズ名・シリーズ ASIN・シリーズ著者・巻番号
  - Amazon 著者 ID(著者の名寄せ用)
- 取り込みコマンド `kindb import-official <kindle.zip>` を追加する。
- 両 import は**同一 DB 内の単一トランザクションで該当テーブル群を DELETE + INSERT する全件置換方式**に統一する。v0.2 の `kindb import` が使っていた「一時 DB + `os.replace`」方式は廃止し、`books` / `book_authors` / `import_metadata` も同じ DB ファイル内のトランザクションで置換する。zip 由来テーブル(`book_genres` 等)はそもそも別テーブルなので、kindle.json の再 import で消えない。
- 同名異 ID の著者を区別できるよう、Amazon 著者 ID に加えて著者名 (`CustomerAuthorNameRelationship_FE.csv`) も取り込み、ID ベースで集計する `v_author_id_counts` を追加する。
- 2 種類の import は独立して動作し、互いに相手のデータを破壊しない。`v_books` は zip 由来テーブルを `LEFT JOIN`(または相関サブクエリ)で取り込むため、zip を取り込んでいない環境では拡張列が `NULL` / 空配列になるだけで従来通り動作する。
- 公式 zip 取り込み時、`Deleted By Customer = Yes` の本は除外する。`kindle.json` 側にも削除済みの本は出ない前提で、両ソースとも「現役の蔵書のみ」で揃える。
- v0.2 で作成済みの DB を v0.3 バイナリで開いた時のために、軽量マイグレーション関数 `ensure_schema()` を導入し、読み取り系 CLI が接続前に自動でスキーマを最新化する。

## 入力フォーマット (Kindle.zip)

実データ(2026-04 取得分、約 3,000 ASIN)で精査済み。zip 内の以下のファイルのみ参照する。それ以外のディレクトリ(端末イベントログ、KU 加入情報、読書セッション等)は v0.3 のスコープ外。

### 取り込み対象ファイル

| パス | 用途 | 1 ASIN あたり |
|---|---|---|
| `Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerGenres_FE/*.csv` | ジャンル(複数) | 1:N |
| `Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerRelationshipIndex_FE/*.csv` | シリーズ・巻番号・削除フラグ | 主に 1:1、稀に 1:N |
| `Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerAuthorIdRelationship_FE/*.csv` | Amazon 著者 ID(順序付き複数) | 1:N(最大 9) |
| `Kindle.UnifiedLibraryIndex/datasets/Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship_FE/*.csv` | 著者名(順序付き複数。翻訳者等を含む) | 1:N(最大 23) |

### 各 CSV のカラム(取り込み対象)

- **CustomerGenres_FE**: `Product Name, ASIN, Genre`
  - `Genre` をそのまま保存。`・` などの記号は分割しない(genre 文字列の一部)。
- **CustomerRelationshipIndex_FE**: 17 列の中から以下を取り込む。
  - `ASIN`
  - `Series Title` (例: `"異世界迷宮でハーレムを B07D4FP6XQ"`) → `^(.+) (B[0-9A-Z]{9})$` で末尾 ASIN を分離。マッチしなければ全体を `series_title`、`series_asin` を空文字 `''` (sentinel) として保存する。view 側で `NULLIF(series_asin, '')` で NULL に戻す。
  - `Series Author` (例: `"氷樹 一世 B07D4FP6XQ"`) → 同じ規則で `series_author` と `series_author_id` に分離。
  - `Position In Collection` → 整数 parse 可能なら INTEGER、それ以外(`Not Available` 等)は `NULL`。
  - `Relation Type` (例: `PRIMARY`) → 文字列保存。
  - `Deleted By Customer` → `Yes` の行は **取り込み時にスキップ**。
  - 上記以外の列(価格・marketplace・ownership_type 等)は v0.3 では取り込まない。
- **CustomerAuthorIdRelationship_FE**: `Product Name, ASIN, Author ID`
  - 同一 ASIN の連続行を CSV 出現順に `author_order` 1 始まりで投入。
- **CustomerAuthorNameRelationship_FE**: `Product Name, ASIN, Author Name`
  - 同一 ASIN の連続行を CSV 出現順に `author_order` 1 始まりで投入。翻訳者・イラストレーター等を含むため `book_author_ids` より行数が多い(実データで 4,145 行 vs 3,614 行)。

### 取り込みスコープ外(zip 内ファイル)

明示的に取り込まないもの:

- `Digital.Content.Ownership/*.json` (取得日時は kindle.json を優先する。zip 側の `acquiredDate` は保持しない)
- `CustomerOrders_FE.csv` (`Order ID` 等。注文情報は v0.3 のスコープ外)
- `Kindle.Devices.ReadingSession/*.csv`, `Kindle.ReadingInsights/*` (読書セッション。スコープ外)
- `Kindle.Devices.autoMarkAsRead.csv` (自動マーク。`read_status` は kindle.json の自己申告を優先するため取り込まない)
- `Kindle.KindleDocs/*` (Send to Kindle 個人文書)
- `Kindle.SagaSeriesInfra/*` (シリーズ集計は `book_series` で十分カバーできる)
- `Digital.SeriesContent.Relation.{1,2}/*` (情報量が `CustomerRelationshipIndex_FE` のサブセット)
- `Kindle.ReadingBehaviorCounts/*`, `Kindle.KindleUnlimitedMembership.3/*` (per-book ではない集計値)
- `Devices.*`, `Retail.*`, `registration` (Kindle 蔵書と無関係)

## Implementation

### CLI

- 新規 `kindb import-official <kindle.zip>`:
  - 引数は zip ファイルパス。`Path(input).resolve()` で絶対パス化。
  - zip を一時ディレクトリに展開し、上記 4 ファイルを読み取り後にディレクトリを削除する。
  - `books` テーブルが空でも実行を許可する(zip 由来データだけが入る状態。後から `kindb import` を実行すれば自然に `v_books` で結びつく)。エラーにはしない。
  - **同一 DB 内のトランザクション 1 つで** 既存の `book_genres` / `book_series` / `book_author_ids` / `book_author_names` を全件 `DELETE` してから新規データを `INSERT` し、`import_metadata_official` シングルトン行を `DELETE` + `INSERT` で更新する。失敗時は `ROLLBACK` で旧データが残る。
  - `kindle.json` 側の `books` / `book_authors` / `import_metadata` には**一切触れない**。
- `kindb status` 出力に zip 取り込み情報を追加(zip 未取り込み時は省略可):
  - `Official import` 行: 最終 zip import 日時、source path、`book_genres` / `book_series` / `book_author_ids` / `book_author_names` の行数、zip 由来 ASIN のユニーク数。
- `kindb import` (kindle.json) の**ユーザーから見た挙動**(全件置換、失敗時に旧 DB のデータが残る、成功時に最新スナップショットに置き換わる)は v0.2 と同等を保つ。**実装は同一 DB 内のトランザクション DELETE + INSERT に切り替える**(下記 `### import 仕様(kindle.json)` 参照)。
- `search`, `query`, `authors`, `recent`, `delete` の**ユーザーから見た挙動**は変更しない。読み取り系コマンドは接続前に `ensure_schema(db_path)` を呼び、v0.2 DB を v0.3 スキーマに自動マイグレーションする(下記 `### マイグレーション` 参照)。`delete` は zip 由来テーブルも含めて DB ごと削除する。
- 新規 CLI(`series`, `genres`)は **追加しない**。代表クエリは SKILL.md / README に追記し、`kindb query` 経由で利用する。

### import 仕様(kindle.json)

v0.2 から実装方式のみ変更する。ユーザーから見た挙動(全件置換 / 失敗時 atomicity / 成功時に最新化)は不変。

- v0.2 の「一時 DB に書き込み → `os.replace` で本体置換 → 旧 DB の `.wal` を `unlink`」方式は**廃止**する。
- 代わりに **同一 DB ファイルに書き込みモードで接続し、単一トランザクションで `books` / `book_authors` / `import_metadata` を全件 `DELETE` してから新規データを `INSERT` する**。
  - `import_metadata` シングルトンも `DELETE` してから 1 行 `INSERT`(再 import で 2 行に増えないこと)。
- 実行順序を厳守する: 接続オープン → `create_schema()`(`BEGIN` の**前**)→ `BEGIN` → `DELETE`/`INSERT` 群 → `COMMIT` → `CHECKPOINT`。
- **`create_schema()` は `BEGIN` の前に独立ステートメントとして実行する**(`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW`)。これによりスキーマ migration とデータ rollback が分離され、`INSERT` 失敗で `ROLLBACK` してもスキーマ migration の結果(新テーブル/新 view)は残る。v0.2 で作成された古い DB を v0.3 バイナリで開いた時の自動マイグレーションを兼ねる。
- **`CHECKPOINT` は必ず `COMMIT` 後の独立ステートメントとして実行する**(DuckDB はトランザクション内で `CHECKPOINT` を呼ぶと `TransactionException: Cannot CHECKPOINT: the current transaction has transaction local changes` で失敗するため)。
- `COMMIT` 成功後に呼ぶ `CHECKPOINT` は WAL を DB 本体にフラッシュし、`<db_path>.wal` を消す。これによりファイルサイズの肥大化を抑え、`.wal` 明示削除も不要にする。
- 失敗時は `ROLLBACK` で旧データに戻す。プロセスが途中で落ちた場合は DuckDB の WAL が次回起動時に自動でチェックポイント/ロールバックを行う。
- zip 由来テーブル(`book_genres` / `book_series` / `book_author_ids` / `book_author_names` / `import_metadata_official`)には**一切触れない**。これにより `kindle.json` の再 import 後も zip データが温存される。

### import 仕様(zip)

- zip 展開先は `tempfile.TemporaryDirectory` で一時ディレクトリを作り、終了時に削除する。WAL サイドカーは触らない(DB 本体は同一)。
- CSV パースは Python 標準ライブラリ `csv` を使う。文字コードは UTF-8(BOM 付与あり)。
- 実行順序は kindle.json import と対称的に: 接続オープン → `create_schema()`(`BEGIN` の**前**)→ `BEGIN` → `DELETE`/`INSERT` 群(対象テーブルは `book_genres` / `book_series` / `book_author_ids` / `book_author_names` / `import_metadata_official`)→ `COMMIT` → `CHECKPOINT`。**`create_schema()` は `BEGIN` の前**、**`CHECKPOINT` は `COMMIT` 後の独立ステートメント**(同上理由)。
- バリデーション:
  - 4 ファイル(`CustomerGenres_FE` / `CustomerRelationshipIndex_FE` / `CustomerAuthorIdRelationship_FE` / `CustomerAuthorNameRelationship_FE`)のうち少なくとも 1 つでも zip 内に**ファイルが存在しない**場合はエラー終了(必須ファイル)。サブディレクトリ内の `*.csv` を glob で取得し、複数あれば全部読む。
  - 各 CSV のヘッダが想定列を含まない場合はエラー終了。期待列名と実際の列名をエラーメッセージに含める。
  - `ASIN` が空文字列・`Not Available` の行は警告なしでスキップ(`CustomerOrders` 等で発生)。
  - `Author ID` が空文字列・`Not Available` の行はスキップ。`Author Name` も同様にスキップ。
  - `Genre` が空文字列の行はスキップ。
  - `Deleted By Customer = Yes` の行は **全テーブルから除外** する。`CustomerRelationshipIndex_FE` で `Deleted By Customer = Yes` の ASIN リストを先に抽出し、`book_genres` / `book_series` / `book_author_ids` / `book_author_names` 投入時にもこの ASIN を弾く。
- 重複行の扱い:
  - `book_genres`: `(asin, genre)` の組合せが重複する行は 1 つにまとめる。
  - `book_author_ids`: 同一 `(asin, author_id)` が複数回出現したら最初の出現順を採用する。`author_order` は重複除去後の出現順で 1 始まり再採番。
  - `book_author_names`: 同一 `(asin, author_name)` が複数回出現したら最初の出現順を採用する。`author_order` は重複除去後の出現順で 1 始まり再採番。
  - `book_series`: 同一 ASIN で複数行(`Relation Type` 違い)を許容し、`(asin, series_asin, relation_type)` を PK とする。`Series Title` 末尾から ASIN を抽出できなかった行は **空文字 `''` を sentinel** として `series_asin` に保存する(DuckDB の PK 列は NOT NULL 扱いで NULL を入れられないため)。view 側で `NULLIF(series_asin, '')` を使って NULL に戻して出力する。
- import 成功後、CLI は取り込んだジャンル数・シリーズ数・著者 ID 数・著者名数のサマリを stdout に表示する。

### マイグレーション

v0.2 で作成済みの DB を v0.3 バイナリで開いた場合、新テーブル/新 view が存在しない状態で読み取り CLI が走ると `v_books` の拡張列参照やその他の新 view 参照が落ちる。これを防ぐため、軽量マイグレーション関数を導入する。

- `src/kindb/db.py` の `TABLES_SQL` / `VIEWS_SQL` / `create_schema()` を v0.3 用に拡張する:
  - `TABLES_SQL` に `book_genres` / `book_series` / `book_author_ids` / `book_author_names` / `import_metadata_official` の `CREATE TABLE IF NOT EXISTS` を追加。
  - `VIEWS_SQL` を v0.3 拡張版の `v_books` と、新 view 群(`v_book_genres` / `v_book_series` / `v_series_counts` / `v_genre_counts` / `v_author_id_counts` / `v_book_authors_official`)で書き換える。`CREATE OR REPLACE VIEW` なので既存 view も上書きされる。
- 新規関数 `ensure_schema(db_path: Path)` を `src/kindb/db.py` に追加:
  - DB ファイルが存在しない場合は何もしない(後続コマンドの「DB が無い」エラーメッセージを温存)。
  - 存在する場合は書き込みモードで接続を開き、`create_schema()` を 1 回実行してから接続を閉じる。
- `src/kindb/cli.py` の以下のコマンドが、自分の処理の冒頭(読み取り接続を作る前)で `ensure_schema(db_path)` を呼ぶ:
  - `status`, `search`, `query`, `authors`, `recent`
- `import` (kindle.json) / `import-official` (zip) は、自分の処理の中で書き込み接続を開いた直後・`BEGIN` の**前**に `create_schema()` を呼ぶ(`ensure_schema` 経由ではなく)。スキーマ migration をデータ操作のトランザクション外に出すことで、データ INSERT が `ROLLBACK` してもスキーマ migration は残る。
- `delete` は呼ばない(DB を消す直前にスキーマを作っても意味がない)。
- v0.2 で `ensure_schema` 相当の API が無かったため、本マイグレーションは「初回 v0.3 コマンド実行時に 1 回だけ余計な書き込み接続が走る」副作用がある。既に v0.3 スキーマに揃った DB では `CREATE IF NOT EXISTS` / `CREATE OR REPLACE` がいずれも no-op になるため、定常状態での性能影響は無視できる。

## Schema

### `book_genres` (新規)
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR NOT NULL | `books.asin` への論理参照(FK 制約は付けない) |
| `genre` | VARCHAR NOT NULL | ジャンル文字列(JSON 原値) |

PK は `(asin, genre)`。

### `book_series` (新規)
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR NOT NULL | `books.asin` への論理参照 |
| `series_asin` | VARCHAR NOT NULL | シリーズ ASIN(`Series Title` 末尾から抽出)。**抽出不可は空文字 `''` を sentinel として保存**。view 側で `NULLIF(series_asin, '')` を使って NULL に戻す |
| `series_title` | VARCHAR NOT NULL | シリーズ名(末尾 ASIN を除去した本体) |
| `series_author` | VARCHAR | シリーズ著者名(末尾 ASIN 除去後)。欠落時は NULL |
| `series_author_id` | VARCHAR | シリーズ著者の Amazon ASIN。欠落時は NULL |
| `position_in_collection` | INTEGER | 巻番号。`Not Available` 等は NULL |
| `relation_type` | VARCHAR NOT NULL | 例: `PRIMARY` |

PK は `(asin, series_asin, relation_type)`。DuckDB の PK 列は NOT NULL 扱いのため、`series_asin` は空文字 sentinel で運用する。

### `book_author_ids` (新規)
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR NOT NULL | `books.asin` への論理参照 |
| `author_id` | VARCHAR NOT NULL | Amazon 著者 ASIN |
| `author_order` | INTEGER NOT NULL | CSV 出現順(1 始まり、重複除去後再採番) |

PK は `(asin, author_order)`。

### `book_author_names` (新規)
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR NOT NULL | `books.asin` への論理参照 |
| `author_name` | VARCHAR NOT NULL | 著者名(翻訳者等を含む) |
| `author_order` | INTEGER NOT NULL | CSV 出現順(1 始まり、重複除去後再採番) |

PK は `(asin, author_order)`。`book_author_ids` とは別テーブル(行数が一致せず 1:1 ペアリングできないため)。

### `import_metadata_official` (新規、シングルトン)
| 列 | 型 | 説明 |
|---|---|---|
| `source_path` | VARCHAR | zip ファイルの絶対パス |
| `source_type` | VARCHAR | 固定値 `'kindle_zip'` |
| `genres_count` | INTEGER | `book_genres` の行数 |
| `series_count` | INTEGER | `book_series` の行数 |
| `author_ids_count` | INTEGER | `book_author_ids` の行数 |
| `author_names_count` | INTEGER | `book_author_names` の行数 |
| `distinct_asin_count` | INTEGER | 上記 4 テーブルで出現した ASIN の和集合の件数 |
| `imported_at` | TIMESTAMP | zip import 実行時刻 |

シングルトン(1 行のみ保持)。

### 既存テーブル/ビューへの変更

- `books` / `book_authors` / `import_metadata`: **変更なし**。
- `v_author_counts`: **変更なし**(従来通り `book_authors` ベース。kindle.json のみの環境でも動く著者名ベースの集計)。
- `v_books`: 拡張(下記参照)。

## Views

### `v_books` (拡張)

既存列(`asin`, `title`, `authors`, `authors_text`, `read_status`, `product_image_url`, `acquired_at`)に**以下を追加**する。NULL/空配列の方針は列ごとに固定する(列型に依らず一貫した結果を返す):

| 列 | 型 | 該当行が無い場合 | 説明 |
|---|---|---|---|
| `genres` | `LIST<VARCHAR>` | **空配列 `[]`** | `book_genres` の genre を辞書順で配列化 |
| `series_title` | `VARCHAR` | **`NULL`** | 最初の `relation_type = 'PRIMARY'` 行から取る |
| `series_asin` | `VARCHAR` | **`NULL`** | 同上(`book_series.series_asin` を `NULLIF(series_asin, '')` で sentinel `''` を NULL に戻して出力) |
| `series_position` | `INTEGER` | **`NULL`** | 同上 (`position_in_collection`) |
| `author_ids` | `LIST<VARCHAR>` | **空配列 `[]`** | `book_author_ids` を `author_order` 昇順で配列化 |
| `author_names_official` | `LIST<VARCHAR>` | **空配列 `[]`** | `book_author_names` を `author_order` 昇順で配列化。kindle.json 由来の `authors` LIST とは別物(翻訳者等を含む) |

実装は `books` を起点に zip 由来テーブルを相関サブクエリ(`list(...)`)で取り込む。`books` 起点なので zip にしか無い ASIN は出ない。

**LIST 列の正規化**: DuckDB の `list(...)` 集約は対象行が 0 件のとき NULL を返すため、計画通り「無ければ `[]`」にするには `coalesce` で空配列にキャストする必要がある。DuckDB の `coalesce` は両辺の型推論で迷うことがあるので、**型を明示してキャストする**。DuckDB の LIST 型キャストは `VARCHAR[]` 表記を使う(`LIST<VARCHAR>` は構文エラーになる):

```sql
coalesce(
    (SELECT list(g.genre ORDER BY g.genre)
     FROM book_genres g WHERE g.asin = b.asin),
    CAST([] AS VARCHAR[])
) AS genres
```

`author_ids` / `author_names_official` も同様に `CAST([] AS VARCHAR[])`(または同等の `[]::VARCHAR[]`)で空配列フォールバック。`series_*` 系 scalar 列は相関サブクエリの結果(NULL)をそのまま出力する。

これにより、zip 未取り込みでも従来クエリが壊れないだけでなく、利用側が「LIST 列は配列、scalar 列は NULL」と一貫して扱える。

### `v_book_genres` (新規)

`books` を起点に `INNER JOIN book_genres ON books.asin = book_genres.asin` で構成する読みやすい view。集計用。`books` に存在しない ASIN は出ない(zip ミスマッチ方針との整合)。

| 列 | 説明 |
|---|---|
| `asin` | |
| `title` | `books.title` |
| `genre` | |

### `v_book_series` (新規)

`books` を起点に `INNER JOIN book_series ON books.asin = book_series.asin` で構成し、シリーズ内の蔵書を巻順で並べる view。`series_asin` は `NULLIF(book_series.series_asin, '')` で sentinel `''` を NULL に戻して出力する。

| 列 | 説明 |
|---|---|
| `series_asin` | `NULLIF(book_series.series_asin, '')` の結果 |
| `series_title` | |
| `series_position` | |
| `asin` | |
| `title` | |
| `relation_type` | |

`ORDER BY series_title ASC, series_position ASC NULLS LAST, asin ASC` を view 定義と利用側 SELECT の両方で指定し、決定的に並ぶようにする。

### `v_series_counts` (新規)

シリーズ別の所有冊数。`v_author_counts` と同じく `ORDER BY book_count DESC, series_title ASC` で決定的に並ぶ。`book_series` の `relation_type = 'PRIMARY'` 行を `books` と `INNER JOIN` して集計し、`series_asin` 出力は `NULLIF(book_series.series_asin, '')` で sentinel を NULL に戻す。

| 列 | 説明 |
|---|---|
| `series_asin` | NULL 可(`NULLIF(series_asin, '')` の結果) |
| `series_title` | |
| `book_count` | `INNER JOIN books` で絞った後の `count(DISTINCT books.asin)` |

### `v_genre_counts` (新規)

ジャンル別の所有冊数。`book_genres` を `books` と `INNER JOIN` で絞り、`ORDER BY book_count DESC, genre ASC` で並ぶ。

| 列 | 説明 |
|---|---|
| `genre` | |
| `book_count` | `INNER JOIN books` で絞った後の `count(DISTINCT books.asin)` |

### `v_author_id_counts` (新規)

Amazon 著者 ID ベースの著者別冊数。**同名・別 ID の著者を区別する**ことが目的。`books` に存在する ASIN のみ集計対象とする。

| 列 | 説明 |
|---|---|
| `author_id` | Amazon 著者 ASIN |
| `author_name` | 代表名。`book_author_names` から `author_order` が同じ行を ASIN ごとに引き、出現回数最多の表記を採用する。タイは辞書順最小。対応する名前が無い場合は `'(unknown)'` |
| `book_count` | `book_author_ids` を `books` と `INNER JOIN` してから `count(DISTINCT books.asin)` で集計 |

`ORDER BY book_count DESC, author_name ASC, author_id ASC` で決定的に並ぶ(`author_id` を最終タイブレーカに使うことで同名・別 ID の区別を順序に反映)。

名前のペアリングは「同一 ASIN 内で `author_order` が一致する `book_author_names` の行を引く」というヒューリスティック。`book_author_names` の方が行数が多く(翻訳者・イラストレーター等を含む)、`book_author_ids` のすべての `(asin, author_order)` ペアに対応する名前が `book_author_names` に存在する保証はないが、実データでは主要著者(`author_order = 1, 2`)はほぼ確実に対応する。

### `v_book_authors_official` (新規)

ASIN ごとに ID と名前を `author_order` で揃えて並べた読みやすい view。`v_author_id_counts` の構築や個別本の著者表示に使う。`book_author_ids` と `book_author_names` の `FULL OUTER JOIN`(`asin, author_order`)結果を `books` と `INNER JOIN` で絞り、`books` に存在しない ASIN を弾く。

| 列 | 説明 |
|---|---|
| `asin` | |
| `author_order` | |
| `author_id` | `book_author_ids` に存在しない `author_order` では NULL(=翻訳者等の名前のみの行) |
| `author_name` | `book_author_names` に存在しない `author_order` では NULL(=名前不明の ID 行。実データでは稀) |

## ASIN ミスマッチ・整合性

- `book_genres` / `book_series` / `book_author_ids` / `book_author_names` には FK 制約を付けない。zip にあって `books` に無い ASIN は **テーブルには残る** が、`v_books` / `v_book_genres` / `v_book_series` / `v_series_counts` / `v_genre_counts` / `v_author_id_counts` / `v_book_authors_official` はすべて `books` を起点とする `INNER JOIN`(`v_books` だけは `books` 起点 LEFT JOIN)で絞るため、**view 経由では見えない**。
- 逆に `books` にあって zip テーブルに無い ASIN は、`v_books` の追加列が `NULL` / 空配列になるだけ。
- `kindb import <kindle.json>` を再実行しても zip 由来テーブルは**別テーブル**であり、kindle.json import のトランザクションは `books` / `book_authors` / `import_metadata` だけを対象に `DELETE` + `INSERT` するため、zip データは温存される。
- 同様に `kindb import-official <kindle.zip>` を再実行しても `books` / `book_authors` / `import_metadata` には触れない。

## CLI 出力例

```text
$ kindb status
... (既存出力)
Official import        2026-05-21 10:00:00
Official source        /Users/tenkao/Downloads/Kindle.zip
Genres (rows)          6317
Series (rows)          3256
Author IDs (rows)      3614
Author names (rows)    4145
Official ASIN (uniq)   3235
```

zip 未取り込みなら `Official import` 以降の行は省略。

## SKILL.md / README 改訂方針

### SKILL.md
- 「主要ビュー」に `v_books` の拡張列、`v_book_genres`, `v_book_series`, `v_series_counts`, `v_genre_counts`, `v_author_id_counts`, `v_book_authors_official` を追記。
- 代表クエリに以下を追加:
  - ジャンル別冊数 Top 10
  - シリーズ別冊数 Top 10
  - 特定シリーズの巻順一覧
  - ジャンル + 読了マーク済みのクロス集計
  - Amazon 著者 ID ベースの著者別冊数(`v_author_id_counts`)。同名・別 ID の著者を区別したい時に使う。
  - 特定 `author_id` の本一覧(`v_book_authors_official` 経由)。
- `v_author_counts`(kindle.json ベース)と `v_author_id_counts`(zip ベース)の使い分けを明記:
  - **同名・別 ID の区別が要らない通常用途** → `v_author_counts`。zip 取り込み不要で動く。
  - **同名・別 ID を区別したい / 特定 `author_id` で本を引きたい用途** → `v_author_id_counts` / `v_book_authors_official`。zip 取り込みが前提。
- zip 未取り込み時の `v_books` の挙動を列型ごとに明記:
  - LIST 列(`genres` / `author_ids` / `author_names_official`)は **空配列 `[]`**(NULL ではない)。判定は `len(genres) = 0` または `genres = []` を使う。
  - scalar 列(`series_title` / `series_asin` / `series_position`)は **`NULL`**。判定は `series_title IS NULL` を使う。
  - 「zip を取り込んでいるか」を最速で判定したいなら `kindb status` で `Official import` 行の有無を確認するのが最も確実。
- `genres` と `author_ids` は LIST 列でペイロードが増えるため、軽い一覧では SELECT しないことを推奨(`product_image_url` と同じ運用)。

### README
- 「データの取り込み」セクションに `kindb import-official` を追加。
- 「主要ビュー」に `v_book_genres`, `v_book_series`, `v_series_counts`, `v_genre_counts`, `v_author_id_counts`, `v_book_authors_official` を追加。
- `v_author_counts`(kindle.json ベース、名前)と `v_author_id_counts`(zip ベース、ID + 代表名)の使い分けを明記。
- 「扱わない項目」の文言は維持(発売日・出版社・購入価格・KU 判定等)。価格列は zip にあるが**取り込まない**ことを明示する。
- MCP 経由クエリの代表クエリにシリーズ・ジャンルの例を 1 つずつ足す。

## Test Plan

- fixture:
  - `tests/create_official_fixture.py` を新規追加。最小構成の `Kindle.zip` を動的生成する(`zipfile.ZipFile` で必要な 4 ファイルだけを含む)。
  - 含めるパターン: 1 ASIN 複数ジャンル、シリーズ ASIN 抽出可能/不可、`Position In Collection` が `Not Available`、複数著者 ID + 翻訳者など名前のみ含まれる行、**同名・別 ID の著者**(`author_id` が異なる 2 ASIN に同じ `author_name` を持たせる)、`Deleted By Customer = Yes` の行(除外確認用)、`ASIN = Not Available` の行(スキップ確認用)、kindle.json fixture に存在しない ASIN(view から見えないことの確認用)。
- import (kindle.json) の検証 (v0.3 で実装方式変更):
  - 全件置換のユーザー挙動を v0.2 と同等に保つ(同じ ASIN セットを 2 回 import して結果が一致、初回と 2 回目で `books` / `book_authors` / `import_metadata` の中身が同じ、`import_metadata` は 1 行のまま増えない)。
  - `os.replace` / `shutil.move` が import コードパスから呼ばれないこと(`grep` ベースのコード検査でも可、または `monkeypatch` で `os.replace` を fail させても import が成功することを確認)。
  - import 失敗時(不正 JSON 等)に `ROLLBACK` され、`books` / `book_authors` / `import_metadata` が import 前の状態に戻る(失敗前にすでに行を投入したかどうかに依らず、テーブル内容が import 前と完全一致する)。
  - import 成功後に `read_only=True` 接続で新データが読める。
  - import 関数戻り時(つまり `COMMIT` + `CHECKPOINT` 完了後)に `<db_path>.wal` が残らない。
  - 別プロセスが書き込み接続を持っている間に別の `read_only=True` 接続を試みると DuckDB の `Connection Error` 系エラーが出る(MCP 等が DB を開いている状態で `kindb import` 実行時の挙動。v0.2 と同じ)。kindb のテストではこの挙動を「失敗が確実に起きること」のみ確認し、MVCC スナップショット分離の確認はスコープ外(DuckDB の保証範囲)。
  - 既に zip データが入っている DB に対して `kindb import <kindle.json>` を実行しても `book_genres` / `book_series` / `book_author_ids` / `book_author_names` / `import_metadata_official` の中身が変わらない。
- import-official の検証:
  - 必須 4 ファイルのうち 1 つでも欠けるとエラー終了する(`CustomerAuthorNameRelationship_FE.csv` を含む)。
  - ヘッダ列が想定と異なるとエラー終了する。
  - `Deleted By Customer = Yes` の ASIN は `book_genres` / `book_series` / `book_author_ids` / `book_author_names` のいずれにも投入されない。
  - `ASIN = Not Available` / 空文字行はスキップされる。
  - `Series Title` の末尾 ASIN 抽出: `"foo B07D4FP6XQ"` → `series_title='foo'`, `series_asin='B07D4FP6XQ'`。末尾 ASIN を持たない `"bar"` → `series_title='bar'`, **`book_series.series_asin = ''` (空文字 sentinel)** で保存され、`v_book_series.series_asin = NULL` / `v_series_counts.series_asin = NULL` / `v_books.series_asin = NULL` として読める。
  - `Position In Collection` が `'12'` → 12、`'Not Available'` → NULL。
  - 同一 ASIN の `(genre)` 重複行は 1 件に縮約。
  - 同一 ASIN の `(author_id)` 重複は CSV 内の出現順で `author_order` を再採番。同様に `(author_name)` 重複も再採番。
  - `v_author_id_counts` で同名・別 ID の著者が 2 行に分かれて出る(`author_id` 違いで `book_count` が独立に集計される)。
  - `v_book_authors_official` で `author_id` のみ / `author_name` のみの行が、もう一方の列が NULL として現れる。
  - import 失敗時はトランザクションがロールバックされ、既存の `book_genres` 等が残る。
  - import 成功時は `import_metadata_official` シングルトンが 1 行で更新される(再 import で 2 行に増えない)。
  - kindle.json import 後に zip import を実行しても `books` / `book_authors` / `import_metadata` の中身が変わらない。
  - 逆に zip import 後に kindle.json import を実行しても `book_genres` / `book_series` / `book_author_ids` / `book_author_names` / `import_metadata_official` の中身が変わらない。
- view の検証:
  - `v_books` に `genres`, `series_title`, `series_asin`, `series_position`, `author_ids`, `author_names_official` の 6 列が追加されている。
  - zip 未取り込み環境で `v_books` を読むと、**LIST 列(`genres` / `author_ids` / `author_names_official`)はすべて空配列 `[]` で返る**(NULL ではない。`coalesce(..., CAST([] AS VARCHAR[]))` の効果を確認)。**scalar 列(`series_title` / `series_asin` / `series_position`)は `NULL` で返る**。
  - zip 取り込み後の `v_books` で、kindle.json と zip に共通する ASIN は LIST 列に実データが入り、scalar 列も値が入る(`series_asin` は `NULLIF` で sentinel `''` が NULL に戻っている)。
  - zip にジャンルだけある ASIN(シリーズ無し)では `genres` が非空、`series_title` 系は NULL のまま、と列ごとの独立性を確認。
  - `v_book_genres`, `v_book_series`, `v_book_authors_official` は `books` に存在しない ASIN を返さない(zip にしか無い ASIN が `INNER JOIN` で view から消える)。
  - `v_series_counts` / `v_genre_counts` / `v_author_id_counts` も `books` 存在 ASIN のみで集計され、決定的な順序で並ぶ(`book_count DESC, タイブレーカ ASC`)。
  - `v_author_id_counts` の `author_name` は、対応する `book_author_names` 行が無い `author_id` で `'(unknown)'` になる。
- マイグレーション (`ensure_schema`) の検証:
  - v0.2 スキーマで作成した DB(新テーブル/新 view 無し)を用意し、v0.3 の `status` / `search` / `query` / `authors` / `recent` を実行すると、エラーにならず新 view が `CREATE OR REPLACE VIEW` で作成される。
  - DB ファイル不在時に `status` 等を実行しても `ensure_schema` がクラッシュせず、後続コマンドが v0.2 と同じエラーメッセージを出す。
  - `kindb import` / `kindb import-official` は自分の処理の中で書き込み接続を開いた直後・`BEGIN` の**前**に `create_schema()` を呼ぶため、`ensure_schema` を経由せずに動く(スキーマ migration はトランザクション外で完了)。
- CLI の検証:
  - `kindb status` が zip 取り込み済み環境で `Official import` 行(著者名 row 数を含む)を出す。未取り込みでは出さない。
  - `kindb delete` 実行後、`book_genres` 等を含む DB 本体ファイルと `<db_path>.wal` の両方が消える。
  - 既存 CLI(`import`, `search`, `query`, `authors`, `recent`, `delete`)のテストが破壊されない。
- 静的検査: `ruff check . && pytest` を通す。

## ドキュメント更新

- `docs/kindb-v0.2-plan.md` は**残す**(v0.2 仕様の参照ドキュメントとして引き続き有効)。`CLAUDE.md` の `@docs/kindb-v0.2-plan.md` 参照に**加えて** `@docs/kindb-v0.3-plan.md` を追加する。
- `README.md`: `kindb import-official` セクション、`v_book_genres` / `v_book_series` / `v_series_counts` / `v_genre_counts` / `v_author_id_counts` / `v_book_authors_official` を追記。MCP 設定セクションは据え置き。
- `SKILL.md`: 上記 SKILL 改訂方針に沿って書き換え。
- `pyproject.toml`: `version` を `0.3.0` に更新。`description` は zip オプション取り込みを明示。依存パッケージは据え置き(zip 展開は標準ライブラリ `zipfile` を使う)。

## Assumptions

- 公式 zip 内の上記 4 ファイル(`CustomerGenres_FE`, `CustomerRelationshipIndex_FE`, `CustomerAuthorIdRelationship_FE`, `CustomerAuthorNameRelationship_FE`)は将来も `Kindle.UnifiedLibraryIndex/datasets/<Name>/<Name>.csv` 配下に固定列で出力される。Amazon 側のフォーマット変更時は本計画を改訂する。1 つでも欠けたら `kindb import-official` はエラー終了する。
- `Series Title` / `Series Author` の末尾 ASIN フォーマット(` B[0-9A-Z]{9}` の suffix)は実データから観測した規則であり、Amazon が将来 ASIN 連結方式を変えた場合は parse ロジックを改訂する。
- zip にあって kindle.json に無い ASIN(`KindlePDoc`・削除済み等)を view から見えなくするのは、`kindle.json` をソースオブトゥルースとする v0.3 の方針による。raw データは `book_*` テーブルに残るため、必要なら `kindb query` で直接参照できる。
- v0.3 では価格・marketplace・ownership_type・order_id・読書セッション・autoMarkAsRead は扱わない。これらは v0.4 以降の検討対象。
- 公式 zip は Amazon の Privacy Hub 経由で取得し、ダウンロードに数時間〜数日かかる。リアルタイム性は `kindle.json` の方が高いため、`acquired_at` は kindle.json 側を優先し、zip 側の `acquiredDate` / `Created At` は取り込まない。
- DuckDB の同時アクセスについては DuckDB 側の保証に従う: **同一プロセスの別接続は MVCC スナップショット分離で commit 前後に異なる結果を見る**(import 中に別接続から読むと commit 前は旧データ、commit 後は新データが見える)。**別プロセスは DB ファイル単位の排他で、書き込み接続が存在する間は `read_only=True` でも接続自体が失敗する**。kindb はこれらの DuckDB の挙動を前提とし、自身ではトランザクション以上の同期制御を行わない。
