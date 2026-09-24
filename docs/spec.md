# kindb 仕様

kindb の現行仕様と、その設計判断の理由をまとめる。DDL とビュー定義の実体は `src/kindb/db.py` の `TABLES_SQL` / `VIEWS_SQL` にあり、本書はその意味と、コードからは読み取れない前提を記す。

## データソース

| ソース | 位置づけ | 取り込みコマンド |
|---|---|---|
| `kindle.json` | 主データ。蔵書の有無、書名、著者、取得日時、読了マーク、表紙 URL の正とする | `kindb import` |
| `Kindle.zip` | 任意の補完データ。ジャンル、シリーズ、Amazon 著者 ID、公式著者名 | `kindb import-official` |

`kindle.json` は Chrome 拡張「Kindle bookshelf exporter」の出力、`Kindle.zip` は Amazon のアカウントサービスからダウンロードする公式アーカイブ。公式 zip は取得に数時間から数日かかるため、鮮度の高い `kindle.json` を正とし、zip 側の取得日時や読了情報は取り込まない。

### kindle.json

ルートは配列で、各要素が 1 冊に対応するオブジェクト。実データ(2,484 件)で観測したキーは次の 6 種類。

| キー | 型 | 必須 | 保存先 |
|---|---|---|---|
| `title` | string | ○ | `books.title` |
| `authors` | string | ○ | `books.authors_text`、分割して `book_authors` |
| `acquiredTime` | int(epoch ms) | ○ | `books.acquired_at` |
| `readStatus` | string | ○ | `books.read_status` |
| `asin` | string | ○ | `books.asin` |
| `productImage` | string / null | | `books.product_image_url` |

検証規則(違反はすべて集めてから 1 つのエラーとして報告し、DB には触れない):

- ルートが配列でない、要素がオブジェクトでない場合はエラー。
- 必須キーが欠落、null、空白のみの文字列ならエラー。エラーには `ASIN <asin>` か、ASIN が取れなければ `index <n>` を付ける。
- `title` / `authors` / `readStatus` / `asin` は string のみ。`productImage` は string か null。
- `acquiredTime` は int のみで、bool は拒否する(Python では bool が int のサブクラスのため明示的に弾く)。範囲は `0 <= acquiredTime < 4102444800000`(2100-01-01 UTC)。
- ASIN が重複したら、重複した ASIN を列挙してエラー。
- 未知のキーは stderr に警告して無視する。拡張側のフィールド追加で import が止まらないようにするため。

変換規則:

- `acquiredTime` は Python 側で UTC に固定変換し、タイムゾーンなしの `TIMESTAMP` として保存する。DuckDB の `to_timestamp` はセッションのタイムゾーンに依存するため使わない。
- `authors` は `", "` で分割し、前後の空白を除いて空要素を捨て、出現順に `author_order` を 1 から振る。実データの最大は 8 著者で、`Last, First` 形式の単独著者は存在しないため、単純分割で安全に扱える。
- `productImage` の空文字列は NULL にする。
- `readStatus` は原値のまま保存し、値域は検査しない。実データの値は `READ` と `UNKNOWN` のみ。

### Kindle.zip

`Kindle.UnifiedLibraryIndex/datasets/<データセット名>/*.csv` の 4 データセットだけを読む。各ディレクトリの CSV はすべて(ファイル名順に)読み、1 つでもデータセットが欠けていればエラーにする。文字コードは BOM 付き UTF-8。

| データセット名(`Kindle.UnifiedLibraryIndex.` 以下) | 必須列 | 保存先 |
|---|---|---|
| `CustomerGenres_FE` | `ASIN`, `Genre` | `book_genres` |
| `CustomerRelationshipIndex_FE` | `ASIN`, `Series Title`, `Series Author`, `Position In Collection`, `Relation Type`, `Deleted By Customer` | `book_series` |
| `CustomerAuthorIdRelationship_FE` | `ASIN`, `Author ID` | `book_author_ids` |
| `CustomerAuthorNameRelationship_FE` | `ASIN`, `Author Name` | `book_author_names` |

必須列が欠けたヘッダはエラーにし、不足列と実際の列名をメッセージに含める。CSV は zip 内のパスで示す。展開先の一時ディレクトリはエラーを表示する時点で消えているため。

変換規則:

- 値が空、または `Not Available` のセルは「値なし」とみなす。ASIN が値なしの行は捨てる。
- `CustomerRelationshipIndex_FE` で `Deleted By Customer = Yes` の ASIN は、4 テーブルすべてから除外する。`kindle.json` にも削除済みの本は出ないため、両ソースを「現役の蔵書」で揃える。
- `Series Title` / `Series Author` は末尾の ` B[0-9A-Z]{9}` を ASIN として分離する。分離できなければ全体を名前とする。`Series Title` が値なしの行と `Relation Type` が値なしの行は捨てる。
- `Position In Collection` は整数に変換できれば INTEGER、できなければ NULL。
- `(asin, genre)` の重複は 1 件にまとめる。
- 著者 ID と著者名は、同じ ASIN 内で同じ値が重複したら最初の出現だけを残し、残った順に `author_order` を 1 から振り直す。
- `book_series` は `(asin, series_asin, relation_type)` の最初の行を採る。

著者名は翻訳者やイラストレーターを含むため、著者 ID より行数が多い(実データで 4,145 行と 3,614 行)。このため 2 つは別テーブルにし、`author_order` による対応づけはヒューリスティックとして扱う。

価格、marketplace、注文、読書セッション、自動読了マーク、個人文書など、上記 4 データセット以外は取り込まない。

## import の動作

2 つの import は同じ手順で、対象テーブルだけが異なる。

1. 入力を読み、検証する。失敗したらここで終わり、DB には接続しない。
2. 書き込み接続を開き、`create_schema()` を実行する。
3. `BEGIN` し、対象テーブルを全件 `DELETE` してから `INSERT` し、メタデータのシングルトン行を入れ直す。
4. `COMMIT` する。途中で失敗したら `ROLLBACK` し、元のデータが残る。
5. `CHECKPOINT` を実行する。

| コマンド | 書き換えるテーブル | 触れないテーブル |
|---|---|---|
| `kindb import` | `books`, `book_authors`, `import_metadata` | zip 由来の 5 テーブル |
| `kindb import-official` | `book_genres`, `book_series`, `book_author_ids`, `book_author_names`, `import_metadata_official` | `kindle.json` 由来の 3 テーブル |

手順の理由:

- 差分更新はせず、毎回スナップショット全体で置き換える。入力が非公式な JSON で、削除や更新を追う手がかりがないため。
- `create_schema()` をトランザクションの外に置くのは、データの `ROLLBACK` でスキーマ移行まで巻き戻らないようにするため。
- `CHECKPOINT` を `COMMIT` の後に置くのは、DuckDB がトランザクション内の `CHECKPOINT` を拒否するため。`CHECKPOINT` で WAL が DB 本体に書き出され、`<db>.wal` が残らない。
- 2 つの import が互いのテーブルに触れないので、どちらを何度再実行しても、もう一方のデータは残る。`books` が空でも `import-official` は実行できる。

同時アクセスは DuckDB のファイルロックに従う。別プロセスが書き込み接続を持っている間は、読み取り専用の接続も開けない。`mcp-server-motherduck` は既定で読み取り専用かつ一時接続(`--ephemeral-connections`)で動くため、問い合わせの合間は DB ファイルを開いておらず、MCP サーバの起動中でも import できる(v1.0.8 と Claude Desktop で確認)。問い合わせの実行中に import が重なった場合は、どちらかがロックの衝突で失敗しうる。kindb 側で衝突したときは、別のプロセスが使用中である旨を 1 行で stderr に出し、終了コード 1 で終わる。衝突は DuckDB のエラーメッセージ `Could not set lock` で見分けるが、この文言は macOS で確認した(DuckDB のソースでは Linux も同じ実装)。Windows では文言が異なる(ソースによる。実機では未確認)ため、トレースバックのままになる。ほかの IO エラーは原因を調べられるよう、元の例外のまま出す。

## スキーマ

| テーブル | 由来 | キー | 内容 |
|---|---|---|---|
| `books` | json | `asin` | 1 冊 1 行 |
| `book_authors` | json | `(asin, author_order)` | 分割した著者名 |
| `import_metadata` | json | シングルトン | source の絶対パス、`source_type = 'kindle_json'`、冊数、取り込み時刻 |
| `book_genres` | zip | `(asin, genre)` | ジャンル |
| `book_series` | zip | `(asin, series_asin, relation_type)` | シリーズ名、シリーズ ASIN、シリーズ著者、巻番号 |
| `book_author_ids` | zip | `(asin, author_order)` | Amazon 著者 ID |
| `book_author_names` | zip | `(asin, author_order)` | 公式著者名 |
| `import_metadata_official` | zip | シングルトン | source の絶対パス、`source_type = 'kindle_zip'`、各テーブルの行数、ASIN の和集合の件数、取り込み時刻 |
| `schema_meta` | kindb | シングルトン | 最後に適用したスキーマのハッシュ(下記「スキーマ移行」) |

