"""Tests for title normalization and matching against NDL candidates.

docs/bibinfo-plan.md の「照合の例」と、標本測定で見つかった落とし穴をケースにしている。
精度を優先する規則なので、「採用しない」ことの確認を「採用する」ことの確認と同じ重さで置く。
"""

from __future__ import annotations

import pytest

from kindb.matching import (
    Book,
    clean_notes,
    decide_isbn_match,
    decide_match,
    is_adoptable,
    isbn_checksum_ok,
    normalize_isbn,
    normalize_issued,
    parse_kindle_title,
    parse_pages,
    search_stages,
)
from tests.ndl_fixtures import record


def _book(title: str, authors: str = "著者", series_title: str | None = None) -> Book:
    return Book("B000000001", title, authors, series_title)


# --- 書名の分解 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "volume", "labels", "editions"),
    [
        ("理想のヒモ生活(25) (角川コミックス・エース)", "25", ("角川コミックス・エース",), ()),
        ("錬金術無人島サヴァイブ（２） (アフタヌーンコミックス)", "2", ("アフタヌーンコミックス",), ()),
        ("エマ 10巻 (HARTA COMIX)", "10", ("HARTA COMIX",), ()),
        ("HUNTER×HUNTER モノクロ版 26 (ジャンプコミックスDIGITAL)", "26", ("ジャンプコミックスDIGITAL",), ()),
        ("MOONLIGHT MILE【完全版】(1)", "1", (), ("完全版",)),
        ("新版　マーケティングの基本　この１冊ですべてわかる", None, (), ("新版",)),
        ("ゲームメカニクス大全 第2版 ボードゲームに学ぶ「おもしろさ」の仕掛け", None, (), ("第2版",)),
        ("ファスト＆スロー（上） (早川書房)", "上", ("早川書房",), ()),
        ("星界の紋章　２―ささやかな戦い―", "2", (), ()),
    ],
)
def test_parse_kindle_title_splits_volume_labels_and_editions(
    title: str, volume: str | None, labels: tuple[str, ...], editions: tuple[str, ...]
) -> None:
    parsed = parse_kindle_title(title)
    assert (parsed.volume, parsed.labels, parsed.editions) == (volume, labels, editions)


def test_parse_kindle_title_drops_trailing_series_name_and_reads_roman_volume() -> None:
    parsed = parse_kindle_title("星界の断章 Ⅰ 星界シリーズ (ハヤカワ文庫JA)", series_title="星界シリーズ")
    assert parsed.key == "星界の断章"
    assert parsed.volume == "1"


def test_parse_kindle_title_does_not_take_a_decimal_as_volume() -> None:
    parsed = parse_kindle_title("2.5次元の誘惑 セミカラー版 24 (ジャンプコミックスDIGITAL)")
    assert parsed.volume == "24"
    assert parsed.key == "25次元の誘惑"


def test_parse_kindle_title_keeps_bracketed_words_that_are_part_of_the_title() -> None:
    parsed = parse_kindle_title(
        "ハズレ枠の【状態異常スキル】で最強になった俺がすべてを蹂躙するまで 12 (オーバーラップ文庫)"
    )
    assert parsed.key == "ハズレ枠の状態異常スキルで最強になった俺がすべてを蹂躙するまで"


# --- 同じ作品かどうか ------------------------------------------------------------------------------------


def test_title_with_subtitle_matches_ndl_main_title_and_subtitle() -> None:
    book = _book("リーダブルコード より良いコードを書くためのシンプルで実践的なテクニック")
    candidate = record(title="リーダブルコード : より良いコードを書くためのシンプルで実践的なテクニック")
    assert is_adoptable(book, candidate)


def test_title_without_subtitle_matches_ndl_main_title() -> None:
    assert is_adoptable(_book("トヨタ生産方式"), record(title="トヨタ生産方式 : 脱規模の経営をめざして"))


