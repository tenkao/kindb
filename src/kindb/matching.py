"""Kindle の書名と NDL サーチの候補の照合。通信も DB も扱わない純粋関数だけを置く。

規則の理由は docs/bibinfo-requirements.md の「照合」と「検索結果の取得」にある。精度を優先し、
紙版を 1 つに定められなければ版の属性は付けず、同じ作品と確かめられない候補は採用しない。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from kindb.ndl import NdlRecord

# NDL 自身の書誌の ID の接頭辞。件名を NDLSH として扱えるのはこの書誌だけ
NDL_RECORD_PREFIX = "R100000002-"

# 巻数として扱う語。「ファスト&スロー(上)」の「上」など
VOLUME_WORDS = ("上", "中", "下", "前編", "中編", "後編")

# 紙版にない電子書籍だけの版名。長いものを先に置き、「フルカラー版」から「カラー版」だけを消さないようにする
_DIGITAL_ONLY = re.compile(
    r"(フルカラー版|セミカラー版|モノクロ版|カラー版|連載版|分冊版|単話版|合本版|コミックス版|"
    r"電子書籍版|電子特別版|電子限定版|電子版|デジタル版)"
)
# 【】の中身がこれを含めば、電子書籍の版名や宣伝として丸ごと除く。含まなければ括弧だけ外す(【状態異常スキル】など)
_BRACKET_LABELLIKE = re.compile(r"(版|連載|分冊|単話|特典|期間限定|合本|カラー|コミックス|電子|デジタル|話)")
_EDITION_TOKEN = re.compile(
    r"^(新版|改訂新版|増補改訂版|新訂版|改訂版|増補版|新装版|完全版|愛蔵版|豪華版|決定版|普及版|第\d+版)$"
)
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")
_ROMAN = {
    "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6", "VII": "7", "VIII": "8", "IX": "9", "X": "10"
}
_ENCLOSED = r"[―—‐~～〜-][^―—‐~～〜-]+[―—‐~～〜-]"
_VOLUME_WORD = "|".join(VOLUME_WORDS)
# 書名の末尾から巻数を探す順。見つかった最初の 1 つを使う
_KINDLE_VOLUME_PATTERNS = (
    re.compile(r"\((\d+)\)$"),
    re.compile(r"\[(\d+)\]$"),
    re.compile(rf"\(({_VOLUME_WORD})\)$"),
    re.compile(r"(\d+)\s*巻$"),
    re.compile(rf"\s({_VOLUME_WORD})$"),
    re.compile(r"\s(\d+)$"),
    # 「星界の紋章 2―ささやかな戦い―」のように、巻数のあとに巻の副題が続く書名
    re.compile(rf"\s(\d+)\s*(?={_ENCLOSED}$)"),
    re.compile(r"\s(I|II|III|IV|V|VI|VII|VIII|IX|X)$"),
    # 「…PART2」のように文字の直後に付いた数字。小数(2.5)の一部は巻数にしない
    re.compile(r"(?<=[^\d\s.])(\d+)$"),
    # 「ファスト＆スロー（上） あなたの意思は…」のように、巻数のあとに副題が続く書名。末尾の形より後に試す
    re.compile(rf"\((\d+|{_VOLUME_WORD})\)(?=\s)"),
)
_CANDIDATE_VOLUME_RANGE = re.compile(r"\d+\s*[-‐~〜]\s*\d+")
_CANDIDATE_VOLUME_HEAD = re.compile(
    r"^\s*(?:no\.?|vol\.?|volume|part|第)?\s*\d+\s*(?:巻|集|号|章|部)?", re.IGNORECASE
)
_NON_WORD = re.compile(r"[\W_]+")
_READING = re.compile(r"《[^《》]*》|\([^()]*\)")
_SERIES_NUMBER = re.compile(r"^(.*?)\s*;\s*(\d+)\s*$")


@dataclass(frozen=True)
class Book:
    asin: str
    title: str
    authors_text: str
    series_title: str | None = None


@dataclass(frozen=True)
class KindleTitle:
    """照合に使う形に分解した Kindle の書名。"""

    key: str
    volume: str | None
    labels: tuple[str, ...]
    editions: tuple[str, ...]
    search_text: str


@dataclass(frozen=True)
class Stage:
    """検索の 1 段。params で総件数が 500 件を超えたら、refinements を順に足して検索し直す。"""

    name: str
    params: dict[str, str]
    refinements: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class Match:
    """照合結果。method は "edition"(紙版が 1 つ)、"work"(作品までしか定まらない)、"isbn"(手動訂正)。"""

    method: str
    candidate_ids: tuple[str, ...]
    isbn: str | None = None
    paper_issued: str | None = None
    publisher: str | None = None
    pages: int | None = None
    bib_series: str | None = None
    ndc: str | None = None
    ndc_edition: str | None = None
    subjects: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def normalize_key(text: str) -> str:
    """書名を比較用の文字列にする。Kindle の書名にも候補の書名にも同じ規則を当てる。"""
    text = nfkc(text)
    # 読みや振り仮名は片方にだけ付くことが多い(「とある科学の超電磁砲 (レールガン)」「救世主《メシア》」)
    text = _READING.sub(" ", text)
    text = _DIGITAL_ONLY.sub(" ", text)
    text = " ".join(token for token in text.split() if not _EDITION_TOKEN.match(token))
    return _NON_WORD.sub("", text).casefold()


def parse_kindle_title(title: str, series_title: str | None = None) -> KindleTitle:
    text = nfkc(title).strip()

    labels: list[str] = []
    # 末尾の (…) はレーベル名として外す。中身が数字や巻の語なら巻数なので残す
    while True:
        m = _TRAILING_PAREN.search(text)
        if not m or m.group(1).strip().isdigit() or m.group(1).strip() in VOLUME_WORDS:
            break
        if m.group(1).strip():
            labels.append(m.group(1).strip())
        text = text[: m.start()].rstrip()

    editions: list[str] = []

    def bracket(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if _EDITION_TOKEN.match(inner):
            editions.append(inner)
            return " "
        if _BRACKET_LABELLIKE.search(inner):
            return " "
        return inner

    text = re.sub(r"【([^】]*)】", bracket, text)
    text = _DIGITAL_ONLY.sub(" ", text)
    tokens = []
    for token in text.split():
        if _EDITION_TOKEN.match(token):
            editions.append(token)
        else:
            tokens.append(token)
    text = " ".join(tokens)

    # Kindle の書名の末尾に付いたシリーズ名(「星界の断章 Ⅰ 星界シリーズ」)を外す
    if series_title:
        series_key = normalize_key(series_title)
        words = text.split()
        for i in range(1, len(words)):
            if series_key and normalize_key(" ".join(words[i:])) == series_key:
                text = " ".join(words[:i])
                break

    volume = None
    for pattern in _KINDLE_VOLUME_PATTERNS:
        m = pattern.search(text)
        if m:
            volume = _ROMAN.get(m.group(1), m.group(1))
            if volume.isdigit():
                volume = str(int(volume))
            text = (text[: m.start()] + " " + text[m.end() :]).strip()
            break

    return KindleTitle(
        key=normalize_key(text),
        volume=volume,
        labels=tuple(labels),
        editions=tuple(editions),
        search_text=_search_text(text),
    )


def _search_text(text: str) -> str:
    # OpenSearch の title は部分一致で、空白区切りの語は AND になる。記号の表記は NDL と食い違いやすいので
    # 語の区切りに置き換え、読みは NDL に無いことがあるので落とす
    text = _READING.sub(" ", nfkc(text))
    return " ".join(_NON_WORD.sub(" ", text).split())


def search_creator(authors_text: str) -> str:
    first = nfkc(authors_text.split(",")[0])
    first = first.replace("ほか", " ")
    return " ".join(t for t in re.split(r"[\s・･=]+", first) if t)


def search_stages(book: Book) -> list[Stage]:
    """書名と著者 → 書名だけ → シリーズ名と著者、の順の検索。条件が空の段は作らない。"""
    kindle = parse_kindle_title(book.title, book.series_title)
    creator = search_creator(book.authors_text)
    volume_term = {"title": f"{kindle.search_text} {kindle.volume}"} if kindle.volume else None

    stages: list[Stage] = []
    if kindle.search_text:
        base = {"title": kindle.search_text}
        with_creator = {**base, "creator": creator} if creator else None
        if with_creator:
            stages.append(
                Stage("title_creator", with_creator, _refinements(with_creator, [volume_term]))
            )
        stages.append(
            Stage(
                "title",
                base,
                _refinements(base, [{"creator": creator} if creator else None, volume_term]),
            )
        )
    if book.series_title and creator:
        series_text = _search_text(parse_kindle_title(book.series_title).search_text)
        if series_text:
            params = {"title": series_text, "creator": creator}
            series_volume = {"title": f"{series_text} {kindle.volume}"} if kindle.volume else None
            stages.append(Stage("series_creator", params, _refinements(params, [series_volume])))
    return stages


def _refinements(base: dict[str, str], steps: list[dict[str, str] | None]) -> tuple[dict[str, str], ...]:
    """足していく条件を累積した検索条件の列にする。変化のない段は飛ばす。"""
    result: list[dict[str, str]] = []
    current = dict(base)
    for step in steps:
        if not step:
            continue
        updated = {**current, **step}
        if updated != current:
            result.append(updated)
            current = updated
    return tuple(result)


def is_candidate(record: NdlRecord) -> bool:
    """紙版の候補として保存する書誌か。NDL 自身の書誌で、紙の図書であるもの。"""
    return record.id.startswith(NDL_RECORD_PREFIX) and record.is_paper_book


@dataclass(frozen=True)
class _CandidateVolume:
    key: str | None
    text: str
    has_value: bool


def _candidate_volume(volume: str | None) -> _CandidateVolume:
    if not volume or not volume.strip():
        return _CandidateVolume(None, "", False)
    text = nfkc(volume).strip()
    bare = re.sub(r"[()\s]", "", text)
    if bare in VOLUME_WORDS:
        return _CandidateVolume(bare, "", True)
    if _CANDIDATE_VOLUME_RANGE.search(text):
        # 「1-3」のような合本は、単独の巻とは対応させない
        return _CandidateVolume("range", "", True)
    number = re.search(r"\d+", text)
    rest = _CANDIDATE_VOLUME_HEAD.sub("", text) if number else text
    rest = normalize_key(rest.replace("(", " ").replace(")", " "))
    return _CandidateVolume(str(int(number.group(0))) if number else None, rest, True)


def _title_variants(record: NdlRecord, volume: _CandidateVolume) -> tuple[set[str], set[str]]:
    """候補の書名の比較用の形。(本タイトルか本タイトル+副題, それに巻の副題を足したもの)。"""
    title = nfkc(record.title)
    # 並列タイトル(「X = Y」)は書名の比較に使わない
    title = re.sub(r" = [^:]*", "", title)
    main = re.split(r" : ", title)[0]
    plain = {k for k in (normalize_key(main), normalize_key(title)) if k}
    with_volume = {k + volume.text for k in plain} if volume.text else set()
    return plain, with_volume


def _series_numbers(record: NdlRecord) -> list[tuple[str, str]]:
    result = []
    for series in record.series:
        m = _SERIES_NUMBER.match(nfkc(series))
        if m and m.group(1).strip():
            result.append((m.group(1), str(int(m.group(2)))))
    return result


def _strip_series_number(title: str, name: str, number: str) -> str | None:
    """Kindle の書名から「叢書名+叢書番号」(谷口ジローコレクション18)を除く。含まなければ None。"""
    chars = normalize_key(name)
    if not chars:
        return None
    pattern = r"\W*".join(re.escape(c) for c in chars) + rf"\W*0*{number}(?!\d)"
    m = re.search(pattern, nfkc(title), re.IGNORECASE)
    if not m:
        return None
    return (nfkc(title)[: m.start()] + " " + nfkc(title)[m.end() :]).strip()


def _kindle_forms(book: Book, record: NdlRecord) -> list[KindleTitle]:
    forms = [parse_kindle_title(book.title, book.series_title)]
    for name, number in _series_numbers(record):
        stripped = _strip_series_number(book.title, name, number)
        if stripped:
            # 叢書番号を巻数と取り違えないよう、叢書名と番号を除いた書名で巻数を取り直す
            forms.append(parse_kindle_title(stripped, book.series_title))
    return forms


def is_adoptable(book: Book, record: NdlRecord) -> bool:
    """採用条件: 紙版であること、同じ作品を指すこと、巻数がある本は巻数が一致すること。"""
    if not is_candidate(record):
        return False
    volume = _candidate_volume(record.volume)
    plain, with_volume = _title_variants(record, volume)
    for kindle in _kindle_forms(book, record):
        if not kindle.key:
            continue
        if kindle.key in plain:
            if kindle.volume is not None:
                if volume.key == kindle.volume:
                    return True
            elif not volume.has_value or volume.key == "1":
                return True
        if kindle.key in with_volume:
            # 巻の副題まで書名に含めて一致した(「星界の紋章 2―ささやかな戦い―」)
            if kindle.volume is None or volume.key == kindle.volume:
                return True
    return False


def _label_matches(label: str, record: NdlRecord) -> bool:
    label_key = normalize_key(label)
    for series in record.series:
        name = re.split(r"\s*[;.]\s", nfkc(series))[0]
        name_key = normalize_key(name)
        shorter, longer = sorted((label_key, name_key), key=len)
        # 2 文字以下の叢書名(FC など)は偶然の部分一致が多いので使わない
        if len(shorter) >= 3 and shorter in longer:
            return True
    return False


def _edition_matches(edition: str, record: NdlRecord) -> bool:
    # normalize_key は版表記を消すので使わない。使うと空文字列の包含になり、どの版とも一致してしまう
    wanted = _NON_WORD.sub("", nfkc(edition)).casefold()
    actual = _NON_WORD.sub("", nfkc(record.edition or "")).casefold()
    return bool(wanted) and wanted in actual


def _narrow(records: list[NdlRecord], predicate) -> list[NdlRecord]:
    # 一部だけが当てはまるときだけ絞る。どれも当てはまらなければ、手がかりにならないので全部残す
    kept = [r for r in records if predicate(r)]
    return kept if kept else records


def normalize_isbn(raw: str) -> str | None:
    """ISBN を 13 桁の数字列にする。10 桁は 978 を付けて検査数字を計算し直す。形が違えば None。"""
    digits = re.sub(r"[^0-9Xx]", "", nfkc(raw)).upper()
    if len(digits) == 13 and digits.isdigit():
        return digits
    if len(digits) == 10 and digits[:9].isdigit():
        core = "978" + digits[:9]
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return None


def isbn_checksum_ok(raw: str) -> bool:
    digits = re.sub(r"[^0-9Xx]", "", nfkc(raw)).upper()
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits[:12]))
        return (10 - total % 10) % 10 == int(digits[12])
    if len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] == "X"):
        total = sum((10 - i) * int(c) for i, c in enumerate(digits[:9]))
        check = (11 - total % 11) % 11
        return (digits[9] == "X" and check == 10) or (digits[9].isdigit() and check == int(digits[9]))
    return False


def normalize_issued(raw: str | None) -> str | None:
    """dcterms:issued(2015.4、[2020] など)を記載の精度のまま YYYY、YYYY-MM、YYYY-MM-DD にする。"""
    if not raw:
        return None
    m = re.fullmatch(r"\[?(\d{4})\]?(?:\.(\d{1,2})(?:\.(\d{1,2}))?)?\.?", nfkc(raw).strip())
    if not m:
        return None
    year, month, day = m.groups()
    if month is None:
        return year
    if not 1 <= int(month) <= 12:
        return None
    if day is None:
        return f"{year}-{int(month):02d}"
    if not 1 <= int(day) <= 31:
        return None
    return f"{year}-{int(month):02d}-{int(day):02d}"


def parse_pages(extent: str | None) -> int | None:
    """dc:extent(389p、xii, 245 pages など)の最後のページ数。ページ付がなければ None。"""
    if not extent:
        return None
    numbers = re.findall(r"(\d+)\s*(?:p\b|p$|pages?\b|ページ)", nfkc(extent))
    return int(numbers[-1]) if numbers else None


def clean_notes(descriptions: Iterable[str]) -> tuple[str, ...]:
    # 応答には、刊行年だけの値や「出版」「頒布」だけの値も dc:description として入る。注記ではないので除く
    notes = []
    for text in descriptions:
        value = text.strip()
        if not value or re.fullmatch(r"\d{4}", value) or value in ("出版", "頒布"):
            continue
        if value not in notes:
            notes.append(value)
    return tuple(notes)


_NDC_EDITION_ORDER = {"10": 0, "9": 1, "8": 2, "": 3}


def preferred_ndc(record: NdlRecord) -> tuple[str, str] | None:
    """(版, 記号)。1 件に複数あれば新しい版を採る。"""
    if not record.ndcs:
        return None
    return sorted(record.ndcs, key=lambda item: _NDC_EDITION_ORDER.get(item[0], 4))[0]


def _dedupe_by_isbn(records: list[NdlRecord]) -> list[NdlRecord]:
    # 同じ ISBN の書誌は同じ紙版なので 1 つにまとめる。ISBN のない書誌は別の紙版として残す
    seen: set[str] = set()
    result = []
    for record in records:
        isbns = {i for i in (normalize_isbn(x) for x in record.isbns) if i}
        if isbns & seen:
            continue
        seen |= isbns
        result.append(record)
    return result


def _common(values: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not values:
        return ()
    rest = [set(v) for v in values[1:]]
    return tuple(v for v in values[0] if all(v in other for other in rest))


def _work_attributes(records: list[NdlRecord]) -> dict[str, object]:
    ndcs = [preferred_ndc(r) for r in records]
    ndc = ndc_edition = None
    if all(ndcs) and len({code for _, code in ndcs}) == 1:
        # 版の違いは無視して記号で比べる。版がそろわなければ版は不明にする
        ndc = ndcs[0][1]
        editions = {edition for edition, _ in ndcs}
        ndc_edition = editions.pop() if len(editions) == 1 else None
    return {
        "ndc": ndc,
        "ndc_edition": ndc_edition or None,
        "subjects": _common([r.subjects for r in records]),
        "notes": _common([clean_notes(r.descriptions) for r in records]),
    }


def _edition_attributes(record: NdlRecord) -> dict[str, object]:
    isbns = [i for i in (normalize_isbn(x) for x in record.isbns) if i]
    return {
        "isbn": isbns[0] if isbns else None,
        "paper_issued": normalize_issued(record.issued),
        "publisher": ", ".join(record.publishers) or None,
        "pages": parse_pages(record.extent),
        "bib_series": record.series[0] if record.series else None,
    }


def _build_match(method: str, records: list[NdlRecord]) -> Match:
    attributes = _work_attributes(records)
    if len(records) == 1:
        attributes.update(_edition_attributes(records[0]))
    return Match(method=method, candidate_ids=tuple(r.id for r in records), **attributes)


def decide_match(book: Book, records: Iterable[NdlRecord]) -> Match | None:
    """書名検索の候補から照合する。採用条件を満たす候補がなければ None。"""
    adopted = _dedupe_by_isbn([r for r in records if is_adoptable(book, r)])
    if not adopted:
        return None
    kindle = parse_kindle_title(book.title, book.series_title)
    for label in kindle.labels:
        adopted = _narrow(adopted, lambda r, label=label: _label_matches(label, r))
    for edition in kindle.editions:
        adopted = _narrow(adopted, lambda r, edition=edition: _edition_matches(edition, r))
    return _build_match("edition" if len(adopted) == 1 else "work", adopted)


def decide_isbn_match(records: Iterable[NdlRecord]) -> Match | None:
    """手動訂正の ISBN で引いた候補から照合する。当たった紙版をそのまま採用する。"""
    adopted = _dedupe_by_isbn([r for r in records if is_candidate(r)])
    if not adopted:
        return None
    return _build_match("isbn", adopted)
