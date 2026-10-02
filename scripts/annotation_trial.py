"""AI による書籍の分類・概要の標本検証を支える補助スクリプト。

kindb 本体には組み込まない実験用の道具。docs/ai-annotation-plan.md の手順で使う。

    uv run python scripts/annotation_trial.py export --db TMP.duckdb --out sample.jsonl
    uv run python scripts/annotation_trial.py prompt > system_prompt.txt
    uv run python scripts/annotation_trial.py check results.jsonl --sheet score.csv

DB は読み取り専用で開く。使うのは v_books だけで、実際の蔵書 DB に対しても書き込まない。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from kindb.db import connect, get_db_path

# 形態は 1 つ、主題は最大 3 つ。標本で足りない語や紛らわしい語が見つかったら、この表を直す
# (プロンプトは prompt で再生成する)
FORMS = [
    "マンガ",
    "ライトノベル",
    "小説",
    "一般書",
    "専門書・学術書",
    "児童書・絵本",
    "雑誌・ムック",
    "画集・写真集",
    "その他",
]
TOPICS = [
    # 物語の主題
    "恋愛",
    "ミステリ・サスペンス",
    "SF",
    "ファンタジー・異世界",
    "ホラー",
    "歴史・時代",
    "青春・学園",
    "日常・コメディ",
    "バトル・アクション",
    "スポーツ",
    "職業・仕事",
    # 解説・論述の主題
    "哲学・思想",
    "宗教",
    "心理学",
    "歴史",
    "政治・社会",
    "経済・経営",
    "科学・技術",
    "数学",
    "情報・プログラミング",
    "医学・健康",
    "言語・語学",
    "芸術・音楽",
    "教育",
    "自己啓発",
    "生活・実用",
    "料理・グルメ",
    "趣味・娯楽",
    "旅行・地理",
    "伝記・エッセイ",
]
BASES = ["known", "web", "title_only", "unknown"]
CONFIDENCES = ["high", "medium", "low"]
SUMMARY_MAX = 100

SYSTEM_PROMPT = """\
あなたは蔵書の分類担当です。作品ごとの入力から、分類と短い概要を JSON で返します。
入力は書名、著者、Kindle ジャンル、シリーズ名と、あれば NDC の分類名・件名・出版社です。
誤った情報を書くより「分からない」と答えるほうを選びます。

出力は作品ごとに 1 つの JSON オブジェクトで、1 行に 1 作品(JSON Lines)。前置きや説明は付けません。
{{"work_key": 入力のまま, "form": 形態, "topics": [主題, ...], "tags": [自由タグ, ...], "summary": 概要,
  "basis": 根拠, "confidence": 確信度, "sources": [URL, ...]}}

- form は次のどれか 1 つ: {forms}
- topics は次から 0〜3 個(当てはまるものだけ): {topics}
- tags は自由記述の短い語を 0〜5 個。作品の特徴(例: 「異世界転生」「料理人」「ミステリ」)。
- summary は {summary_max} 字以内の自分の言葉による概要。出版社の紹介文などの文章は写さない。
- basis は根拠で、次のどれか 1 つ。
  - known: 自分が作品を知っていて、書いてある内容に確信がある
  - web: Web 検索で確かめた(sources に URL を入れる)
  - title_only: 書名・著者・ジャンルからの推測だけ
  - unknown: 作品を特定できない。このとき form と topics は null と [] にし、summary は空文字にする
