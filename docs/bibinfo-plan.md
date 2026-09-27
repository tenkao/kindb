# 書誌情報の追加 実装計画

`docs/bibinfo-requirements.md` を実装する手順。語の意味は `docs/glossary.md` に従う。細部は試行で決め、困った点だけこの計画に戻す。

## 全体像

```
kindb enrich --where "<v_books への条件>" [--overrides <csv>]
  0. --overrides があれば訂正を置き換え、内容が変わった本の状態を戻す(短い書き込みトランザクション)
  1. 読み取り専用で対象の ASIN を選ぶ(状態が残っている本は飛ばす)
  2. DB を閉じたまま NDL サーチを直列で引く(3 秒以上の間隔、cnt=500)
  3. 候補を手元に溜め、N 冊ごとに短い書き込みトランザクションで保存
  4. 保存した候補から照合し、結果も同じトランザクションで保存

kindb rematch
  保存済みの候補と保存済みの訂正から照合だけをやり直す(通信せず、訂正も変えない)
```

## モジュール

| ファイル | 責務 | 外部依存 |
|---|---|---|
| `src/kindb/ndl.py` | OpenSearch の URL 組み立て、HTTP、RSS の解析、間隔と 429 の待機 | 標準ライブラリの `urllib` と `xml.etree` だけ。依存を足すと `constraints.txt` の再生成とツールの再インストールが要るため |
| `src/kindb/matching.py` | 書名の正規化、巻数とレーベルの抽出、候補の絞り込み、照合結果(作品の属性と版の属性)の決定 | なし(純粋関数) |
| `src/kindb/enrich.py` | 対象の選択、取得の進行、バッファと書き込み、再開、rematch | `ndl`, `matching`, `db` |
| `src/kindb/sqlguard.py` | `kindb query` の SQL 検査を `cli.py` から移す | なし |
| `src/kindb/data/ndc9_3digit.tsv` | NDC9 の 3 桁の分類名(JLA、CC-BY) | |

`sqlguard.py` に移すのは、`enrich.py` が `--where` の検査に同じ関数を使い、`cli.py` から import すると循環するため。

## テーブル

新しいテーブルだけを足すので、既存テーブルの移行処理は要らない(`CREATE TABLE IF NOT EXISTS` とハッシュ移行で入る)。

| テーブル | キー | 内容 |
|---|---|---|
| `bib_fetches` | `asin` | 取得の状態(`found` / `not_found` / `incomplete` / `excluded` / `error`)、使ったクエリと各段の総件数、取得日時 |
| `bib_candidates` | `(asin, candidate_id)` | 紙版の候補の `<item>` の XML 原文と検索順位 |
| `bib_matches` | `asin` | 照合方法、採用した候補 ID、版の属性、NDC(記号と版)、照合日時 |
| `bib_subjects` | `(asin, subject_order)` | 件名 |
| `bib_notes` | `(asin, note_order)` | 原題などの注記 |
| `bib_overrides` | `asin` | 手動訂正(ISBN か NULL) |
| `bib_metadata` | シングルトン | 最後の enrich / rematch の日時と件数 |

- `bib_candidates` には、JPRO の電子書籍の書誌を除いた紙版の候補だけを入れる。標本 50 冊の生の応答は 4.1MB あり、全部を残すと全冊で 200MB を超える見込みのため。
- `bib_overrides` は `enrich --overrides <csv>` を渡したときに全件置換する。rematch も同じ訂正を使えるように DB に持つ。
- 状態の意味(段と採用条件は requirements の「検索結果の取得」): `found` は採用条件を満たす候補が残った段があった、`not_found` は全段が解消して採用条件を満たす候補が 0 件、`incomplete` はそのどちらでもなく未解消の段があった(保留)、`excluded` は訂正で ISBN が空、`error` は通信の失敗。
- `bib_candidates` は `found` の本についてだけ持つ。引き直しと訂正の変更のたびに、その本の行を全部消してから入れる。

NDC 分類名は、同梱の TSV を Python 側で `VALUES` の CTE に展開して `VIEWS_SQL` に埋め込む案を第一候補にする。`create_schema()` を DDL だけに保てて、TSV を変えればハッシュが変わり、自動で移行されるため。ビュー定義が 1,000 行程度長くなるのが欠点で、遅ければテーブルに切り替える。