- zip 由来テーブルには外部キーを付けない。zip にしかない ASIN(個人文書など)も生データとして残し、ビューで除外する。
- `book_series.series_asin` を抽出できなかった行は、空文字列 `''` を入れる。DuckDB の主キー列には NULL を入れられないため。ビューは `NULLIF(series_asin, '')` で NULL に戻して返す。
- 時刻はすべてタイムゾーンなしの UTC。

## ビュー

問い合わせは原則としてビューを使う。zip 由来のビューは `books` との INNER JOIN で絞るため、`books` にない ASIN は現れない。

### `v_books`

1 冊 1 行の主ビュー。`books` を起点に、zip 由来の値を相関サブクエリで付ける。

| 列 | 型 | 値がないとき | 内容 |
|---|---|---|---|
| `asin`, `title`, `authors_text`, `read_status`, `product_image_url`, `acquired_at` | | | `books` の値 |
| `authors` | `VARCHAR[]` | NULL(`authors` が `", "` のように分割後に空になる場合) | `author_order` 順の著者名 |
| `genres` | `VARCHAR[]` | `[]` | 辞書順のジャンル |
| `series_title` / `series_asin` / `series_position` | scalar | NULL | `relation_type = 'PRIMARY'` の行を `series_title`、巻番号(NULL は後)、`series_asin` の順に並べた先頭 1 行から取る |
| `author_ids` | `VARCHAR[]` | `[]` | `author_order` 順の著者 ID |
| `author_names_official` | `VARCHAR[]` | `[]` | `author_order` 順の公式著者名(翻訳者などを含む) |

zip 由来の LIST 列は、空配列を明示的に `CAST([] AS VARCHAR[])` で返す。「LIST 列は常に配列、scalar 列は NULL」と利用側が一貫して扱えるようにするため。

### 集計と展開のビュー

| ビュー | 列 | 定義上の ORDER BY | 内容 |
|---|---|---|---|
| `v_author_counts` | `author_name`, `book_count` | `book_count DESC, author_name ASC` | `kindle.json` の著者名ごとの冊数。zip なしで使える |
| `v_book_genres` | `asin`, `title`, `genre` | `genre, title, asin` | 本とジャンルを 1:N に展開 |
| `v_book_series` | `series_asin`, `series_title`, `series_position`, `asin`, `title`, `relation_type` | `series_title, series_position NULLS LAST, asin` | シリーズ内の本。全 relation_type を含む |
| `v_series_counts` | `series_asin`, `series_title`, `book_count` | `book_count DESC, series_title ASC` | PRIMARY 行だけを数えたシリーズ別冊数 |
| `v_genre_counts` | `genre`, `book_count` | `book_count DESC, genre ASC` | ジャンル別冊数 |
| `v_book_authors_official` | `asin`, `author_order`, `author_id`, `author_name` | `asin, author_order` | 著者 ID と公式著者名を `(asin, author_order)` で FULL OUTER JOIN。片方しかない順位はもう片方が NULL |
| `v_author_id_counts` | `author_id`, `author_name`, `book_count` | `book_count DESC, author_name ASC, author_id ASC` | 著者 ID ごとの冊数。同名で別 ID の著者を区別する |

`v_author_id_counts.author_name` は、著者 ID ごとに、各本で同じ `author_order` に並ぶ公式著者名を集め、最も多く現れた名前を採る。同数なら辞書順で最小の名前を採る。名前が対応しない本は多数決に入れず、候補が 1 つもない著者 ID だけ `'(unknown)'` にする。

ビュー定義の `ORDER BY` は結果の順序を保証せず、一意になるとも限らない。順序が必要な問い合わせでは、呼び出し側で一意な列まで含めた `ORDER BY` を書く(`SKILL.md` の「その他のビュー」の表を参照)。

## スキーマ移行

`create_schema()` は `CREATE TABLE IF NOT EXISTS` と `CREATE OR REPLACE VIEW` だけで構成され、何度実行しても結果は同じ。最後に `TABLES_SQL` と `VIEWS_SQL` の SHA-256(`SCHEMA_HASH`)を `schema_meta` に記録する。