def test_author_is_not_compared() -> None:
    # NDL は Kahneman, Daniel, Kindle はダニエル・カーネマン。比べると翻訳書が落ちる
    book = _book("ファスト＆スロー（上）", authors="ダニエル・カーネマン")
    candidate = record(title="ファスト&スロー : あなたの意思はどのように決まるか?", volume="上",
                       creators=("Kahneman, Daniel, 1934-2024",))
    assert is_adoptable(book, candidate)


@pytest.mark.parametrize(
    ("kindle_title", "ndl_title"),
    [
        # 検索語を書名の一部に含むだけの別作品(NDL の title は部分一致なので返ってくる)
        ("ファスト＆スロー", "ファスト&スローの経済学入門"),
        # 前方一致する別の本
        ("トヨタ生産方式", "トヨタ生産方式の原点 : かんばん方式の生みの親が「現場力」を語る"),
        # 副題の側だけが検索語を含む別の本
        ("トヨタ生産方式", "なぜ必要なものを、必要な分だけ : トヨタ生産方式から経営システムへ"),
    ],
)
def test_candidate_that_only_contains_the_title_is_another_work(kindle_title: str, ndl_title: str) -> None:
    book = _book(kindle_title)
    candidate = record(title=ndl_title)
    assert not is_adoptable(book, candidate)
    assert decide_match(book, [candidate]) is None


def test_kindle_subtitle_absent_from_ndl_is_not_matched() -> None:
    # 精度を優先し、Kindle にだけある副題を無視して一致とはみなさない
    book = _book("詳細！SwiftUI iPhoneアプリ開発 入門ノート　iOS 16+Xcode 14対応")
    assert not is_adoptable(book, record(title="詳細!SwiftUI iPhoneアプリ開発入門ノート"))


def test_reading_and_ruby_are_ignored_on_both_sides() -> None:
    book = _book("救世主《メシア》～異世界を救った元勇者が魔物のあふれる現実世界を無双する～ 7")
    candidate = record(title="救世主《メシア》 : 異世界を救った元勇者が魔物のあふれる現実世界を無双する", volume="7")
    assert is_adoptable(book, candidate)
    reading = record(title="とある科学の超電磁砲 (レールガン)", volume="7")
    assert is_adoptable(_book("とある科学の超電磁砲(7)"), reading)


# --- 巻数 --------------------------------------------------------------------------------------------------


def test_volume_must_match_when_kindle_title_has_one() -> None:
    book = _book("理想のヒモ生活(25) (角川コミックス・エース)")
    assert is_adoptable(book, record(title="理想のヒモ生活", volume="25"))
    assert not is_adoptable(book, record(title="理想のヒモ生活", volume="24"))
    assert not is_adoptable(book, record(title="理想のヒモ生活"))


@pytest.mark.parametrize("ndl_volume", ["no.26", "NO.26", "Vol. 26", "第26集", "26巻", "026"])
def test_volume_notation_variants_are_compared_by_number(ndl_volume: str) -> None:
    assert is_adoptable(_book("HUNTER×HUNTER 26"), record(title="Hunter×hunter", volume=ndl_volume))


def test_volume_range_is_not_a_single_volume() -> None:
    assert not is_adoptable(_book("エマ 2巻"), record(title="エマ", volume="1-3"))


def test_book_without_volume_matches_only_first_or_unnumbered_candidate() -> None:
    # 第 1 巻に巻表記がなく、続刊にだけ巻数がある(ブルーアーカイブ オフィシャルアートワークス)
    book = _book("ブルーアーカイブ オフィシャルアートワークス")
    assert is_adoptable(book, record(title="ブルーアーカイブオフィシャルアートワークス"))
    assert is_adoptable(book, record(title="ブルーアーカイブオフィシャルアートワークス", volume="1"))
    assert not is_adoptable(book, record(title="ブルーアーカイブオフィシャルアートワークス", volume="2"))
    assert not is_adoptable(book, record(title="体力おばけへの道", volume="超ハードモード編"))