## 照合の例

規則は requirements にある。判断に迷いやすい場面を例で示す。

| 場面 | 結果 |
|---|---|
| 『ファスト&スロー(上)』で単行本(NDC9 141.5、件名「思考」)と文庫(NDC10 141.5、件名「思考」)が残る | NDC は版の違いを無視して 141.5 で一致するので採用。件名は共通の「思考」を採用。ISBN、刊行年月、出版社は付けない |
| 同じく、件名が単行本「思考」、文庫「思考 / 意思決定」 | 件名は共通部分の「思考」だけを採用 |
| 「谷口ジローコレクション18」 | 18 は叢書番号。`seriesTitle` の `; 18` と照らし、巻数としては使わない |
| Kindle 著者「ダニエル・カーネマン」、NDL「Kahneman, Daniel, 1934-2024」 | 著者名は検索にだけ使うので、不一致でも落とさない |
| 「理想のヒモ生活(25)」で電子書籍の分冊版が 238 件混ざる | 電子書籍の書誌を除いてから巻数で絞る |
| 巻数もレーベルもない本で、書名に検索語を含む別作品が 1 件だけ返る | 正規化した書名が一致しないので捨てる。候補 0 件で `not_found` |
| Kindle「リーダブルコード より良いコードを書くためのシンプルで実践的なテクニック」、NDL「リーダブルコード : より良いコードを書くための…」 | 本タイトルと副題の連結が一致するので同じ作品 |
| 書名と著者の検索で総件数 800 件 | 電子書籍の除外、巻数の語を足して検索し直す。それでも 500 件超ならその段は未解消。ほかの段でも採用できる候補がなければ `incomplete`。候補も照合結果も付けない |
| 書名と著者の検索が 800 件、絞り直すと 100 件で採用できる候補なし。ほかの段も解消して候補なし | 途中の超過は段の結論に影響しないので `not_found` |
| 書名と著者の検索は 0 件、書名だけの検索は総件数 120 件で同じ作品なし | `not_found` |
| 「○○(3)」で、書名と著者の検索では同じ作品の 1 巻と 2 巻だけ、書名だけの検索では 3 巻も返る | 1 段目は巻数で全部落ちるので止めずに進み、2 段目の 3 巻を採用 |
| 手動訂正で ISBN が指定されている | 書名検索をせず ISBN で引く。当たった紙版をそのまま採用 |
| 取得済みの本に、あとから訂正で ISBN を足す、または変える | 状態、候補、照合結果を消して未取得に戻し、次の enrich で ISBN で引く |
| 手動訂正で ISBN が空 | 候補と照合結果を消して `excluded`。取得もしない |
| 訂正の行を消す | 状態、候補、照合結果を消して未取得に戻し、書名検索からやり直す |
| 訂正の行を消したあと、`--where` の対象外で引き直さないまま rematch | 未取得の本は rematch の対象外。古い ISBN の候補は消えているので、再採用されない |

## 取得と書き込み

- 1 回の書き込みは import と同じ形にする: `BEGIN` → 対象 ASIN の行を `DELETE` → `INSERT` → `COMMIT` → `CHECKPOINT`。`create_schema()` は開始時に 1 回だけ実行する。
- 書き込み時にロックの衝突が起きたら、待って再試行する。`_report_locked_db` のように終了すると、バッファの取得結果が消えるため。
- Ctrl-C を受けたら、バッファを書き込んでから終わる。
- 再開の扱い: `found`、`not_found`、`incomplete`、`excluded` は飛ばす。`not_found` と `incomplete` は `--retry-missing` のときだけ引き直す。`--refresh` のときは `excluded` 以外をすべて引き直す(ISBN が指定された本は ISBN で)。`error` は次回に自動で引き直す。訂正で内容が変わった本は手順 0 で状態が消えているので、未取得として拾われる。
- 引き直した本は、状態、候補、照合結果を同じ書き込みトランザクションで置き換える。引き直しで `error` になった本は置き換えず、前回の行を残す。
- 総件数が 500 件以下の応答だけを「全部見た」とみなし、照合に使う。保存する候補は、検索を止めた段の最後の応答から取る。
- User-Agent に kindb の名前と連絡先の URL を入れる。

