"""日本図書館協会の NDC9 データ(ndc9.ttl)から、3 桁の分類記号と分類名の表を作る。

使い方:
    python scripts/build_ndc_table.py <ndc9.ttl のパス> > src/kindb/data/ndc9_3digit.tsv

ndc9.ttl(7.6MB)はリポジトリに入れず、https://www.jla.or.jp/committees/bunrui/ndc-data/ から
取得したものをリポジトリの外に置いてパスで渡す。標準ライブラリだけで動かす。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SOURCE_URL = "https://www.jla.or.jp/committees/bunrui/ndc-data/"

# ttl の 1 件は「ndc9:<記号> a <型> ;」で始まり、行頭の「.」だけの行で終わる
_SUBJECT = re.compile(r"^ndc9:(\S+) a ([^;]+);", re.MULTILINE)
_NOTATION = re.compile(r'skos:notation "([^"]*)"')
_LABEL_JA = re.compile(r'skos:prefLabel "([^"]*)"@ja')


def build_rows(ttl_text: str) -> list[tuple[str, str]]:
    rows: dict[str, str] = {}
    for block in re.split(r"\n\.\n", ttl_text):
        subject = _SUBJECT.search(block)
        notation = _NOTATION.search(block)
        label = _LABEL_JA.search(block)
        if not subject or not notation or not label:
            continue
        code = notation.group(1)
        if not re.fullmatch(r"\d{3}", code):
            continue
        # 別法(ndcv:Variant)は NDL の書誌では使われないため除く。
        # 841〜888、971〜988 は ndcv:Section ではなく skos:Concept として載っているので、型では絞らない
        if "ndcv:Variant" in subject.group(2):
            continue
        if code in rows:
            raise ValueError(f"duplicate notation: {code}")
        rows[code] = label.group(1)
    return sorted(rows.items())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    rows = build_rows(Path(argv[1]).read_text(encoding="utf-8"))
    out = sys.stdout
    out.write("# NDC9 の 3 桁の分類記号と分類名\n")
    out.write("# 出典: 日本図書館協会分類委員会「NDC9 データ」(ndc9.ttl)、CC BY\n")
    out.write(f"# {SOURCE_URL}\n")
    out.write("# scripts/build_ndc_table.py で ndc9.ttl から生成した。分類名は配布データの表記のまま\n")
    for code, label in rows:
        out.write(f"{code}\t{label}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
