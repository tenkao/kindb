"""Kindle の書名と NDL サーチの候補の照合。通信も DB も扱わない純粋関数だけを置く。

規則の理由は docs/bibinfo-requirements.md の「照合」と「検索結果の取得」にある。精度を優先し、
紙版を 1 つに定められなければ版の属性は付けず、同じ作品と確かめられない候補は採用しない。
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from kindb.ndl import NdlRecord

# NDL 自身の書誌の ID の接頭辞。件名を NDLSH として扱えるのはこの書誌だけ
NDL_RECORD_PREFIX = "R100000002-"

# 巻数として扱う語。「ファスト&スロー(上)」の「上」など
VOLUME_WORDS = ("上", "中", "下", "前編", "中編", "後編")
# 「上巻」は「上」と同じ巻として比べる。Kindle と NDL で片方だけが「巻」を付けることがある
_VOLUME_WORD_ALIASES = {"上巻": "上", "中巻": "中", "下巻": "下"}
# 試したが不可: Ⅰ〜Ⅻ を NFKC の前に数字へ置き換えると、NDL が英字で書く書名(ファイナルファンタジーVII)と
# 一致しなくなった。Ⅲ は NFKC のまま III にし、巻数としては巻数の形(_KINDLE_VOLUME_PATTERNS)で読む

# 紙版にない電子書籍だけの版名。長いものを先に置き、「フルカラー版」から「カラー版」だけを消さないようにする
_DIGITAL_ONLY = re.compile(
    r"(フルカラー版|セミカラー版|モノクロ版|カラー版|連載版|分冊版|単話版|合本版|コミックス版|"
    r"電子書籍版|電子特別版|電子限定版|電子版|デジタル版)"
)
# 【】の中身がこれを含めば、電子書籍の版名や宣伝として丸ごと除く。含まなければ括弧だけ外す(【状態異常スキル】など)
_BRACKET_LABELLIKE = re.compile(r"(版|連載|分冊|単話|特典|期間限定|合本|カラー|コミックス|電子|デジタル|話)")
_NUMERAL = r"[\d〇一二三四五六七八九十]+"
# 版表記。書名の語、【】の中、末尾の括弧のどこにあっても版表記として記録する
_EDITION_TOKEN = re.compile(
    rf"^(?:新|改訂|増補|新装|完全|愛蔵|豪華|決定|普及|新訂|特装|限定|復刻|ワイド|改訂新|増補改訂|増補新|改訂増補"
    rf"|第{_NUMERAL}|改訂第{_NUMERAL}|\d{{4}}年)版$"
)
# 版表記がなくても、これらは特別な版で、通常の Kindle 版の元になった紙版ではない
_SPECIAL_EDITION = re.compile(r"(愛蔵|新装|完全|豪華|特装|限定|復刻|ワイド)版")
# 分冊版、単話、合本の番号は紙版の巻数と対応しない(標本測定の落とし穴 9)。こうした本は紙版に照合しない
_SPLIT_EDITION = re.compile(rf"(分冊|単話|合本|第\s*{_NUMERAL}\s*話)")
# 括弧の中が数字でできているのに 1 つの巻として読めないもの(1.5、弐、其の二、第三話)。レーベルとして外すと
# 巻数のない本として 1 巻を採るので、どの巻とも一致しない巻数として扱う
_VOLUME_LIKE = re.compile(
    r"^(?:第|其の|その|巻)?\s*[\d.〇一二三四五六七八九十百壱弐参肆伍陸漆捌玖拾]+\s*(?:巻|集|部|話|の巻|号)?$"
)
_UNREADABLE_VOLUME = "?"
# 空になった括弧(「（４）〈電子特別版〉」から版名を除いたあとの「〈〉」)
_EMPTY_BRACKETS = re.compile(r"[〈《(\[【〔「]\s*[〉》)\]】〕」]")
# NDL の副題がこれなら、本タイトルだけの一致を認めない(小説版やガイドブックなど、同じ書名の別の作品)
_DERIVED_WORK_SUBTITLES = {
    "小説", "ノベライズ", "外伝", "外譚集", "公式ガイド", "公式ガイドブック", "公式ファンブック", "ファンブック",
    "画集", "設定資料集", "アニメーションガイド", "コミック", "コミック版", "コミカライズ", "総集編",
}
# レーベルから叢書名を除いた残りがこれを含めば、同じ叢書とみなさない(ハヤカワ・ミステリ と ハヤカワ・ミステリ文庫)
_OTHER_IMPRINT = re.compile(r"(文庫|新書|選書|スペシャル|special|ワイド|wide|愛蔵)")
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")
_ROMAN = {
    "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6", "VII": "7", "VIII": "8", "IX": "9", "X": "10"
}
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_NUMBER = "[〇一二三四五六七八九十]+"
_ENCLOSED = r"[―—‐~～〜-][^―—‐~～〜-]+[―—‐~～〜-]"
_VOLUME_WORD = "|".join(sorted([*VOLUME_WORDS, *_VOLUME_WORD_ALIASES], key=len, reverse=True))
# 括弧の中や候補の巻に書かれる、1 つの巻を表す形(3、三、第2巻、Vol.2、その2、no.26、第1集)
_VOLUME_TOKEN = re.compile(
    rf"^(?:(?:第|no\.?|vol\.?|v\.|volume|part|その)\s*)?(\d+|{_KANJI_NUMBER})\s*(?:巻|集|号)?$", re.IGNORECASE
)
# 書名の末尾から巻数を探す順。見つかった最初の 1 つを使う。2 つ目の値は巻数の書き方の種類で、
# "bare"(括弧のない末尾の数字やローマ数字)だけは、書名の一部と読む解釈も残す(「ジ・アート・オブ Fallout 4」)
_KINDLE_VOLUME_PATTERNS = (
    (re.compile(r"\(([^()]*)\)$"), "paren"),
    (re.compile(r"\[([^\[\]]*)\]$"), "paren"),
    (re.compile(rf"((?:第\s*)?(?:\d+|{_KANJI_NUMBER})\s*巻)$"), "word"),
    (re.compile(rf"\s({_VOLUME_WORD})$"), "word"),
    (re.compile(r"\s(\d+)$"), "bare"),
    # 「星界の紋章 2―ささやかな戦い―」「星界の戦旗Ⅲ ―家族の食卓―」のように、巻数のあとに巻の副題が続く書名
    (re.compile(rf"(?<=[^\d.])(\d+)\s*(?={_ENCLOSED}$)"), "subtitle"),
    (re.compile(rf"(?<=[^\sA-Za-z])(I|II|III|IV|V|VI|VII|VIII|IX|X)\s*(?={_ENCLOSED}$)"), "subtitle"),
    (re.compile(r"\s(I|II|III|IV|V|VI|VII|VIII|IX|X)$"), "bare"),
    (re.compile(rf"\s({_KANJI_NUMBER})$"), "bare"),
    # 「…PART2」のように文字の直後に付いた数字。小数(2.5)の一部は巻数にしない
    (re.compile(r"(?<=[^\d\s.])(\d+)$"), "bare"),
    # 「ファスト＆スロー（上） あなたの意思は…」「…（４）〈電子特別版〉」のように、巻数のあとに書名が続くもの。
    # 末尾の形より後に試す
    (re.compile(r"\(([^()]*)\)"), "paren"),
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
    # 末尾の数字を巻数と読まず、書名の一部と読んだときの key。括弧のない末尾の数字だけに作る
    whole_key: str | None = None
    # 巻数のあとの巻の副題を除いた key(「災悪のアヴァロン 3 ～…～」の「災悪のアヴァロン」)
    key_without_volume_subtitle: str | None = None
    # 分冊版、単話、合本。紙版の巻と対応しないので照合しない
    split_edition: bool = False


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


def _drop_reading(m: re.Match[str]) -> str:
    # 巻数として読める括弧は残す。消すと、読めなかった巻数の本が巻数のない本として 1 巻と一致する
    inner = m.group(0)[1:-1]
    return f" {inner} " if m.group(0).startswith("(") and parse_volume_token(inner) is not None else " "


def normalize_key(text: str) -> str:
    """書名を比較用の文字列にする。Kindle の書名にも候補の書名にも同じ規則を当てる。

    長音符「ー」も除く。記号の「～」を除くので、NDL が「ぬーべー」、Kindle が「ぬ～べ～」と書く書名を同じにするため。
    """
    text = nfkc(text)
    # 読みや振り仮名は片方にだけ付くことが多い(「とある科学の超電磁砲 (レールガン)」「救世主《メシア》」)
    text = _READING.sub(_drop_reading, text)
    text = _DIGITAL_ONLY.sub(" ", text)
    text = " ".join(token for token in text.split() if not _EDITION_TOKEN.match(token))
    return _NON_WORD.sub("", text).replace("ー", "").casefold()


def _plain_key(text: str) -> str:
    """記号と空白と長音符だけを除いた比較用の文字列。normalize_key と違い、括弧の中身(巻数など)を残す。"""
    return _NON_WORD.sub("", nfkc(text)).replace("ー", "").casefold()


def _kanji_number(text: str) -> int | None:
    if not text or any(c not in _KANJI_DIGITS and c != "十" for c in text):
        return None
    if "十" in text:
        tens, _, ones = text.partition("十")
        if len(tens) > 1 or len(ones) > 1 or "十" in ones:
            return None
        return (_KANJI_DIGITS[tens] if tens else 1) * 10 + (_KANJI_DIGITS[ones] if ones else 0)
    return int("".join(str(_KANJI_DIGITS[c]) for c in text))


def parse_volume_token(text: str) -> str | None:
    """1 つの巻を表す語(3、三、Ⅲ、第2巻、Vol.2、上巻、下)を、比べる形("3"、"上")にする。巻でなければ None。"""
    token = nfkc(text).strip()
    word = _VOLUME_WORD_ALIASES.get(token.replace(" ", ""), token.replace(" ", ""))
    if word in VOLUME_WORDS:
        return word
    if token.upper() in _ROMAN:
        return _ROMAN[token.upper()]
    m = _VOLUME_TOKEN.match(token)
    if not m:
        return None
    number = int(m.group(1)) if m.group(1).isdigit() else _kanji_number(m.group(1))
    return str(number) if number is not None else None


def parse_kindle_title(
    title: str, series_title: str | None = None, trailing_names: tuple[str, ...] = ()
) -> KindleTitle:
    """trailing_names は、書名の末尾に付いていれば外す叢書名(候補の seriesTitle から渡す)。"""
    text = nfkc(title).strip()
    split_edition = bool(_SPLIT_EDITION.search(text))

    labels: list[str] = []
    editions: list[str] = []
    # 末尾の (…) はレーベル名として外す。中身が巻数(三、第2巻、下巻など)や巻数らしいもの(1.5)なら残し、
    # 版表記(新装版)なら版表記として記録する
    while True:
        m = _TRAILING_PAREN.search(text)
        inner = m.group(1).strip() if m else ""
        if not m or parse_volume_token(inner) is not None or _VOLUME_LIKE.match(inner):
            break
        if _EDITION_TOKEN.match(inner):
            editions.append(inner)
        elif inner:
            labels.append(inner)
        text = text[: m.start()].rstrip()

    def bracket(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if _EDITION_TOKEN.match(inner):
            editions.append(inner)
            return " "
        if _BRACKET_LABELLIKE.search(inner):
            return " "
        return inner

    text = re.sub(r"【([^】]*)】", bracket, text)
    text = _EMPTY_BRACKETS.sub(" ", _DIGITAL_ONLY.sub("", text))
    tokens = []
    for token in text.split():
        if _EDITION_TOKEN.match(token):
            editions.append(token)
        else:
            tokens.append(token)
    text = " ".join(tokens)

    # Kindle の書名の末尾に付いたシリーズ名(「星界の断章 Ⅰ 星界シリーズ」)や叢書名を外す。
    # 括弧の中身を残して比べ、「…とある科学の超電磁砲(7)」を巻数ごと外さないようにする
    for name in (series_title, *trailing_names):
        name_key = _plain_key(name) if name else ""
        words = text.split()
        stripped = next(
            (" ".join(words[:i]) for i in range(1, len(words)) if _plain_key(" ".join(words[i:])) == name_key),
            None,
        )
        if name_key and stripped:
            text = stripped
            break

    volume = whole_key = key_without_volume_subtitle = None
    for pattern, kind in _KINDLE_VOLUME_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        token = parse_volume_token(m.group(1))
        if token is None and kind == "paren" and _VOLUME_LIKE.match(m.group(1).strip()):
            token = _UNREADABLE_VOLUME
        if token is None:
            continue
        volume = token
        if kind == "bare":
            whole_key = normalize_key(text)
        text = (text[: m.start()] + " " + text[m.end() :]).strip()
        if kind == "subtitle":
            key_without_volume_subtitle = normalize_key(re.sub(rf"\s*{_ENCLOSED}$", "", text))
        break

    return KindleTitle(
        key=normalize_key(text),
        volume=volume,
        labels=tuple(labels),
        editions=tuple(editions),
        search_text=_search_text(text),
        whole_key=whole_key,
        key_without_volume_subtitle=key_without_volume_subtitle,
        split_edition=split_edition,
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
    if kindle.split_edition:
        return []
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
    """候補の巻。巻の数字として比べるのは、値全体が 1 つの巻を表すとき(3、no.26、Vol. 1、第1集、上巻)だけ。

    11.5、別巻2、第2部 1、1-3、1・2、rerecord 1 のような値は、どの巻数とも一致させない。
    後ろの括弧は巻の副題(「2 (ささやかな戦い)」)として書名との比較に使う。
    """
    if not volume or not volume.strip():
        return _CandidateVolume(None, "", False)
    text = nfkc(volume).strip()
    m = re.match(r"^(.*?)\s*\((.*)\)$", text)
    head, subtitle = (m.group(1), m.group(2)) if m and m.group(1) else (text, "")
    key = parse_volume_token(head)
    if key is not None:
        return _CandidateVolume(key, normalize_key(subtitle.replace("(", " ").replace(")", " ")), True)
    return _CandidateVolume(None, normalize_key(text.replace("(", " ").replace(")", " ")), True)


# 比べる副題と同じ normalize_key で作る。記号だけを除くと、長音符を除いた副題(アニメションガイド)と一致しない
_DERIVED_WORK_KEYS = frozenset(normalize_key(w) for w in _DERIVED_WORK_SUBTITLES)
# 書名を語に分ける区切り。特別な版の語(「蟲師 : 愛蔵版」「蟲師 (愛蔵版)」)を、語の一部(完全版マニュアル)と区別する
_TITLE_TOKEN_SEPARATOR = re.compile(r"[\s:()\[\]〈〉【】]+")


def _is_derived_work_subtitle(subtitle: str) -> bool:
    # 「小説版」「コミカライズ版」のように「版」を付けた形も同じ別の作品として扱う
    key = normalize_key(subtitle)
    return key in _DERIVED_WORK_KEYS or key.removesuffix("版") in _DERIVED_WORK_KEYS


def _is_special_edition(record: NdlRecord) -> bool:
    """愛蔵版や新装版などの特別な版か。版表示だけでなく書名も見る。

    NDL は版を書名の副題に書くことがあり(「ぼくらの : 完全版」)、normalize_key は版表記も読みの括弧も消すので、
    書名の比較では通常の版と区別できない。
    """
    if _SPECIAL_EDITION.search(nfkc(record.edition or "")):
        return True
    return any(_SPECIAL_EDITION.fullmatch(token) for token in _TITLE_TOKEN_SEPARATOR.split(nfkc(record.title)))


def _record_editions(record: NdlRecord) -> list[str]:
    """候補の版表記。版表示があればそれだけを使い、空なら書名の語から拾う(「蟲師 : 愛蔵版」)。"""
    if (record.edition or "").strip():
        return [record.edition]
    return [token for token in _TITLE_TOKEN_SEPARATOR.split(nfkc(record.title)) if _EDITION_TOKEN.match(token)]


def _title_parts(record: NdlRecord) -> list[str]:
    title = nfkc(record.title)
    # 並列タイトル(「X = Y : Z」の Y)は書名の比較に使わない。後ろの「 : 」は残す
    title = re.sub(r" = [^:]*?(?= : |$)", "", title)
    return title.split(" : ")


# 副題の数が多い書誌で並べ替えの数が増えすぎないための上限。実データで並べ替えが要った書誌は 3 部まで
_MAX_REORDERED_PARTS = 4


def _title_variants(record: NdlRecord, volume: _CandidateVolume) -> tuple[set[str], set[str]]:
    """候補の書名の比較用の形。(本タイトルか、本タイトルに副題を前から順に足したもの, それに巻の副題を足したもの)。

    NDL の書名は「本タイトル : 副題 : 副題」と副題を 2 つ以上持つことがあり、Kindle は最初の副題までを書名に
    入れることが多い(「線一本からはじめる伝わる絵の描き方 : ロジカルデッサンの技法 : まったく新しい…」)。
    """
    parts = _title_parts(record)
    first = 1 if len(parts) > 1 and _is_derived_work_subtitle(parts[1]) else 0
    plain = {k for k in (normalize_key(" : ".join(parts[: i + 1])) for i in range(first, len(parts))) if k}
    # Kindle は副題を本タイトルの前に置くことがある(NDL「継続する技術 : 200万人の…」、Kindle「200万人の… 継続する
    # 技術」)。先頭から続く部分(本タイトルを必ず含み、途中を飛ばさない)を並べ替えた形も、全体の完全一致でだけ比べる
    if 2 <= len(parts) <= _MAX_REORDERED_PARTS:
        for n in range(2, len(parts) + 1):
            for order in itertools.permutations(parts[:n]):
                if (key := normalize_key(" : ".join(order))):
                    plain.add(key)
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
    # normalize_key は記号と長音符を除くので、名前の文字の間に記号や長音符があってもよいとする
    pattern = r"[\Wー]*".join(re.escape(c) for c in chars) + rf"[\Wー]*0*{number}(?!\d)"
    m = re.search(pattern, nfkc(title), re.IGNORECASE)
    if not m:
        return None
    return (nfkc(title)[: m.start()] + " " + nfkc(title)[m.end() :]).strip()


def _series_names(record: NdlRecord) -> tuple[str, ...]:
    names = []
    for series in record.series:
        name = re.split(r"\s*[;.]\s", nfkc(series))[0].strip()
        if name:
            names.append(name)
    return tuple(names)


def _kindle_forms(book: Book, record: NdlRecord) -> list[KindleTitle]:
    forms = [parse_kindle_title(book.title, book.series_title)]
    for name, number in _series_numbers(record):
        stripped = _strip_series_number(book.title, name, number)
        if stripped:
            # 叢書番号を巻数と取り違えないよう、叢書名と番号を除いた書名で巻数を取り直す
            forms.append(parse_kindle_title(stripped, book.series_title))
    names = _series_names(record)
    if names:
        # 書名の末尾に括弧なしで付いた叢書名(「SQL 第2版 … プログラミング学習シリーズ」)は、候補がその叢書に
        # 入っているときだけ外す
        forms.append(parse_kindle_title(book.title, book.series_title, trailing_names=names))
    return forms


def is_adoptable(book: Book, record: NdlRecord) -> bool:
    """採用条件: 紙版であること、同じ作品を指すこと、巻数がある本は巻数が一致すること。"""
    if not is_candidate(record):
        return False
    volume = _candidate_volume(record.volume)
    plain, with_volume = _title_variants(record, volume)
    for kindle in _kindle_forms(book, record):
        if not kindle.key or kindle.split_edition:
            continue
        if kindle.key in plain:
            if kindle.volume is not None:
                if volume.key == kindle.volume:
                    return True
            elif not volume.has_value or (volume.key == "1" and not _has_numeral_label(kindle)):
                # 巻数のない本は、巻のない候補か 1 巻を採る。ただしレーベルとして外した括弧に数字があれば、
                # 読めなかった巻数かもしれないので 1 巻は採らない
                return True
        if kindle.key in with_volume:
            # 巻の副題まで書名に含めて一致した(「星界の紋章 2―ささやかな戦い―」)
            if kindle.volume is None or volume.key == kindle.volume:
                return True
    return False


def _has_numeral_label(kindle: KindleTitle) -> bool:
    return any(re.search(r"\d", label) for label in kindle.labels)


def _is_adoptable_on_second_reading(book: Book, record: NdlRecord) -> bool:
    """末尾の数字を巻数と読まず、書名の一部と読んで比べる(「ジ・アート・オブ Fallout 4」)。

    どの候補も is_adoptable を満たさないときだけ使う。後回しにするのは、「X 2」の 2 巻がある本で、書名が「X2」の
    別の本を採らないため。巻のない候補だけを採り、「X2」の 1 巻(続編の 1 巻)は採らない。

    試したが不可: 巻数のあとの巻の副題(「災悪のアヴァロン 3 ～…～」)を NDL が持たないものとして外す読み方。
    実データで 1 冊救えたが、小説の巻がまだ NDL にないと、副題のない同名のコミカライズの同じ巻を採ってしまう。
    """
    if not is_candidate(record):
        return False
    volume = _candidate_volume(record.volume)
    plain, _ = _title_variants(record, volume)
    return any(
        not kindle.split_edition and kindle.whole_key and kindle.whole_key in plain and not volume.has_value
        for kindle in _kindle_forms(book, record)
    )


def adoptable_records(book: Book, records: Iterable[NdlRecord]) -> list[NdlRecord]:
    """採用条件を満たす候補。1 件もなければ、書名の読み方を変えて探し直す(_is_adoptable_on_second_reading)。"""
    records = list(records)
    adopted = [r for r in records if is_adoptable(book, r)]
    if adopted:
        return adopted
    return [r for r in records if _is_adoptable_on_second_reading(book, r)]


def _label_matches(label: str, record: NdlRecord) -> bool:
    """候補の叢書名が Kindle のレーベルに含まれるか(「ハヤカワ文庫JA」に「ハヤカワ文庫」)。

    逆向き(レーベルが叢書名に含まれる)は数えない。レーベルが出版社名(講談社)だと、同じ出版社の文庫
    (講談社文芸文庫)に絞ってしまうため。2 文字以下の叢書名(FC など)は偶然の一致が多いので使わない。
    """
    label_key = normalize_key(label)
    for name in _series_names(record):
        name_key = normalize_key(name)
        if len(name_key) >= 3 and name_key in label_key:
            # 残りが文庫や新書なら別の叢書(ハヤカワ・ミステリ と ハヤカワ・ミステリ文庫)
            if not _OTHER_IMPRINT.search(label_key.replace(name_key, "", 1)):
                return True
    return False


def _edition_matches(edition: str, record: NdlRecord) -> bool:
    # normalize_key は版表記を消すので使わない。使うと空文字列の包含になり、どの版とも一致してしまう。
    # 包含でなく一致で比べる。包含だと「新版」が「改訂新版」と一致し、あとの版を選んでしまう
    wanted = _NON_WORD.sub("", nfkc(edition)).casefold()
    return bool(wanted) and any(wanted == _NON_WORD.sub("", nfkc(a)).casefold() for a in _record_editions(record))


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
    m = re.fullmatch(r"\[?(\d{4})(?:\.(\d{1,2})(?:\.(\d{1,2}))?)?\.?\]?\.?", nfkc(raw).strip())
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
    """dc:extent のページ数。「209, 13p」のように単位が最後にだけ付く並びでは、最大の数(本文)を採る。

    大きさ(; 19cm)と図版以降は見ない。ページ付がなければ None。
    """
    if not extent:
        return None
    text = nfkc(extent)
    if "ページ付なし" in text:
        return None
    text = re.split(r"図版|;|\+", text)[0]
    if not re.search(r"\d\s*(?:p\b|p$|pages?\b|ページ)", text):
        return None
    return max(int(n) for n in re.findall(r"\d+", text))


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


def _build_match(method: str, records: list[NdlRecord], *, edition_known: bool = True) -> Match:
    attributes = _work_attributes(records)
    if len(records) == 1 and edition_known:
        attributes.update(_edition_attributes(records[0]))
    return Match(method=method, candidate_ids=tuple(r.id for r in records), **attributes)


def decide_match(book: Book, records: Iterable[NdlRecord]) -> Match | None:
    """書名検索の候補から照合する。採用条件を満たす候補がなければ None。"""
    adopted = _dedupe_by_isbn(adoptable_records(book, records))
    if not adopted:
        return None
    kindle = parse_kindle_title(book.title, book.series_title)
    before_labels = len(adopted)
    for label in kindle.labels:
        adopted = _narrow(adopted, lambda r, label=label: _label_matches(label, r))
    # 版表記(新版、第2版、完全版)が候補のどれにも合わなければ、残った候補は別の版なので版の属性を付けない
    edition_known = True
    if not kindle.editions and len(adopted) < before_labels and all(_record_editions(r) for r in adopted):
        # 版表記のない Kindle 書名で、レーベルで絞った残りが新版や改訂版だけなら、版を定めない。NDL の叢書名の
        # 書き方は版ごとに違うことがあり(旧版「早川文庫」、新版「ハヤカワ文庫 JA」)、旧版だけが外れうるため
        edition_known = False
    for edition in kindle.editions:
        kept = [r for r in adopted if _edition_matches(edition, r)]
        if kept:
            adopted = kept
        else:
            edition_known = False
    if not kindle.editions and all(_is_special_edition(r) for r in adopted):
        # Kindle 書名に版表記がないのに、残ったのが愛蔵版や新装版だけなら、通常の版の紙版が NDL の結果にない
        edition_known = False
    single = len(adopted) == 1 and edition_known
    return _build_match("edition" if single else "work", adopted, edition_known=edition_known)


def decide_isbn_match(records: Iterable[NdlRecord]) -> Match | None:
    """手動訂正の ISBN で引いた候補から照合する。当たった紙版をそのまま採用する。"""
    adopted = _dedupe_by_isbn([r for r in records if is_candidate(r)])
    if not adopted:
        return None
    return _build_match("isbn", adopted)