- 読み取り系コマンド(`status` / `search` / `query` / `authors` / `recent`)は、`ensure_schema()` を呼んでから読み取り専用で接続する。`ensure_schema()` は、まず読み取り専用で `schema_meta` のハッシュを照合し、一致しないとき(テーブルがない旧版の DB を含む)だけ書き込み接続で `create_schema()` を実行する。DB ファイルがなければ何もせず、「No database found.」のエラーを出す。
- 書き込み接続を必要なときだけ開くのは、DuckDB では書き込み接続が別プロセスの接続(読み取り専用を含む)と共存できないため。毎回開くと、読み取り系コマンドの並列実行や、MCP サーバの問い合わせとぶつかる。ただし移行が必要な最初の 1 回(kindb の更新後やスキーマ変更後)は書き込み接続を開くので、その瞬間に別の接続があれば衝突しうる。
- 版をハッシュで表すので、`TABLES_SQL` / `VIEWS_SQL` を変更すれば、次の読み取り系コマンドで自動的に移行される。コメントだけの変更でも 1 回移行が走るが、結果は同じなので害はない。
- import 系は手順 2 で `create_schema()` を直接呼ぶ。
- 既存テーブルへの列追加や型変更はこの仕組みでは反映されない。必要になったら明示的な移行処理を追加する。

## CLI

オプションの一覧は `kindb <command> --help` を参照する。

DB パスは、`--db` → 環境変数 `KINDB_DB_PATH` → `~/.kindb/kindle.duckdb` の順で決まる。

| コマンド | 仕様 |
|---|---|
| `status` | 最終取り込み日時、source、冊数、著者数、`read_status` ごとの冊数(値を固定せず GROUP BY で列挙)、表紙 URL がある冊数。zip 取り込み済みなら公式 import の情報も表示する |
| `search <term>` | `v_books` の `title` / `authors_text` / `asin` / `read_status` を ILIKE で検索する。`%` `_` `\` はエスケープする。並びは `title, asin`。`-n` 件(既定 50、`0` で全件)まで表示し、最後に表示件数と総件数を出す。表紙 URL は出さない |
| `query <sql>` | 下記「query の制約」を参照 |
| `authors` | `v_author_counts` を `-n` 件(既定 50、`0` で全件)まで表示し、最後に表示件数と総件数を出す |
| `recent` | `acquired_at DESC, asin DESC` で `-n` 件(既定 20 件)。表紙 URL は出さない |
| `delete` | DB ファイルと `<db>.wal` を削除する。`--yes` で確認を省く |

表を出すコマンドは、列に収まらない値を「…」で切らず、文字単位で折り返す。パイプに出すときは 80 桁で組まれ、空白のない日本語の書名が丸ごと 1 語として切られるため。

書名やパスなどのデータは、表でもエラーでも rich のマークアップや絵文字の記法として解釈しない。`Clean Code [Paperback]` の `[Paperback]` が黙って消えたり、`[/i]` を含む書名でコマンドが落ちたり、`:smile:` が絵文字に置き換わったりするため。エラーや `Database: <path>` のようにパスを含む行は、端末幅で折り返さない。

### query の制約

`kindb query` は AI からの問い合わせ経路として、読み取り専用と出力量の制御を担う。

- 先頭が `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` の単一文だけを受け付け、読み取り専用接続で実行する。文字列リテラルとコメントを除いたうえで、末尾以外に `;` があれば複文とみなして拒否する。
- `SELECT` / `WITH` は、トップレベルの末尾が `LIMIT n [OFFSET m]` でなければ拒否する。サブクエリや CTE の中の LIMIT、`FETCH FIRST` は数えない。
- 例外として、GROUP BY と集合演算を含まず、トップレベルの SELECT 句が `count` / `sum` / `avg` / `min` / `max` だけの集計クエリは LIMIT なしで通す。
- `--allow-unlimited` を付けると LIMIT の検査を省く。
- 出力は既定で JSON を標準出力にそのまま書く。rich を通すと端末幅で改行が入り JSON が壊れるため。`--table` / `-t` は人が読むための rich の表。

LIMIT を必須にした理由は、AI が一覧を取るときに、件数を数えてからページングする流れへ誘導するため。MCP サーバ(返却上限の既定は 1,024 行 / 50,000 文字)も Claude Code の Bash ツール(既定 30,000 文字)も、長い出力を切り詰める。判定は構文解析をしない文字列ベースの近似で、依存を増やさないことを優先した。誤って拒否されたときは `LIMIT` を足すか `--allow-unlimited` を使う。MCP 経由の問い合わせはこの CLI を通らないため、同じ規則は `SKILL.md` で指示する。

## 扱わない項目

次の項目は保存せず、問い合わせにも答えない。

- 発売日、出版社、購入価格(zip に価格列はあるが取り込まない)
- Kindle Unlimited かどうか、購入経路
- マンガかどうか、固定レイアウトかどうか
- 読書セッション、取り込み履歴、差分

`read_status = 'READ'` は利用者が Kindle で付けた読了マークで、`UNKNOWN` は読了マークがないことだけを意味する。`acquired_at` はライブラリに入った日時で、再ダウンロードなどで変わりうるため購入日とは限らない。
