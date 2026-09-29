"""Tests for title normalization and matching against NDL candidates.

docs/bibinfo-plan.md の「照合の例」と、標本測定で見つかった落とし穴をケースにしている。
精度を優先する規則なので、「採用しない」ことの確認を「採用する」ことの確認と同じ重さで置く。
"""

from __future__ import annotations

import pytest

from kindb.matching import (
    Book,
    _label_matches,
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
     ("2020.3.10", "2020-03-10"), ("[2020.3]", "2020-03"), ("昭和52", None), ("[2006]-", None), ("2015.13", None),
     (None, None)],
)
def test_normalize_issued_keeps_the_recorded_precision(raw: str | None, expected: str | None) -> None:
    assert normalize_issued(raw) == expected


@pytest.mark.parametrize(
    ("extent", "expected"),
    [("389p", 389), ("12, 389p", 389), ("209, 13p", 209), ("xii, 345, 21p", 345), ("345p 図版16p", 345),
     ("389p ; 19cm", 389), ("xviii, 245 pages", 245), ("1冊(ページ付なし)", None), ("volumes", None), ("2冊", None)],
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


# --- 実データ 50 冊で取りこぼした書名の形 ---------------------------------------------------------------


def test_unicode_roman_numeral_before_volume_subtitle_is_the_volume() -> None:
    # NFKC は Ⅲ を III に変えるので、そのままでは巻数が書名に残り、検索も 0 件になっていた
    book = _book("星界の戦旗Ⅲ　―家族の食卓―")
    parsed = parse_kindle_title(book.title)
    assert (parsed.volume, parsed.key, parsed.search_text) == ("3", "星界の戦旗家族の食卓", "星界の戦旗 家族の食卓")
    assert is_adoptable(book, record(title="星界の戦旗", volume="3 (家族の食卓)"))
    assert not is_adoptable(book, record(title="星界の戦旗", volume="4 (軋む時空)"))


def test_volume_words_with_maki_match_either_notation() -> None:
    assert is_adoptable(_book("銃・病原菌・鉄　上巻"), record(title="銃・病原菌・鉄", volume="上"))
    assert is_adoptable(_book("銃・病原菌・鉄（上）"), record(title="銃・病原菌・鉄", volume="上巻"))
    assert not is_adoptable(_book("銃・病原菌・鉄　上巻"), record(title="銃・病原菌・鉄", volume="下巻"))


def test_kindle_title_matches_ndl_title_up_to_its_first_subtitle() -> None:
    # NDL の書名が副題を 2 つ持ち、Kindle は最初の副題までを書名に入れている
    book = _book("線一本からはじめる伝わる絵の描き方　ロジカルデッサンの技法")
    candidate = record(
        title="線一本からはじめる伝わる絵の描き方 : ロジカルデッサンの技法 : まったく新しいデッサンの教科書"
    )
    assert is_adoptable(book, candidate)
    # 副題の途中で切れた形とは一致させない
    assert not is_adoptable(_book("線一本からはじめる伝わる絵の描き方　ロジカルデッサン"), candidate)


def test_trailing_series_name_is_removed_only_for_candidates_in_that_series() -> None:
    book = _book("SQL 第2版 ゼロからはじめるデータベース操作 プログラミング学習シリーズ")
    in_series = record("R100000002-I027342611", "SQL : ゼロからはじめるデータベース操作",
                       series=("プログラミング学習シリーズ",), edition="第2版", isbn="978-4-7981-4445-0")
    first_edition = record("R100000002-I000010912604", "SQL : ゼロからはじめるデータベース操作",
                           series=("プログラミング学習シリーズ",), isbn="978-4-7981-2291-5")
    not_in_series = record("R100000002-I000000009", "SQL : ゼロからはじめるデータベース操作")
    assert is_adoptable(book, in_series)
    assert not is_adoptable(book, not_in_series)
    match = decide_match(book, [in_series, first_edition, not_in_series])
    assert match is not None and match.candidate_ids == ("R100000002-I027342611",)


def test_trailing_number_is_read_as_part_of_the_title_only_when_no_volume_matches() -> None:
    fallout = record("R100000002-I029478804", "ジ・アート・オブFallout 4")
    match = decide_match(_book("ジ・アート・オブ Fallout 4 (G-NOVELS)"), [fallout])
    assert match is not None and match.candidate_ids == ("R100000002-I029478804",)
    # 巻数と読んで一致する候補があれば、書名が「X2」の別の本は採らない
    volume_2 = record("R100000002-I000000002", "ドラゴンの本", volume="2")
    titled_2 = record("R100000002-I000000102", "ドラゴンの本2")
    match = decide_match(_book("ドラゴンの本 2"), [volume_2, titled_2])
    assert match is not None and match.candidate_ids == ("R100000002-I000000002",)


def test_volume_subtitle_absent_from_ndl_is_not_matched() -> None:
    # NDL が巻の副題を持たない本(災悪のアヴァロン 3)は採らない。小説の巻がまだ NDL にないと、副題のない同名の
    # コミカライズの同じ巻を採ってしまうため
    book = _book("【電子版限定特典付き】災悪のアヴァロン 3 ～悪役デブだった俺、クラス対抗戦で影に徹していたら、"
                 "なぜか伝説のラスボスとガチバトルになった件～ (ＨＪノベルス)")
    comic = record("R100000002-I1", "災悪のアヴァロン", volume="3", series=("ヤングジャンプコミックス",))
    assert decide_match(book, [comic]) is None
    # 巻の副題まで一致する紙版があれば採る。副題のない同名の本は採らない(「星界の紋章」のコミカライズ)
    seikai = _book("星界の紋章　２―ささやかな戦い―")
    novel_2 = record("R100000002-I000002498252", "星界の紋章", volume="2 (ささやかな戦い)")
    comic_2 = record("R100000002-I025412419", "星界の紋章", volume="2", series=("METEOR COMICS",))
    match = decide_match(seikai, [novel_2, comic_2])
    assert match is not None and match.candidate_ids == ("R100000002-I000002498252",)


# --- レビューで見つかった誤照合の形 ---------------------------------------------------------------------


def _volumes(title: str, count: int = 3, **kwargs: object) -> list:
    return [record(f"R100000002-I00000010{n}", title, volume=str(n), **kwargs) for n in range(1, count + 1)]


@pytest.mark.parametrize(
    "kindle_title",
    ["竜馬がゆく（三） (文春文庫)", "竜馬がゆく (Ⅲ)", "竜馬がゆく (3巻)", "竜馬がゆく (第3巻)", "竜馬がゆく (Vol.3)",
     "竜馬がゆく (その3)", "竜馬がゆく 第3巻", "竜馬がゆく 三"],
)
def test_volume_notations_in_and_out_of_parentheses_pick_that_volume(kindle_title: str) -> None:
    # 括弧の中の巻数をレーベルとして外すと、巻数のない本として 1 巻を採ってしまう
    match = decide_match(_book(kindle_title), _volumes("竜馬がゆく"))
    assert match is not None and match.candidate_ids == ("R100000002-I000000103",)


def test_volume_word_in_parentheses_with_maki() -> None:
    candidates = [
        record("R100000002-I1", "銃・病原菌・鉄", volume="上巻"),
        record("R100000002-I2", "銃・病原菌・鉄", volume="下巻"),
    ]
    match = decide_match(_book("銃・病原菌・鉄 (下巻)"), candidates)
    assert match is not None and match.candidate_ids == ("R100000002-I2",)


def test_unparsed_numeral_in_a_label_blocks_volume_1() -> None:
    # 読めなかった巻数かもしれない数字が括弧にあれば、巻数のない本として 1 巻を採らない
    candidates = _volumes("竜馬がゆく")
    assert decide_match(_book("竜馬がゆく (2021年新装)"), candidates) is None
    assert decide_match(_book("竜馬がゆく (文春文庫)"), candidates).candidate_ids == ("R100000002-I000000101",)


@pytest.mark.parametrize(
    "kindle_title",
    [
        "理想のヒモ生活【分冊版】　12",
        "理想のヒモ生活【単話版】(12)",
        "理想のヒモ生活 【第12話】",
        "【極！合本シリーズ】 理想のヒモ生活12巻",
        "理想のヒモ生活 全3冊合本版",
    ],
)
def test_split_episode_and_omnibus_editions_are_not_matched_to_paper_volumes(kindle_title: str) -> None:
    book = _book(kindle_title)
    assert parse_kindle_title(kindle_title).split_edition
    assert search_stages(book) == []
    assert decide_match(book, _volumes("理想のヒモ生活", count=12)) is None


def test_edition_statement_without_a_matching_candidate_keeps_only_work_attributes() -> None:
    # 新版の紙版がまだ NDL になく、旧版だけが残る。旧版の ISBN や刊行年月を新版の本に付けない
    book = _book("新版　マーケティングの基本　この１冊ですべてわかる")
    old = record("R100000002-I000010061268", "マーケティングの基本 : この1冊ですべてわかる", isbn="978-4-534-04548-9",
                 issued="2009.3", ndc=("9", "675"))
    match = decide_match(book, [old])
    assert match is not None
    assert (match.method, match.isbn, match.paper_issued, match.ndc) == ("work", None, None, "675")


def test_trailing_series_name_with_a_volume_is_not_stripped() -> None:
    parsed = parse_kindle_title("とある魔術の禁書目録外伝　とある科学の超電磁砲(7) (電撃コミックス)",
                                series_title="とある科学の超電磁砲")
    assert parsed.volume == "7"
    book = _book("銀河英雄伝説 黎明篇 (3)", series_title="黎明篇")
    unnumbered = record("R100000002-I1", "銀河英雄伝説")
    volume_3 = record("R100000002-I3", "銀河英雄伝説 : 黎明篇", volume="3")
    match = decide_match(book, [unnumbered, volume_3])
    assert match is not None and match.candidate_ids == ("R100000002-I3",)


@pytest.mark.parametrize("ndl_volume", ["11.5", "別巻11", "第2部 11", "11・12", "4&11", "rerecord 11", "1-11"])
def test_candidate_volume_that_is_not_a_single_volume_does_not_match(ndl_volume: str) -> None:
    assert not is_adoptable(_book("エマ 11"), record(title="エマ", volume=ndl_volume))


def test_regular_volume_is_an_edition_match_even_with_a_half_volume_next_to_it() -> None:
    book = _book("ハズレ枠の【状態異常スキル】で最強になった俺がすべてを蹂躙するまで 11 (オーバーラップ文庫)")
    title = "ハズレ枠の〈状態異常スキル〉で最強になった俺がすべてを蹂躙するまで"
    regular = record("R100000002-I032867000", title, volume="11", series=("オーバーラップ文庫 ; し-03-20",))
    half = record("R100000002-I033258107", title, volume="11.5", series=("オーバーラップ文庫 ; し-03-21",))
    match = decide_match(book, [regular, half])
    assert match is not None and (match.method, match.candidate_ids) == ("edition", ("R100000002-I032867000",))


def test_publisher_label_does_not_narrow_to_that_publishers_paperback() -> None:
    book = _book("つげ義春日記 (講談社)")
    hardcover = record("R100000002-I1", "つげ義春日記", isbn="4-06-201085-6")
    bunko = record("R100000002-I2", "つげ義春日記", series=("講談社文芸文庫 ; つK1",), isbn="978-4-06-519067-8")
    assert decide_match(book, [hardcover, bunko]).method == "work"


def test_short_series_names_do_not_narrow() -> None:
    book = _book("ビューティフル・エブリデイ（３） (FEEL COMICS)")
    fc = record("R100000002-I1", "ビューティフル・エブリデイ", volume="3", series=("FC",), isbn="978-4-396-76834-8")
    other = record("R100000002-I2", "ビューティフル・エブリデイ", volume="3", isbn="978-4-396-76835-5")
    assert decide_match(book, [fc, other]).method == "work"


def test_volume_subtitle_match_still_requires_the_same_volume() -> None:
    candidate = record(title="星界の紋章", volume="2 (ささやかな戦い)")
    assert not is_adoptable(_book("星界の紋章　３―ささやかな戦い―"), candidate)


def test_parallel_title_keeps_the_main_title_variant() -> None:
    candidate = record(
        title="ファスト&スロー = Thinking, fast and slow : あなたの意思はどのように決まるか?", volume="上"
    )
    assert is_adoptable(_book("ファスト＆スロー（上）"), candidate)


def test_trailing_roman_x_can_be_part_of_the_title() -> None:
    match = decide_match(_book("マルコム X"), [record("R100000002-I1", "マルコムX : 自伝")])
    assert match is not None and match.candidate_ids == ("R100000002-I1",)


def test_series_stage_adds_the_volume_word_when_over_the_limit() -> None:
    stages = search_stages(Book("B1", "理想のヒモ生活(25)", "日月 ネコ", "理想のヒモ生活"))
    assert stages[2].refinements == ({"title": "理想のヒモ生活 25", "creator": "日月 ネコ"},)


# --- 2 回目のレビューで見つかった誤照合の形 --------------------------------------------------------------


def test_wave_dash_and_long_vowel_mark_are_the_same() -> None:
    # NDL は通常の巻を「ぬーべー」、文庫の再刊を「ぬ～べ～」と書く。長音符を残すと文庫の巻だけが一致する
    book = _book("地獄先生ぬ～べ～ 7 (ジャンプコミックスDIGITAL)")
    comics = record("R100000002-I1", "地獄先生ぬーべー", volume="7", series=("ジャンプ・コミックス",))
    bunko = record("R100000002-I2", "地獄先生ぬ～べ～", volume="7", series=("集英社文庫 : コミック版",))
    match = decide_match(book, [comics, bunko])
    assert match is not None and (match.method, match.candidate_ids) == ("edition", ("R100000002-I1",))


def test_volume_paren_followed_by_a_removed_digital_marker() -> None:
    parsed = parse_kindle_title("【愛蔵版】新世紀エヴァンゲリオン（４）〈電子特別版〉 (カドカワデジタルコミックス)")
    assert (parsed.key, parsed.volume, parsed.editions) == ("新世紀エヴァンゲリオン", "4", ("愛蔵版",))


def test_unread_volume_paren_is_kept_in_the_key() -> None:
    # 巻数として読める括弧を書名から消すと、巻数のない本として 1 巻と一致する
    assert not is_adoptable(_book("竜馬がゆく(3)新装版 決定稿"), record(title="竜馬がゆく", volume="1"))


def test_lone_special_edition_is_not_the_edition_of_a_plain_kindle_title() -> None:
    book = _book("蟲師（８） (アフタヌーンコミックス)")
    aizoban = record("R100000002-I1", "蟲師", volume="8", edition="愛蔵版", series=("KCDX ; 3588",),
                     isbn="978-4-06-376988-3", ndc=("9", "726.1"))
    match = decide_match(book, [aizoban])
    assert match is not None
    assert (match.method, match.isbn, match.ndc) == ("work", None, "726.1")


@pytest.mark.parametrize("ndl_title", ["蟲師 : 愛蔵版", "蟲師 (愛蔵版)", "蟲師 愛蔵版"])
def test_special_edition_in_the_ndl_title_is_not_the_edition_of_a_plain_kindle_title(ndl_title: str) -> None:
    # 版表示が空でも、書名に愛蔵版と書かれた紙版の ISBN を付けない
    aizoban = record("R100000002-I1", ndl_title, volume="8", isbn="978-4-06-376988-3", ndc=("9", "726.1"))
    match = decide_match(_book("蟲師（８） (アフタヌーンコミックス)"), [aizoban])
    assert match is not None
    assert (match.method, match.isbn) == ("work", None)


def test_edition_statement_matches_the_edition_written_in_the_ndl_title() -> None:
    book = _book("蟲師（８） 愛蔵版")
    plain = record("R100000002-I1", "蟲師", volume="8", isbn="4-06-314393-9")
    aizoban = record("R100000002-I2", "蟲師 : 愛蔵版", volume="8", isbn="978-4-06-376988-3")
    match = decide_match(book, [plain, aizoban])
    assert match is not None
    assert (match.method, match.isbn) == ("edition", "9784063769883")
    # 版表示があれば、書名の版表記より版表示を使う
    stated = record("R100000002-I3", "蟲師 : 愛蔵版", volume="8", edition="新装版", isbn="978-4-06-376988-3")
    assert decide_match(book, [plain, stated]).method == "work"


def test_special_edition_word_inside_a_title_word_is_not_an_edition() -> None:
    book = _book("Excel完全版マニュアル")
    match = decide_match(book, [record("R100000002-I1", "Excel完全版マニュアル", isbn="978-4-06-376988-3")])
    assert match is not None and match.method == "edition"


@pytest.mark.parametrize(
    "kindle_title", ["攻殻機動隊（１．５）", "竜馬がゆく (弐)", "竜馬がゆく (其の二)", "竜馬がゆく (第三話)"]
)
def test_numeric_parens_that_are_not_a_single_volume_match_no_volume(kindle_title: str) -> None:
    title = kindle_title.split(" ")[0].split("（")[0]
    candidates = [record("R100000002-I0", title), *_volumes(title)]
    assert decide_match(_book(kindle_title), candidates) is None


def test_kanji_episode_number_is_a_split_edition() -> None:
    assert parse_kindle_title("竜馬がゆく 【第三話】").split_edition


@pytest.mark.parametrize("kindle_title", ["竜馬がゆく (新装版)", "竜馬がゆく【改訂第2版】", "竜馬がゆく【第二版】"])
def test_edition_statements_in_brackets_and_parentheses_are_recorded(kindle_title: str) -> None:
    old = record("R100000002-I1", "竜馬がゆく", isbn="978-4-16-710567-4", issued="1998.9")
    match = decide_match(_book(kindle_title), [old])
    assert match is not None and (match.method, match.isbn) == ("work", None)


def test_edition_statement_matches_only_the_same_edition() -> None:
    book = _book("新版　マーケティングの基本")
    old = record("R100000002-I1", "マーケティングの基本", isbn="978-4-534-04548-9")
    revised = record("R100000002-I2", "マーケティングの基本", edition="改訂新版", isbn="978-4-534-00002-8")
    match = decide_match(book, [old, revised])
    assert match is not None and match.method == "work"


def test_label_does_not_match_a_shorter_series_of_another_imprint() -> None:
    book = _book("長いお別れ (ハヤカワ・ミステリ文庫)")
    pocket = record("R100000002-I1", "長いお別れ", series=("ハヤカワ・ミステリ ; 1234",), isbn="4-15-000123-4")
    hardcover = record("R100000002-I2", "長いお別れ", isbn="4-15-200123-5")
    assert decide_match(book, [pocket, hardcover]).method == "work"
    # DIGITAL のような付け足しは同じ叢書として扱う
    assert _label_matches("ジャンプコミックスDIGITAL", record(series=("ジャンプ・コミックス",)))


def test_novelization_does_not_share_the_manga_title() -> None:
    book = _book("エマ 1巻 (HARTA COMIX)")
    novel = record("R100000002-I1", "エマ : 小説", volume="1", series=("ファミ通文庫",))
    manga = record("R100000002-I2", "エマ", volume="1", series=("Beam comix",))
    match = decide_match(book, [novel, manga])
    assert match is not None and match.candidate_ids == ("R100000002-I2",)


def test_derived_work_subtitle_with_a_long_vowel_mark_does_not_share_the_title() -> None:
    assert not is_adoptable(_book("エマ"), record("R100000002-I1", "エマ : アニメーションガイド"))


@pytest.mark.parametrize("subtitle", ["小説版", "コミカライズ版"])
def test_derived_work_subtitle_with_ban_does_not_share_the_title(subtitle: str) -> None:
    assert not is_adoptable(_book("エマ"), record("R100000002-I1", f"エマ : {subtitle}"))
    # 副題まで含めた書名なら同じ作品として採る
    assert is_adoptable(_book(f"エマ {subtitle}"), record("R100000002-I1", f"エマ : {subtitle}"))


def test_whole_title_reading_does_not_take_volume_1_of_a_sequel() -> None:
    candidates = [record("R100000002-I1", "ドラゴン桜", volume="1"), record("R100000002-I2", "ドラゴン桜2", volume="1")]
    assert decide_match(_book("ドラゴン桜 2"), candidates) is None


def test_unicode_roman_numerals_match_ascii_roman_in_ndl_titles() -> None:
    book = _book("ファイナルファンタジーⅦ アルティマニア")
    assert is_adoptable(book, record(title="ファイナルファンタジーVIIアルティマニア"))
    assert is_adoptable(book, record(title="ファイナルファンタジーⅦアルティマニア"))