def test_volume_subtitle_in_kindle_title_matches_ndl_volume_field() -> None:
    book = _book("星界の紋章　２―ささやかな戦い―")
    novel = record(title="星界の紋章", volume="2 (ささやかな戦い)", series=("ハヤカワ文庫. JA",))
    comic = record(title="星界の紋章", volume="2", series=("METEOR COMICS",))
    assert is_adoptable(book, novel)
    assert not is_adoptable(book, comic)


def test_series_number_is_not_taken_as_volume() -> None:
    # 「谷口ジローコレクション18」の 18 は叢書番号。seriesTitle の「; 18」と照らし、巻数としては使わない
    book = _book("谷口ジローコレクション18　孤独のグルメ２")
    right = record("R100000002-I032215772", "孤独のグルメ", volume="2", series=("谷口ジローコレクション ; 18",))
    wrong_volume = record("R100000002-I000000018", "孤独のグルメ", volume="18", series=("谷口ジローコレクション ; 99",))
    other_number = record("R100000002-I032152395", "孤独のグルメ", volume="2", series=("谷口ジローコレクション ; 17",))
    assert is_adoptable(book, right)
    assert not is_adoptable(book, wrong_volume)
    assert not is_adoptable(book, other_number)


# --- 紙版の候補 ------------------------------------------------------------------------------------------


def test_only_ndl_paper_books_are_candidates() -> None:
    book = _book("理想のヒモ生活(25)")
    jpro_ebook = record("R100000137-I000000001", "理想のヒモ生活", volume="25",
                        categories=("図書", "デジタル", "電子書籍・電子雑誌"))
    other_library = record("R100000136-I000000001", "理想のヒモ生活", volume="25")
    audio = record("R100000002-I000000002", "理想のヒモ生活", volume="25", categories=("録音資料", "記録メディア"))
    paper = record("R100000002-I034572316", "理想のヒモ生活", volume="25")
    for candidate in (jpro_ebook, other_library, audio):
        assert not is_adoptable(book, candidate)
    match = decide_match(book, [jpro_ebook, other_library, audio, paper])
    assert match is not None and match.candidate_ids == ("R100000002-I034572316",)


# --- 紙版の絞り込みと属性 --------------------------------------------------------------------------------


def _fast_and_slow(subjects_bunko: tuple[str, ...] = ("思考",)) -> list:
    hardcover = record("R100000002-I000000101", "ファスト&スロー : あなたの意思はどのように決まるか?", volume="上",
                       isbn="978-4-15-209368-5", ndc=("9", "141.5"), subjects=("思考",), issued="2012.11",
                       publishers=("早川書房",), descriptions=("原タイトル: Thinking, fast and slow", "2012"))
    bunko = record("R100000002-I000000102", "ファスト&スロー : あなたの意思はどのように決まるか?", volume="上",
                   series=("ハヤカワ文庫 ; NF 410",), isbn="978-4-15-050410-6", ndc=("10", "141.5"),
                   subjects=subjects_bunko, issued="2014.6", publishers=("早川書房",),
                   descriptions=("原タイトル: Thinking, fast and slow", "出版"))
    return [hardcover, bunko]


def test_several_paper_editions_keep_only_shared_work_attributes() -> None:
    match = decide_match(_book("ファスト＆スロー（上） あなたの意思はどのように決まるか？"), _fast_and_slow())
    assert match is not None
    assert match.method == "work"
    assert match.candidate_ids == ("R100000002-I000000101", "R100000002-I000000102")
    # NDC は版の違いを無視して記号で比べる。版がそろわないので版は不明
    assert (match.ndc, match.ndc_edition) == ("141.5", None)
    assert match.subjects == ("思考",)
    assert match.notes == ("原タイトル: Thinking, fast and slow",)
    assert (match.isbn, match.paper_issued, match.publisher, match.pages, match.bib_series) == (None,) * 5


