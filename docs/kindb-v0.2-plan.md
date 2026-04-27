# kindb v0.2 実装計画 (kindle.json 専用蔵書ビューア)

## Summary

- v0.1 の公式 `Kindle.zip` 取り込み機能は全廃し、Chrome 拡張で取得した非公式 `kindle.json` を唯一の入力とするローカル蔵書 DB に転換する。
- 利用形態は CLI / DuckDB / MCP / Claude Code Skill から `~/.kindb/kindle.duckdb` を参照する形のみ。Web UI は対象外(現状未実装)。
- import は v0.1 と同じく**全件再インポート**方式。一時 DB に作成して成功後に置換、失敗時は既存 DB を残す。
- 表紙画像 URL を `kindle.json.productImage` から取り込み、MCP からの蔵書ビューアで利用できるようにする。

## 入力フォーマット (kindle.json)

実データ(2,484 件)で精査済みの仕様。

- ルートは配列。各要素は1冊に対応するオブジェクト。
- 出現キーは以下の 6 種類のみ。
  - `title` (string, 必須, 空文字なし)
  - `authors` (string, 必須, 空文字なし)
  - `acquiredTime` (number, 必須, epoch milliseconds)
  - `readStatus` (string, 必須)
  - `asin` (string, 必須, 一意)
  - `productImage` (string, 任意。欠落しうる唯一のキー)
- `readStatus` の実出現値は `READ` / `UNKNOWN` の 2 値のみ。`UNREAD` / `READING` 等は出現しない。`UNKNOWN` は「読了マークなし」を意味し「未読」と断定しない。
- `authors` は単一文字列で、複数著者は `, ` 区切り。最大 8 著者まで観測。Western `Last, First` 形式の単一著者は実データに存在しないため、`, ` 単純分割で安全に扱える。
- ASIN は同一 JSON 内で一意。重複は import エラーとする。
- サンプル:

```json
{
  "title": "図解 武器と甲冑",
  "authors": "樋口 隆晴, 渡辺 信吾",
  "acquiredTime": 1775589770148,
  "readStatus": "UNKNOWN",
  "asin": "B08H7M9R7W",
  "productImage": "https://m.media-amazon.com/images/I/51hEpY7diYL.jpg"
}
```

## Implementation

- Python パッケージとして v0.1 を上書き再実装する。依存は `duckdb`, `typer`, `rich`、開発依存は `pytest`, `ruff`。日付パースは Python 標準ライブラリで処理し、追加依存は増やさない。
- デフォルト DB パスは `~/.kindb/kindle.duckdb` で v0.1 から据え置き。CLI は `--db PATH` で上書き可能。
- v0.1 由来の公式 zip 取り込みコード・テスト・fixture は **main 上で全削除**(別ブランチ退避は不要)。
- CLI:
  - `kindb import <kindle.json>`: 一時 DB に全件取り込み、成功後に既存 DB を置換する。
  - `kindb status`: 最終取り込み日時、蔵書数、著者数(分割後 unique)、**`read_status` 別の内訳**(`GROUP BY read_status` の結果を全件表示。`READING` 等が将来出現しても抜け落ちないように、固定 2 値ではなく動的に列挙する)、画像 URL 保有冊数を表示。
  - `kindb search <term>`: `v_books` を対象に、`title`, `authors_text`, `asin`, `read_status` を `ILIKE` で検索する。ユーザー入力の `%` / `_` / `\` はエスケープして `ILIKE ... ESCAPE '\'` で実行する。
  - `kindb query "<SQL>" [--table]`: 読み取り専用接続で `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` / `PRAGMA` のみ実行し、JSON または表形式で出力する。
  - `kindb authors`: 分割済み著者で冊数を多い順に表示する。
  - `kindb recent [--limit/-n N]`: `acquired_at DESC` で表紙 URL と読書状態を含む直近の本を表示する(デフォルト 20 件)。
  - `kindb delete [--yes]`: 確認つきで DB を削除する。
- v0.1 から**削除する CLI**: `genres`, `series`, `reading`。これらに対応する DuckDB テーブル・ビュー・読み込みコードもすべて削除。

### import 仕様