## CLI

| コマンド | 主な引数 |
|---|---|
| `kindb enrich` | `--where`、`--limit`(今回の冊数上限)、`--overrides`、`--retry-missing`、`--refresh`(`--where` の対象を状態によらず引き直す)、`--interval`(既定 3.0、下限あり) |
| `kindb rematch` | なし(訂正は enrich でだけ変える) |
| `kindb status` | 書誌情報の件数(照合方法ごと、未取得、見つからない)を足す |
| `kindb search` | 件名も検索対象にする。件名の列は表に出さない。80 桁の表がさらに狭くなるため |

`v_books` に足す列の案: `isbn`, `paper_issued`(文字列)、`publisher`, `pages`, `bib_series`, `ndc`, `ndc_label`, `subjects`(`VARCHAR[]`、なければ `[]`)、`bib_match`(照合方法)。

## 手順

各段で `uv run ruff check . && uv run pytest` を通す。

0. **下調べ**(スキーマを固める前に行う)
   - `dpid=iss-ndl-opac` や `mediatype` の指定で電子書籍の書誌を除けるかを、標本の数冊で確かめる。
   - JLA の `ndc9.ttl` を取得して 3 桁の TSV を作る。取得は利用者に依頼する(curl が権限で拒否されるため)。NDC10 の記号の上位 3 桁で名前を引いたとき、明らかにずれるものがないかを標本の NDC で見る。
   - hatchling が `src/kindb/data/` の TSV を wheel と editable インストールに含めるかを確かめる。
   - 結果は requirements の「未確認の事実」に書き戻す。
1. `sqlguard.py` への移動(振る舞いは変えない)
2. `matching.py` をテスト先行で作る。上の「照合の例」と標本の落とし穴をテストケースにする。別作品しか返らない場合(書名の一部だけ一致する 1 件、前方一致する別の本)が `not_found` になることを必ず含める
3. `ndl.py`。標本の応答 XML を数件 `tests/fixtures/ndl/` に置き(出典を README に書く)、解析をテストする。HTTP は差し替えられるようにし、テストでは通信しない
4. テーブルと `enrich.py`、`kindb enrich` / `rematch`。偽の HTTP で、再開、ロックの再試行、Ctrl-C を確かめる。あわせて次を確かめる: 総件数 500 件超の絞り直しで、未解消なら `incomplete`、解消して候補 0 件なら `not_found` になること。別の巻しか返らない段で止まらず次の段へ進むこと。訂正の追加、変更、空、削除のそれぞれで状態と候補が消えること。`--refresh` で `found` の本の候補と照合結果が新しい応答のものに置き換わり、`--where` の対象外と `excluded` の本は変わらないこと。`--refresh` 中に通信が失敗した本は前回の行が残ること。rematch が通信せず、`found` 以外の本を扱わないこと
5. ビュー(`v_books` の列、NDC 分類名)、`search`、`status`
6. 文書: `docs/spec.md`(「扱わない項目」から発売日と出版社を外す、各節を追加)、`SKILL.md`、README(コマンド、出典、会話冒頭文)、CLAUDE.md(コード構成、不変条件、更新先の表)、手動テスト、版を 0.4.0 に
7. 実データでの確認: 実 DB を作業用ディレクトリに複製し、`enrich --where "<非マンガ>" --limit 50` を実際に NDL に対して流す。照合結果を目で見て、標本測定と同程度に当たっているかを確かめる。全冊の実行は利用者に任せる

## 既存テストへの影響

- `v_books` の列を足しても、列を名指しする既存テストは壊れない。`DESCRIBE v_books`(`tests/test_cli.py`)は検査の通過だけを見ている。
- `tests/test_skill.py` は SKILL.md の SQL をすべて fixture で実行する。件名や NDC の例が空の結果にならないよう、fixture に書誌情報の行を入れる。

## スコープ外

requirements のスコープ外に加えて:

- 取得の並列化
- `bib_candidates` の古い候補の掃除