def test_subjects_across_editions_are_intersected() -> None:
    match = decide_match(
        _book("ファスト＆スロー（上） あなたの意思はどのように決まるか？"), _fast_and_slow(("思考", "意思決定"))
    )
    assert match is not None and match.subjects == ("思考",)


def test_ndc_is_dropped_when_editions_disagree_or_one_lacks_it() -> None:
    book = _book("知的複眼思考法")
    a = record("R100000002-I1", "知的複眼思考法", ndc=("9", "002.7"))
    b = record("R100000002-I2", "知的複眼思考法", ndc=("10", "141.5"))
    c = record("R100000002-I3", "知的複眼思考法")
    assert decide_match(book, [a, b]).ndc is None
    assert decide_match(book, [a, c]).ndc is None


def test_label_narrows_to_the_edition_in_the_kindle_title() -> None:
    book = _book("つげ義春日記 (講談社文芸文庫)")
    hardcover = record("R100000002-I000001657059", "つげ義春日記", isbn="4-06-201085-6", issued="1983.12")
    bunko = record("R100000002-I030280980", "つげ義春日記", series=("講談社文芸文庫 ; つK1",),
                   isbn="978-4-06-519067-8", issued="2020.3", extent="374p", ndc=("10", "726.101"),
                   subjects=("つげ, 義春, 1937-2026",), publishers=("講談社",))
    match = decide_match(book, [hardcover, bunko])
    assert match is not None
    assert match.method == "edition"
    assert match.candidate_ids == ("R100000002-I030280980",)
    assert match.isbn == "9784065190678"
    assert match.paper_issued == "2020-03"
    assert match.pages == 374
    assert match.bib_series == "講談社文芸文庫 ; つK1"
    assert (match.ndc, match.ndc_edition) == ("726.101", "10")


def test_label_that_matches_no_candidate_does_not_narrow() -> None:
    book = _book("つげ義春日記 (文春e-book)")
    candidates = [
        record("R100000002-I1", "つげ義春日記", isbn="4-06-201085-6"),
        record("R100000002-I2", "つげ義春日記", series=("講談社文芸文庫 ; つK1",), isbn="978-4-06-519067-8"),
    ]
    assert decide_match(book, candidates).method == "work"


def test_edition_statement_narrows_to_that_edition() -> None:
    book = _book("新版　マーケティングの基本　この１冊ですべてわかる")
    old = record("R100000002-I000010061268", "マーケティングの基本 : この1冊ですべてわかる", isbn="978-4-534-04548-9")
    new = record("R100000002-I029096970", "マーケティングの基本 : この1冊ですべてわかる", edition="新版",
                 isbn="978-4-534-05609-6")
    match = decide_match(book, [old, new])
    assert match is not None and match.candidate_ids == ("R100000002-I029096970",)


def test_edition_statement_that_matches_no_candidate_does_not_pick_another_edition() -> None:
    # 【完全版】の紙版はなく、通常版と新装版だけが当たる。新装版を完全版として採用しない
    book = _book("MOONLIGHT MILE【完全版】(1)")
    regular = record("R100000002-I000009421014", "Moonlight mile", volume="1", isbn="4-09-186251-9")
    rerecord = record("R100000002-I024027369", "Moonlight mile", volume="rerecord 1", edition="新装版",
                      isbn="978-4-09-184812-3")
    match = decide_match(book, [regular, rerecord])
    assert match is not None
    assert match.method == "work"
    assert match.isbn is None


def test_same_isbn_records_are_one_paper_edition() -> None:
    book = _book("HARD THINGS　答えがない難問と困難にきみはどう立ち向かうか")
    a = record("R100000002-I026300125", "HARD THINGS : 答えがない難問と困難にきみはどう立ち向かうか",
               isbn="978-4-8222-5085-0")
    b = record("R100000002-I026300126", "HARD THINGS : 答えがない難問と困難にきみはどう立ち向かうか",
               isbn="9784822250850")
    match = decide_match(book, [a, b])
    assert match is not None and match.method == "edition"