- 入力 JSON の構造検証:
  - ルートが配列でない場合はエラー。
  - 各要素はオブジェクトでなければエラー。
  - 必須キー (`title`, `authors`, `acquiredTime`, `readStatus`, `asin`) のうち 1 つでも欠落・null・空文字列の要素があれば、該当 ASIN または index(ASIN 取得不能時)を列挙してエラー終了。
  - `productImage` の欠落・null・空文字列は許容して `NULL` を保存。
  - 同一 JSON 内で ASIN が重複していたら、重複 ASIN を列挙してエラー終了。
- 各キーの型検証(非公式 JSON のため明示的に検査する):
  - `title` / `authors` / `readStatus` / `asin`: `str` 型のみ許容。
  - `productImage`: `str` または欠落/`null` のみ許容。空文字列は `NULL` 扱い。
  - `acquiredTime`: `int` のみ許容。ただし Python では `bool` が `int` のサブクラスであるため、`true` / `false` は明示的に拒否する(非数値・浮動小数点・負数・将来日付過ぎる値はエラー。具体的には `0 <= acquiredTime < 4102444800000` (2100-01-01 UTC) の範囲外をエラー)。
  - 型不一致は該当 ASIN/index と期待型を含むメッセージでエラー終了。
- 未知キー(上記 6 種以外のキー)が要素に含まれていた場合は、CLI 利用者に見えるよう stderr に警告を出して**無視**する(import は続行)。Chrome 拡張側のフィールド追加で import が止まらないようにする。
- `acquiredTime` (epoch ms) は **Python 側で固定変換**する: `datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)` を使い、naive `TIMESTAMP` (UTC 値) として `acquired_at` に保存する。DuckDB の `to_timestamp` はセッション TZ や `TIMESTAMPTZ` の影響を受けるため使用しない。
- `authors` は元文字列をそのまま `books.authors_text` に保存し、`, ` で分割した各要素(前後空白を `strip()`)を `book_authors` に `author_order` 1 始まりで投入する。空要素(連続カンマ等)はスキップ。
- `readStatus` は原値をそのまま `books.read_status` に保存(値域チェックはしない。未知の値も素通し)。
- 単一 JSON 由来のため、テーブル列としての `source_file` は持たず、`import_metadata.source_path` に集約。`source_path` は **`Path(input).resolve()` の絶対パス**を保存する。

### WAL サイドカーの取り扱い

- v0.1 の `db.wal_path()` ヘルパーは v0.2 でも維持する(`<db_path>.wal` を返す)。
- import 時: 一時 DB → 既存 DB 置換後に、旧 DB に紐づく `.wal` が孤児として残らないよう確実に削除する。新 DB 側でも置換完了直後に WAL が無いことを確認する。
- `kindb delete`: DB 本体ファイルに加え `<db_path>.wal` も削除する。
- 理由: `.wal` が残ると次回 DuckDB 起動時に削除済み/置換済み DB へ巻き戻ろうとして事故るため。

## Schema

### `books`
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR PRIMARY KEY | Amazon ASIN |
| `title` | VARCHAR NOT NULL | 書名(JSON 原値) |
| `authors_text` | VARCHAR NOT NULL | 著者の元文字列(分割前) |
| `acquired_at` | TIMESTAMP NOT NULL | `acquiredTime` を epoch ms から変換 |
| `read_status` | VARCHAR NOT NULL | JSON 原値(`READ` / `UNKNOWN` 等) |
| `product_image_url` | VARCHAR | 表紙画像 URL。欠落時は `NULL` |
| `imported_at` | TIMESTAMP NOT NULL | import 実行時刻 |

### `book_authors`
| 列 | 型 | 説明 |
|---|---|---|
| `asin` | VARCHAR NOT NULL | `books.asin` への参照 |
| `author_name` | VARCHAR NOT NULL | trim 済み著者名 |
| `author_order` | INTEGER NOT NULL | JSON 内の出現順(1 始まり) |

PK は `(asin, author_order)`。

### `import_metadata`
| 列 | 型 | 説明 |
|---|---|---|
| `source_path` | VARCHAR | 取り込んだ JSON ファイルの絶対パス |
| `source_type` | VARCHAR | 固定値 `'kindle_json'` |
| `books_count` | INTEGER | 取り込んだ書籍数 |
| `imported_at` | TIMESTAMP | import 実行時刻 |

