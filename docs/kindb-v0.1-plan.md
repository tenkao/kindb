# kindb v0.1 実装計画

## Summary

- `kindb` は公式 `Kindle.zip` を DuckDB に取り込み、CLI / Claude Desktop / Claude Code から Kindle 蔵書と読書セッションを検索・集計できるローカルツールとして作る。
- v0.1 は **意味が明確な公式データだけ**を対象にする。推定フィールド、不明瞭なAmazon内部メタデータ、価格、発売日、出版社、raw行は保存しない。
- import は差分更新ではなく **全件再インポート**。一時DBに作成して成功後に置換し、失敗時は既存DBを残す。
- 保存は正規化テーブル、利用は `v_*` ビュー中心にする。生成AIには基本的に `v_books` / `v_books_with_reading` を使わせる。

## Implementation

- Python パッケージとして新規実装する。依存は `duckdb`, `typer`, `rich`、開発依存は `pytest`, `ruff`。日付パースは Python 標準ライブラリまたは DuckDB の関数で処理し、追加依存は増やさない。
- デフォルトDBパスは `~/.kindb/kindle.duckdb`。CLI は `--db PATH` で上書き可能にする。
- CLI:
  - `kindb import <Kindle.zip>`: 一時DBに全件取り込み、成功後に既存DBを置換する。
  - `kindb status`: 取り込み件数、最終取り込み日時、蔵書数、著者数、ジャンル数、読書セッション数を表示する。
  - `kindb search <term>`: `v_books_with_reading` を対象に、書名・著者・ジャンル・シリーズ・ASINを検索する。
  - `kindb query "<SQL>" [--table]`: 読み取り専用接続で `SELECT` / `WITH` / `SHOW` / `DESCRIBE` 系のSQLだけを実行し、JSON または表形式で出力する。
  - `kindb authors`: 著者別の所有冊数を多い順に表示する。
  - `kindb genres`: ジャンル別の所有冊数を多い順に表示する。
  - `kindb series`: シリーズ名ごとの所有冊数と所有巻位置を表示する。
  - `kindb recent`: `relationship_creation_date` が新しい順に直近の本を表示する。
  - `kindb reading`: `reading_sessions` 由来のASIN別読書セッション数、最終読書日時、合計読書時間、合計ページめくり数を表示する。
  - `kindb delete`: 確認つきでDBを削除する。
- 読み込む公式zip内ファイル:
  - 蔵書: `Kindle.UnifiedLibraryIndex/...CustomerRelationshipIndex_FE.csv`
  - 著者: `Kindle.UnifiedLibraryIndex/...CustomerAuthorNameRelationship_FE.csv`
  - ジャンル: `Kindle.UnifiedLibraryIndex/...CustomerGenres_FE.csv`
  - 画像URL: `Kindle.UnifiedLibraryIndex/...CustomerTags_FE.csv` の `Image URL` だけ
  - 読書セッション: `Kindle.Devices.ReadingSession/Kindle.Devices.ReadingSession.csv`
  - Reading Insights セッション: `Kindle.ReadingInsights/...sessions_with_adjustments.csv`
  - 個人文書: `Kindle.KindleDocs/...DocumentMetadata.csv`
- 蔵書本体の取り込みでは、`CustomerRelationshipIndex_FE.csv` にサンプル、レコメンド/興味なし、著者、リストなどが混在するため、`Resource Type = ITEM`, `Ownership Type = Item Owner`, `Deleted By Customer != Yes` の行だけを `books` に入れる。
- `resource_type`, `ownership_type`, `deleted_by_customer` は import 時のフィルタに使うが、DBには保存しない。
- `Kindle.Devices.ReadingSession.csv` には書名列がないため、`reading_sessions` には `product_name` を保存しない。書名が必要な場合はビューで `books` と結合する。
- `Kindle.ReadingInsights/...sessions_with_adjustments.csv` には `product_name` が存在するため、`reading_insight_sessions` には公式項目として保存する。ただし `reading_sessions` と重複する可能性があるため、v0.1 の読書集計には使わず補助/参照用テーブルとして扱う。
- `Kindle.ReadingInsights/...ReadingInsightsDayUnits.csv` は日付1列だけで用途が薄く、読書日はセッションから集計できるため v0.1 では取り込まない。
- `personal_documents` は `HasBeenDeleted != Yes` の行だけを取り込み、削除済み個人文書と `has_been_deleted` 列は保存しない。
- 保存しない項目:
  - `our_price`, `resource_type`, `ownership_type`, `ownership_subtype`, `relationship_status`, `relation_type`, `ordered`
  - `tag_name`, `tag_scope`, `tag_source_group`, `tag_source_subgroup`
  - `device_family`, `reading_marketplace`, 注文ID、注文タイプ、raw行
  - `deleted_by_customer`, `has_been_deleted`
  - 発売日、出版社、購入価格、Kindle Unlimited判定、未読/既読、読了/未読了、マンガ判定、固定レイアウト判定

## Schema