def test_isbn_override_adopts_the_record_without_comparing_titles() -> None:
    candidate = record("R100000002-I1", "全く別の書名", isbn="978-4-8222-5085-0")
    match = decide_isbn_match([candidate])
    assert match is not None
    assert (match.method, match.isbn) == ("isbn", "9784822250850")
    assert decide_isbn_match([record("R100000136-I1", "全く別の書名")]) is None


# --- 検索の段 ------------------------------------------------------------------------------------------


def test_search_stages_go_from_narrow_to_broad_and_add_terms_when_over_limit() -> None:
    stages = search_stages(Book("B1", "理想のヒモ生活(25) (角川コミックス・エース)", "日月 ネコ", "理想のヒモ生活"))
    assert [s.name for s in stages] == ["title_creator", "title", "series_creator"]
    title_creator, title_only, series = stages
    assert title_creator.params == {"title": "理想のヒモ生活", "creator": "日月 ネコ"}
    assert title_creator.refinements == ({"title": "理想のヒモ生活 25", "creator": "日月 ネコ"},)
    assert title_only.params == {"title": "理想のヒモ生活"}
    # 足す順は著者、巻数の語。電子書籍は最初の検索から除いている
    assert title_only.refinements == (
        {"title": "理想のヒモ生活", "creator": "日月 ネコ"},
        {"title": "理想のヒモ生活 25", "creator": "日月 ネコ"},
    )
    assert series.params == {"title": "理想のヒモ生活", "creator": "日月 ネコ"}


def test_search_text_replaces_symbols_with_word_breaks() -> None:
    book = _book("スクラム　仕事が４倍速くなる“世界標準”のチーム戦術 (早川書房)", "ジェフ・サザーランド")
    stages = search_stages(book)
    assert stages[0].params == {
        "title": "スクラム 仕事が4倍速くなる 世界標準 のチーム戦術",
        "creator": "ジェフ サザーランド",
    }


# --- 値の正規化 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2015.4", "2015-04"), ("2015.12", "2015-12"), ("[2020]", "2020"), ("2015", "2015"), ("2015.", "2015"),
     ("2020.3.10", "2020-03-10"), ("昭和52", None), ("[2006]-", None), ("2015.13", None), (None, None)],
)
def test_normalize_issued_keeps_the_recorded_precision(raw: str | None, expected: str | None) -> None:
    assert normalize_issued(raw) == expected


@pytest.mark.parametrize(
    ("extent", "expected"),
    [("389p", 389), ("12, 389p", 389), ("xviii, 245 pages", 245), ("1冊(ページ付なし)", None), ("volumes", None)],
)
def test_parse_pages(extent: str, expected: int | None) -> None:
    assert parse_pages(extent) == expected


def test_normalize_isbn_converts_isbn10_and_hyphens() -> None:
    assert normalize_isbn("4-15-030552-8") == "9784150305529"
    assert normalize_isbn("978-4-8222-5085-0") == "9784822250850"
    assert normalize_isbn("12345") is None


def test_isbn_checksum() -> None:
    assert isbn_checksum_ok("978-4-8222-5085-0")
    assert isbn_checksum_ok("4-15-030552-8")
    assert isbn_checksum_ok("4-00-000008-X")
    assert not isbn_checksum_ok("4-00-000009-X")
    assert not isbn_checksum_ok("978-4-8222-5085-1")
    assert not isbn_checksum_ok("4-15-030552-9")


def test_clean_notes_drops_year_and_publication_markers() -> None:
    assert clean_notes(["原タイトル: SCRUM", " 2015", "出版", "頒布", "索引あり", "索引あり"]) == (
        "原タイトル: SCRUM",
        "索引あり",
    )