シングルトン(1 行のみ保持)。

## Views

### `v_books` (1冊1行の主ビュー)
| 列 | 説明 |
|---|---|
| `asin` | |
| `title` | |
| `authors` | `LIST<VARCHAR>` (分割後の著者配列。`author_order` 昇順) |
| `authors_text` | 元文字列 |
| `read_status` | JSON 原値 |
| `product_image_url` | |
| `acquired_at` | |

### `v_author_counts`
著者別冊数(`book_authors` を集計)。`author_name`, `book_count` を返す。ビュー定義と CLI 側の最終 SELECT の両方で `ORDER BY book_count DESC, author_name ASC` を指定する。同冊数時は著者名昇順で決定的に並べ、CLI 出力とテストの安定性を確保する。

v0.1 の `v_books_with_reading` / `v_reading_summary` は廃止。読書セッションは扱わない。

## SKILL.md 改訂方針

- 「通常は `v_books` を使う」と明記。`books` / `book_authors` を直接参照するのは集計・デバッグ用途に限る。
- `read_status` の運用ルールを追加:
  - `READ` = ユーザーが Kindle 上で読了マークを付けた自己申告のフラグ。集計に使ってよい。
  - `UNKNOWN` = 「読了マークなし」。**「未読」と断定しない**(読み始めていても READ マークを付けていない場合は `UNKNOWN` のまま)。
  - 「未読書籍」を聞かれた場合は `WHERE read_status = 'UNKNOWN'` を使ってよいが、回答文では「読了マークが付いていない本」と言い換える。
- 引き続き断定しない項目: 発売日 / 出版社 / 購入価格 / Kindle Unlimited 判定 / マンガ / 固定レイアウト判定。
- `acquired_at` は「ライブラリ取得日時」であり購入日とは限らない(再ダウンロード等で更新されうる)。
- 表紙画像が必要な用途では `product_image_url` を使う。欠落 (`NULL`) はそのまま提示する。
- 代表クエリ集を v0.2 用に差し替え:
  - 著者別冊数 Top N (`v_author_counts` の SELECT)
  - 最近取得した本(`v_books` を `acquired_at DESC`)
  - 読了マーク済みの本一覧(`WHERE read_status = 'READ'`)
  - 表紙付き蔵書リスト(MCP からの一覧表示用)

## Test Plan

- 入力 fixture:
  - `tests/create_fixture.py` を JSON 生成版に書き換える(zip 生成は削除)。
  - 含めるパターン: 日本語タイトル、単一著者、複数著者(2 名・3 名以上)、`READ` / `UNKNOWN` 両方、`productImage` 欠落、Western フルネーム複数(カンマ区切り)、3 著者以上。
  - 異常系 fixture を別途用意: 必須キー欠落、ASIN 重複、`acquiredTime` 異常(非数値・bool・浮動小数点・負数・範囲外境界 `4102444800000`)、ルートが配列でない JSON、空配列(0 件成功)。
- import の検証:
  - 必須キー欠落で明示エラー、エラーメッセージに該当 ASIN / index が含まれる。
  - 各キーの型不正(`title` が数値、`productImage` がオブジェクト、`acquiredTime` が浮動小数点・負数・範囲外、`authors` が配列など)でエラー、メッセージに期待型と該当 ASIN/index が含まれる。
  - 未知キー(7 種類目以降)を持つ要素は stderr 警告のみで取り込みが続行される。
  - `productImage` 欠落・null・空文字列は `NULL` で許容。
  - `acquiredTime` (epoch ms) が UTC 固定変換で `acquired_at` に保存される(セッション TZ に依存しない)。境界値: `0`, `1775589770148` 等の代表値で値が一致することを確認。
  - 同一 JSON 内 ASIN 重複でエラー、エラーメッセージに重複 ASIN が列挙される。
  - ルートが配列でない / 各要素がオブジェクトでない JSON でエラー。
  - 空配列 JSON は 0 件成功で `import_metadata` が記録される。
  - 一時 DB に作成され、成功時のみ既存 DB を置換、失敗時は既存 DB が残る。
  - 再 import で既存データが完全に差し替わる。
  - `import_metadata.source_path` は `Path(input).resolve()` 後の絶対パスで保存される(相対パス入力時に絶対パス化されることを確認)。`source_type = 'kindle_json'` 固定であることも検証。
  - import 置換完了後、旧 DB 由来の `<db_path>.wal` が残らない。