- `books`
  - `asin`, `product_name`, `sortable_title`, `sortable_author_name`, `series_title`, `series_author`, `position_in_collection`, `marketplace`, `relationship_creation_date`, `source_file`, `imported_at`
  - `relationship_creation_date` は購入日とは呼ばず、ライブラリ追加日/取得日相当として扱う。
- `book_authors`
  - `asin`, `author_name`, `source_file`
- `book_genres`
  - `asin`, `genre`, `source_file`
- `book_images`
  - `asin`, `image_url`, `source_file`
  - `CustomerTags_FE.csv` から画像URLだけ抽出し、タグ本体は保存しない。
- `reading_sessions`
  - `asin`, `start_timestamp`, `end_timestamp`, `content_type`, `total_reading_millis`, `number_of_page_flips`, `source_file`
- `reading_insight_sessions`
  - `asin`, `product_name`, `start_time`, `end_time`, `total_reading_milliseconds`, `source_file`
- `personal_documents`
  - `document_id`, `title`, `document_provider`, `filename`, `document_original_type`, `document_size_in_bytes`, `entry_creation_date`, `source_file`
- `import_metadata`
  - `import_id`, `source_path`, `source_type`, `imported_at`, `books_count`, `reading_sessions_count`
  - 全件再インポート方式のため、履歴テーブルではなくDB内に1行だけ持つシングルトンメタデータとして最新インポート情報を記録する。

## Views

- `v_books`: 1冊1行の基本ビュー。`authors` と `genres` は DuckDB の `list(distinct ...)` で配列化し、`image_url` はASINごとに決定的に `min(image_url)` を選ぶ。
- `v_reading_summary`: `reading_sessions` だけを集計元にして、ASINごとの `reading_session_count`, `first_read_at`, `last_read_at`, `total_reading_millis`, `total_page_flips` を集計する。`reading_insight_sessions` は二重計上を避けるためこのビューには含めない。
- `v_books_with_reading`: `v_books` と `v_reading_summary` を結合した、CLI / Claude向けの主ビュー。
- `SKILL.md` には「通常は `v_books` / `v_books_with_reading` を使う」「読了・未読・マンガ・KU・発売日・価格は断定しない」と明記する。

## Test Plan

- 匿名化した `Kindle.zip` fixture で import を検証する。
- import は一時DBに作成され、成功時だけ既存DBを置換し、失敗時は既存DBが残ることを確認する。
- 各CSVが正しいテーブルに入り、件数・主要列・日付パースが期待通りであることを確認する。
- `books` には `Resource Type = ITEM`, `Ownership Type = Item Owner`, `Deleted By Customer != Yes` の行だけが入り、サンプル、レコメンド/興味なし、著者、リストは混入しないことを確認する。
- `personal_documents` には `HasBeenDeleted != Yes` の行だけが入り、削除済み個人文書は混入しないことを確認する。
- `book_authors`, `book_genres`, `book_images` は `product_name` を持たず、書名は `books` との結合で取得することを確認する。
- 著者・ジャンル・画像・読書セッションが ASIN で結合され、`v_books` と `v_books_with_reading` が1冊1行になることを確認する。
- `v_reading_summary`, `v_books_with_reading`, `kindb reading` は `reading_sessions` だけを集計元にし、`reading_insight_sessions` を二重計上しないことを確認する。
- `kindb search` は `ILIKE '%term%'` ベースで、`product_name`, `authors`, `genres`, `series_title`, `asin` を対象に検索することを確認する。
- `kindb authors`, `genres`, `series`, `recent`, `reading` が定義済みの並び順と列で出力されることを確認する。
- `kindb query` は読み取り専用で実行され、書き込み系SQLを拒否することを確認する。
- 一部ファイル欠落時は、該当テーブルを空にして取り込みを継続する。ただし蔵書本体ファイル欠落時は明示エラーにする。
- `status`, `search`, `query`, `authors`, `genres`, `series`, `recent`, `reading` の CLI 出力をテストする。
- 不正zip、不正CSV、空データ、ASIN欠落、日付パース不能をテストする。
- 標準検証コマンドは `ruff check` と `pytest`。
- fixture は実データを丸ごと匿名化するのではなく、必要CSVだけを含む最小 `Kindle.zip` を手作りする。日本語の書名/著者、複数著者、複数ジャンル、ASIN欠落行、フィルタ対象外行、削除済み行、画像重複、読書セッション、個人文書を含める。

## Assumptions

- v0.1 の正式入力は公式 `Kindle.zip` のみ。
- 非公式 `kindle.json` は v0.1 の対象外。
- 実装前のリポジトリは空の初期状態で、既存コードとの互換性は考慮不要。
- 価格、発売日、出版社、読了状態、未読状態、Kindle Unlimited、マンガ、固定レイアウトは、公式zipから確定できないため v0.1 では扱わない。
- v0.1 は履歴保持や差分インポートを扱わず、最新の公式zipスナップショットをDB全体として再構築する。