- confidence は high / medium / low。作品の内容について確信が持てないときは low にする。
- 書名が同じでも別作品の可能性があるときは、著者とジャンルで確かめ、確かめられなければ unknown にする。
- 入力に ISBN があれば作品の特定に使ってよい。ISBN を知識から逆引きしようとしない。
""".format(forms=" / ".join(FORMS), topics=" / ".join(TOPICS), summary_max=SUMMARY_MAX)

# 作品 = シリーズ(series_asin があれば)または単巻。代表の 1 冊は巻数の若い順
EXPORT_SQL = """
WITH base AS (
    SELECT
        coalesce(series_asin, asin) AS work_key,
        asin, title, authors, genres, series_title, series_position,
        isbn, ndc_label, subjects, publisher, paper_issued,
        CASE
            WHEN list_contains(genres, 'コミック・ラノベ・BL') THEN 'manga'
            WHEN genres = [] AND ndc IS NULL THEN 'unclear'
            ELSE 'other'
        END AS kind
    FROM v_books
),
works AS (
    SELECT
        work_key,
        arg_min(asin, coalesce(series_position, 0)) AS asin,
        arg_min(title, coalesce(series_position, 0)) AS title,
        arg_min(authors, coalesce(series_position, 0)) AS authors,
        arg_min(genres, coalesce(series_position, 0)) AS genres,
        any_value(series_title) AS series_title,
        count(*) AS volumes_owned,
        arg_min(isbn, coalesce(series_position, 0)) FILTER (WHERE isbn IS NOT NULL) AS isbn,
        arg_min(ndc_label, coalesce(series_position, 0)) FILTER (WHERE ndc_label IS NOT NULL) AS ndc_label,
        arg_min(subjects, coalesce(series_position, 0)) AS subjects,
        arg_min(publisher, coalesce(series_position, 0)) FILTER (WHERE publisher IS NOT NULL) AS publisher,
        any_value(kind) AS kind
    FROM base
    GROUP BY work_key
)
SELECT * FROM works
"""


def _stratum(row: dict) -> str:
    # kind は Kindle ジャンルと NDC の有無による粗い推定で、genres が空で NDC もない本は unclear にする
    return "{}-{}".format(row["kind"], "isbn" if row["isbn"] else "noisbn")


def cmd_export(args: argparse.Namespace) -> int:
    path = get_db_path(args.db)
    if not path.exists():
        print(f"DB がありません: {path}", file=sys.stderr)
        return 1
    con = connect(path, read_only=True)
    try:
        cur = con.execute(EXPORT_SQL)
        names = [d[0] for d in cur.description]
        works = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        con.close()

    # 層ごとに、作品キーとシードのハッシュ順で先頭から取る(シードが同じなら同じ標本になる)
    def order(w: dict) -> str:
        return hashlib.sha256(f"{args.seed}:{w['work_key']}".encode()).hexdigest()

    strata: dict[str, list[dict]] = {}
    for w in works:
        strata.setdefault(_stratum(w), []).append(w)
    sample = []
    for name in sorted(strata):
        picked = sorted(strata[name], key=order)[: args.per_stratum]
        print(f"{name}: {len(strata[name])} 作品から {len(picked)} 作品", file=sys.stderr)
        sample.extend(picked)

    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for w in sample:
            w = {k: v for k, v in w.items() if k != "kind"}
            if args.no_isbn:
                w["isbn"] = None
            w = {k: v for k, v in w.items() if v not in (None, [], "")}
            f.write(json.dumps(w, ensure_ascii=False, default=str) + "\n")
    print(f"{len(sample)} 作品を {out} に書きました(全 {len(works)} 作品)", file=sys.stderr)
    return 0


def cmd_prompt(_: argparse.Namespace) -> int:
    sys.stdout.write(SYSTEM_PROMPT)
    return 0


def validate(row: dict) -> list[str]:
    """1 作品の結果の違反を返す。空なら問題なし。"""
    errors = []
    if not isinstance(row.get("work_key"), str):
        errors.append("work_key がない")
    basis = row.get("basis")
    if basis not in BASES:
        errors.append(f"basis が不正: {basis!r}")
    if row.get("confidence") not in CONFIDENCES:
        errors.append(f"confidence が不正: {row.get('confidence')!r}")
    topics = row.get("topics") or []
    if basis == "unknown":
        if row.get("form") is not None or topics or row.get("summary"):
            errors.append("unknown なのに form / topics / summary がある")
    else:
        if row.get("form") not in FORMS:
            errors.append(f"form が語彙にない: {row.get('form')!r}")
        if len(topics) > 3:
            errors.append("topics が 3 個を超える")
        errors += [f"topics が語彙にない: {t!r}" for t in topics if t not in TOPICS]
        summary = row.get("summary") or ""
        if not summary:
            errors.append("summary が空")
        elif len(summary) > SUMMARY_MAX:
            errors.append(f"summary が {SUMMARY_MAX} 字を超える({len(summary)} 字)")
    if basis == "web" and not row.get("sources"):
        errors.append("web なのに sources がない")
    return errors


def cmd_check(args: argparse.Namespace) -> int:
    rows, bad = [], 0
    for lineno, line in enumerate(Path(args.results).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"{lineno} 行目: JSON として読めない: {e}")
            bad += 1
            continue
        errors = validate(row)
        if errors:
            bad += 1
            print(f"{lineno} 行目 ({row.get('work_key')}): " + "; ".join(errors))
        rows.append(row)
    print(f"\n{len(rows)} 件を読み、違反のある行 {bad} 件")
    for key in ("basis", "confidence", "form"):
        print(f"{key}: {dict(Counter(r.get(key) for r in rows))}")

    if args.sheet:
        titles = {}
        if args.sample:
            for line in Path(args.sample).read_text(encoding="utf-8").splitlines():
                w = json.loads(line)
                titles[w["work_key"]] = w.get("series_title") or w.get("title", "")
        with Path(args.sheet).open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "work_key",
                    "title",
                    "form",
                    "topics",
                    "summary",
                    "basis",
                    "confidence",
                    "form_ok",
                    "topics_ok",
                    "summary_ok",
                ]
            )
            for r in rows:
                writer.writerow(
                    [
                        r.get("work_key"),
                        titles.get(r.get("work_key"), ""),
                        r.get("form"),
                        "/".join(r.get("topics") or []),
                        r.get("summary"),
                        r.get("basis"),
                        r.get("confidence"),
                        "",
                        "",
                        "",
                    ]
                )
        print(f"採点表を {args.sheet} に書きました(form_ok などの列に 1/0 を入れる)")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export", help="標本の入力を JSON Lines で書き出す")
    p.add_argument("--db", help="DB のパス(省略時は KINDB_DB_PATH か既定。動作確認は一時 DB を指定する)")
    p.add_argument("--out", required=True)
    p.add_argument("--per-stratum", type=int, default=12, help="層(ジャンルの推定 × ISBN があるか)ごとの作品数")
    p.add_argument("--seed", default="0")
    p.add_argument("--no-isbn", action="store_true", help="ISBN を入力から外す(ISBN の有無の比較用)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("prompt", help="語彙を埋め込んだシステムプロンプトを標準出力へ書く")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("check", help="結果の JSON Lines を検証し、分布と採点表を出す")
    p.add_argument("results")
    p.add_argument("--sample", help="export の出力。採点表に書名を入れる")
    p.add_argument("--sheet", help="採点用 CSV の出力先")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