- スキーマ・ビューの検証:
  - `books` / `book_authors` / `import_metadata` 以外のテーブルが存在しない。
  - `v_books` / `v_author_counts` 以外のビューが存在しない。
  - `v_books` が 1 ASIN 1 行で、`authors` 配列が `author_order` 順に並ぶ。
  - `authors_text` が JSON 原文字列をそのまま保持する。
- CLI の検証:
  - `status`: 冊数 / 著者数(分割後 unique) / `read_status` 別内訳(動的列挙、未知ステータスが来ても合計が蔵書数と一致する) / 画像 URL 保有冊数 / 最終取り込み日時を表示。
  - `search`: `title`, `authors_text`, `asin`, `read_status` で `ILIKE` 検索。`%` / `_` / `\` のエスケープが効く。
  - `authors`: 分割済み著者の冊数集計が `book_count DESC, author_name ASC` で表示される。
  - `recent`: `acquired_at DESC` で並び、`--limit/-n` が効く。表紙 URL と `read_status` を含む。
  - `query`: 読み取り専用接続。書き込み系 SQL を拒否し、許可プレフィックス通過後も read-only 接続で複文書き込みが拒否される二重防御を確認。
  - `delete`: 確認プロンプト、`--yes` でスキップ、削除後は DB 本体ファイルと `<db_path>.wal` の両方が存在しないことを確認。
  - 削除済み CLI (`genres`, `series`, `reading`) が `kindb --help` に存在しない。
- 静的検査: `ruff check . && pytest` を通す。

## ドキュメント更新

- `docs/kindb-v0.1-plan.md` は本ファイル (`docs/kindb-v0.2-plan.md`) で置き換える前提で**削除**(履歴は git に残る)。
- `docs/kindb-json-redesign-plan.md` は本ファイルで吸収するため**削除**。
- `CLAUDE.md` の `@docs/kindb-v0.1-plan.md` 参照を `@docs/kindb-v0.2-plan.md` に差し替え。プロジェクト概要文も `kindle.json` 専用に書き換え。
- `README.md`:
  - 「使用データ」を `kindle.json` 取得手順(Chrome 拡張)に差し替え。
  - 「使い方」を `kindb import <kindle.json>` に変更。
  - 削除コマンド (`genres`, `series`, `reading`) を一覧から除去。
  - 「主要ビュー」を `v_books` / `v_author_counts` に差し替え。
  - 「扱わない項目」セクションを v0.2 仕様に更新(読了の自己申告フラグについての扱いを追記)。
  - MCP 設定セクションは DB パス据え置きで継続。
- `SKILL.md` を上記 SKILL 改訂方針に沿って書き換え。
- `docs/manual-test-scenarios.md` を v0.2 機能セットに合わせて改訂。
- `pyproject.toml`: `version` を `0.2.0` に更新、`description` を `kindle.json` 専用前提の文言に書き換え(例: `"Import Kindle library JSON into DuckDB for local search and analysis"`)。依存パッケージは据え置き。

## Assumptions

- `kindle.json` の取得元は Chrome 拡張(本計画では拡張名・URL 等は固定しない。フォーマットが上記 6 キー仕様と一致する限り取り込み可)。フォーマット変更時は本計画を更新する。
- 完成形は CLI / DuckDB / MCP / Skill から DuckDB を参照するローカル蔵書 DB。Web UI / 公式 zip 互換は持たない。
- `kindle.json` の `authors` は元文字列を保持しつつ、`, ` 単純分割で複数著者に展開する。
- `readStatus` は `READ` / `UNKNOWN` の原値を保存する。`READ` は自己申告の読了マーク、`UNKNOWN` は「読了マークなし」として扱い、「未読」と断定しない。
- v0.2 は履歴保持や差分インポートを扱わず、最新 `kindle.json` スナップショットで DB 全体を再構築する。
- 既存 v0.1 ユーザーへの移行パスは提供しない(リリース前段階のため)。
