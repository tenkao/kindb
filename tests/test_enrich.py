"""Tests for kindb enrich / rematch: 検索の段、状態の遷移、訂正、書き込み、中断。通信は偽物に置き換える。"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kindb import enrich
from kindb.db import DatabaseLockedError, connect
from kindb.enrich import load_overrides_csv, run_enrich, run_rematch
from kindb.importer import import_kindle_json
from kindb.ndl import NdlError
from tests.create_fixture import create_kindle_json
from tests.ndl_fixtures import FakeOpenSearch, http_error, item_xml, rss

HIMO = "B0000000A1"
TOYOTA = "B0000000A2"
TSUGE = "B0000000A3"

BOOKS = [
    {"asin": HIMO, "title": "理想のヒモ生活(3) (角川コミックス・エース)", "authors": "日月 ネコ"},
    {"asin": TOYOTA, "title": "トヨタ生産方式", "authors": "大野耐一"},
    {"asin": TSUGE, "title": "つげ義春日記 (講談社文芸文庫)", "authors": "つげ義春"},
]


def _kindle_rows(books: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {**b, "acquiredTime": 1704067200000, "readStatus": "UNKNOWN", "productImage": None} for b in books
    ]


@pytest.fixture
def library(tmp_path: Path) -> Path:
    db = tmp_path / "library.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "kindle.json", _kindle_rows(BOOKS)), db)
    return db


def _rows(db: Path, sql: str, params: list | None = None) -> list[tuple]:
    con = connect(db, read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _status(db: Path) -> dict[str, str]:
    return dict(_rows(db, "SELECT asin, status FROM bib_fetches"))


def _himo(volume: str, record_id: str | None = None, **kwargs: object) -> str:
    return item_xml(record_id or f"R100000002-I0000000{int(volume):02d}", "理想のヒモ生活", volume=volume,
                    series=("角川コミックス・エース",), isbn=f"978-4-04-000000-{volume}", ndc=("10", "726.1"), **kwargs)


TOYOTA_ITEM = item_xml("R100000002-I000001376735", "トヨタ生産方式 : 脱規模の経営をめざして",
                       ndc=("", "509.6"), subjects=("トヨタ生産方式",), issued="1978.5", extent="232p",
                       publishers=("ダイヤモンド社",))
TSUGE_BUNKO = item_xml("R100000002-I030280980", "つげ義春日記", series=("講談社文芸文庫 ; つK1",),
                       isbn="978-4-06-519067-8", ndc=("10", "726.101"), subjects=("つげ, 義春, 1937-2026",))
TSUGE_HARDCOVER = item_xml("R100000002-I000001657059", "つげ義春日記", isbn="4-06-201085-6", issued="1983.12")

HIMO_QUERY = {"title": "理想のヒモ生活", "creator": "日月 ネコ"}
TOYOTA_QUERY = {"title": "トヨタ生産方式", "creator": "大野耐一"}
TSUGE_QUERY = {"title": "つげ義春日記", "creator": "つげ義春"}


def _standard_ndl() -> FakeOpenSearch:
    return FakeOpenSearch(
        [
            (HIMO_QUERY, rss([_himo("2"), _himo("3"), _himo("4")])),
            (TOYOTA_QUERY, rss([TOYOTA_ITEM])),
            (TSUGE_QUERY, rss([TSUGE_HARDCOVER, TSUGE_BUNKO])),
        ]
    )


# --- 取得と保存 ----------------------------------------------------------------------------------------


def test_enrich_saves_state_candidates_and_match(library: Path) -> None:
    ndl = _standard_ndl()
    summary = run_enrich(library, ndl.client())

    assert (summary.targets, summary.fetched, summary.counts) == (3, 3, {"found": 3})
    assert _status(library) == {HIMO: "found", TOYOTA: "found", TSUGE: "found"}
    # 検索を止めた段の紙版の候補をすべて保存する(採用した候補だけではない)
    assert _rows(library, "SELECT candidate_id, search_rank FROM bib_candidates WHERE asin = ? ORDER BY search_rank",
                 [HIMO]) == [("R100000002-I000000002", 1), ("R100000002-I000000003", 2), ("R100000002-I000000004", 3)]
    assert not _rows(library, "SELECT 1 FROM bib_candidates WHERE item_xml LIKE '%rdfs:seeAlso%'")

    rows = _rows(
        library,
        """SELECT asin, bib_match, isbn, paper_issued, publisher, pages, bib_series, ndc, ndc_label, subjects
           FROM v_books ORDER BY asin""",
    )
    assert rows == [
        (HIMO, "edition", "9784040000003", "2020-01", "出版社", 200, "角川コミックス・エース", "726.1",
         "漫画．挿絵．童画", []),
        (TOYOTA, "edition", None, "1978-05", "ダイヤモンド社", 232, None, "509.6", "工業．工業経済",
         ["トヨタ生産方式"]),
        (TSUGE, "edition", "9784065190678", "2020-01", "出版社", 200, "講談社文芸文庫 ; つK1", "726.101",
         "漫画．挿絵．童画", ["つげ, 義春, 1937-2026"]),
    ]
    assert _rows(library, "SELECT stages FROM bib_fetches WHERE asin = ?", [TOYOTA])[0][0] == (
        '[{"stage": "title_creator", "params": {"title": "トヨタ生産方式", "creator": "大野耐一"}, "total": 1}]'
    )


def test_enrich_resumes_without_refetching_saved_books(library: Path) -> None:
    ndl = _standard_ndl()
    run_enrich(library, ndl.client(), limit=1)
    assert _status(library) == {HIMO: "found"}

    ndl.calls.clear()
    summary = run_enrich(library, ndl.client())
    assert summary.targets == 2
    assert HIMO_QUERY not in ndl.calls
    assert set(_status(library)) == {HIMO, TOYOTA, TSUGE}

    ndl.calls.clear()
    assert run_enrich(library, ndl.client()).targets == 0
    assert ndl.calls == []


def test_where_limits_the_books_and_is_validated(library: Path) -> None:
    ndl = _standard_ndl()
    run_enrich(library, ndl.client(), where="title LIKE 'トヨタ%'")
    assert _status(library) == {TOYOTA: "found"}
    with pytest.raises(ValueError, match="single condition"):
        run_enrich(library, ndl.client(), where="TRUE; DELETE FROM books")
    with pytest.raises(ValueError, match="Invalid --where"):
        run_enrich(library, ndl.client(), where="no_such_column = 1")


# --- 検索の段と状態 ------------------------------------------------------------------------------------


def test_stage_with_only_other_volumes_continues_to_the_next_stage(library: Path) -> None:
    # 書名と著者の検索では同じ作品の別の巻しか返らず、書名だけの検索で 3 巻が返る
    ndl = FakeOpenSearch(
        [
            (HIMO_QUERY, rss([_himo("1"), _himo("2")])),
            ({"title": "理想のヒモ生活"}, rss([_himo("1"), _himo("3")])),
        ]
    )
    run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'")
    assert ndl.calls == [HIMO_QUERY, {"title": "理想のヒモ生活"}]
    assert _rows(library, "SELECT candidate_ids FROM bib_matches WHERE asin = ?", [HIMO]) == [
        (["R100000002-I000000003"],)
    ]
    # 保存する候補は止めた段のもの
    assert [r[0] for r in _rows(library, "SELECT candidate_id FROM bib_candidates ORDER BY search_rank")] == [
        "R100000002-I000000001",
        "R100000002-I000000003",
    ]


def test_stage_over_limit_even_after_refinement_leaves_book_incomplete(library: Path) -> None:
    ndl = FakeOpenSearch(
        [
            (HIMO_QUERY, rss([_himo("3")], total=800)),
            ({"title": "理想のヒモ生活 3", "creator": "日月 ネコ"}, rss([_himo("3")], total=600)),
            ({"title": "理想のヒモ生活"}, rss([], total=0)),
        ]
    )
    run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'")
    # 500 件を超えた応答に採用できる候補があっても、全部を見ていないので使わない
    assert _status(library) == {HIMO: "incomplete"}
    assert _rows(library, "SELECT count(*) FROM bib_candidates")[0][0] == 0
    assert _rows(library, "SELECT bib_match FROM v_books WHERE asin = ?", [HIMO]) == [(None,)]


def test_stage_resolved_by_refinement_without_candidates_counts_as_not_found(library: Path) -> None:
    # 最初の検索は 800 件、絞り直すと 100 件で候補なし。途中の超過は段の結論に影響しない
    ndl = FakeOpenSearch(
        [
            (HIMO_QUERY, rss([], total=800)),
            ({"title": "理想のヒモ生活 3", "creator": "日月 ネコ"}, rss([_himo("4")], total=100)),
        ]
    )
    run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'")
    assert _status(library) == {HIMO: "not_found"}


def test_only_other_works_means_not_found(library: Path) -> None:
    other = item_xml("R100000002-I025245537", "トヨタ生産方式の原点 : かんばん方式の生みの親が「現場力」を語る")
    ndl = FakeOpenSearch([({"title": "トヨタ生産方式"}, rss([other], total=120))])
    run_enrich(library, ndl.client(), where=f"asin = '{TOYOTA}'")
    assert ndl.calls == [TOYOTA_QUERY, {"title": "トヨタ生産方式"}]
    assert _status(library) == {TOYOTA: "not_found"}


def test_retry_missing_refetches_only_not_found_and_incomplete(library: Path) -> None:
    ndl = FakeOpenSearch([(TOYOTA_QUERY, rss([TOYOTA_ITEM]))])
    run_enrich(library, ndl.client())
    assert _status(library) == {HIMO: "not_found", TOYOTA: "found", TSUGE: "not_found"}

    ndl = _standard_ndl()
    assert run_enrich(library, ndl.client()).targets == 0
    summary = run_enrich(library, ndl.client(), retry_missing=True)
    assert summary.targets == 2
    assert TOYOTA_QUERY not in ndl.calls
    assert _status(library) == {HIMO: "found", TOYOTA: "found", TSUGE: "found"}


def test_error_is_saved_and_retried_next_time(library: Path) -> None:
    ndl = FakeOpenSearch([(TOYOTA_QUERY, urllib.error.URLError("down"))])
    run_enrich(library, ndl.client(), where=f"asin = '{TOYOTA}'")
    assert _rows(library, "SELECT status, error FROM bib_fetches") == [
        ("error", "Could not reach NDL Search: <urlopen error down>")
    ]

    run_enrich(library, _standard_ndl().client(), where=f"asin = '{TOYOTA}'")
    assert _status(library) == {TOYOTA: "found"}


def test_consecutive_network_failures_stop_the_run(library: Path, tmp_path: Path) -> None:
    books = [{"asin": f"B00000ERR{i}", "title": f"本{i}", "authors": "著者"} for i in range(8)]
    db = tmp_path / "errors.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "errors.json", _kindle_rows(books)), db)
    ndl = FakeOpenSearch()
    ndl.on_call = lambda _: (_ for _ in ()).throw(urllib.error.URLError("down"))

    summary = run_enrich(db, ndl.client())
    assert summary.aborted
    assert summary.fetched == enrich.MAX_CONSECUTIVE_ERRORS
    assert set(_status(db).values()) == {"error"}
    assert len(_status(db)) == enrich.MAX_CONSECUTIVE_ERRORS


# --- 引き直し ------------------------------------------------------------------------------------------


def test_refresh_replaces_found_books_in_scope_and_keeps_others(library: Path, tmp_path: Path) -> None:
    run_enrich(library, _standard_ndl().client())
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(f"asin,isbn\n{TSUGE},\n", encoding="utf-8")
    run_enrich(library, _standard_ndl().client(), overrides_path=csv_path)
    assert _status(library)[TSUGE] == "excluded"

    newer = _himo("3", record_id="R100000002-I000000099", issued="2024.1")
    ndl = FakeOpenSearch([(HIMO_QUERY, rss([newer]))])
    summary = run_enrich(library, ndl.client(), refresh=True, where=f"asin IN ('{HIMO}', '{TSUGE}')")

    assert summary.targets == 1  # 「照合しない」の本は引き直さない
    assert _rows(library, "SELECT candidate_id FROM bib_candidates WHERE asin = ?", [HIMO]) == [
        ("R100000002-I000000099",)
    ]
    assert _rows(library, "SELECT candidate_ids, paper_issued FROM bib_matches WHERE asin = ?", [HIMO]) == [
        (["R100000002-I000000099"], "2024-01")
    ]
    # --where の外の本は変わらない
    assert _rows(library, "SELECT candidate_ids FROM bib_matches WHERE asin = ?", [TOYOTA]) == [
        (["R100000002-I000001376735"],)
    ]
    assert _status(library)[TSUGE] == "excluded"


def test_refresh_failure_keeps_the_previous_state_candidates_and_match(library: Path) -> None:
    run_enrich(library, _standard_ndl().client(), where=f"asin = '{TOYOTA}'")
    before = _rows(library, "SELECT * FROM bib_matches")

    ndl = FakeOpenSearch([(TOYOTA_QUERY, urllib.error.URLError("down"))])
    summary = run_enrich(library, ndl.client(), refresh=True, where=f"asin = '{TOYOTA}'")

    assert summary.counts == {"error": 1}
    assert _status(library) == {TOYOTA: "found"}
    assert _rows(library, "SELECT * FROM bib_matches") == before
    assert _rows(library, "SELECT count(*) FROM bib_candidates")[0][0] == 1


# --- 手動訂正 ------------------------------------------------------------------------------------------


def _write_overrides(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "overrides.csv"
    path.write_text("asin,isbn\n" + body, encoding="utf-8")
    return path


def test_overrides_reset_state_and_fetch_by_isbn(library: Path, tmp_path: Path) -> None:
    run_enrich(library, _standard_ndl().client())
    isbn_item = item_xml("R100000002-I000009999999", "トヨタ生産方式 : 脱規模の経営をめざして",
                         isbn="978-4-478-46037-5", ndc=("10", "509.6"))
    ndl = FakeOpenSearch([({"isbn": "9784478460375"}, rss([isbn_item]))])

    # ISBN を足した本は、状態、候補、照合結果を消して未取得に戻し、指定された ISBN で引き直す
    overrides = _write_overrides(tmp_path, f"{TOYOTA},978-4-478-46037-5\n")
    summary = run_enrich(library, ndl.client(), overrides_path=overrides)
    assert summary.overrides.reset == [TOYOTA]
    assert ndl.calls == [{"isbn": "9784478460375"}]
    assert _rows(library, "SELECT source, status FROM bib_fetches WHERE asin = ?", [TOYOTA]) == [("isbn", "found")]
    assert _rows(library, "SELECT method, isbn FROM bib_matches WHERE asin = ?", [TOYOTA]) == [
        ("isbn", "9784478460375")
    ]

    # 同じ内容の CSV をもう一度渡しても、状態は戻らない
    ndl.calls.clear()
    overrides = _write_overrides(tmp_path, f"{TOYOTA},9784478460375\n")
    summary = run_enrich(library, ndl.client(), overrides_path=overrides)
    assert summary.overrides.reset == [] and ndl.calls == []

    # ISBN を変えた本も未取得に戻る
    overrides = _write_overrides(tmp_path, f"{TOYOTA},4-15-030552-8\n")
    summary = run_enrich(library, ndl.client(), overrides_path=overrides)
    assert summary.overrides.reset == [TOYOTA]
    assert ndl.calls == [{"isbn": "9784150305529"}]
    assert _status(library)[TOYOTA] == "not_found"


def test_override_with_empty_isbn_excludes_the_book(library: Path, tmp_path: Path) -> None:
    run_enrich(library, _standard_ndl().client())
    ndl = _standard_ndl()
    summary = run_enrich(library, ndl.client(), overrides_path=_write_overrides(tmp_path, f"{TSUGE},\n"),
                         refresh=True, retry_missing=True)
    assert summary.overrides.excluded == [TSUGE]
    assert TSUGE_QUERY not in ndl.calls
    assert _status(library)[TSUGE] == "excluded"
    assert _rows(library, "SELECT count(*) FROM bib_candidates WHERE asin = ?", [TSUGE])[0][0] == 0
    assert _rows(library, "SELECT bib_match, subjects FROM v_books WHERE asin = ?", [TSUGE]) == [(None, [])]


def test_removed_override_row_resets_the_book_to_title_search(library: Path, tmp_path: Path) -> None:
    isbn_item = item_xml("R100000002-I000009999999", "トヨタ生産方式", isbn="978-4-478-46037-5")
    ndl = FakeOpenSearch([({"isbn": "9784478460375"}, rss([isbn_item])), (TOYOTA_QUERY, rss([TOYOTA_ITEM]))])
    run_enrich(library, ndl.client(), where=f"asin = '{TOYOTA}'",
               overrides_path=_write_overrides(tmp_path, f"{TOYOTA},9784478460375\n"))
    assert _rows(library, "SELECT candidate_id FROM bib_candidates") == [("R100000002-I000009999999",)]

    # 行を消した CSV を渡すと未取得に戻る。--where の外なので引き直されない
    summary = run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'",
                         overrides_path=_write_overrides(tmp_path, ""))
    assert summary.overrides.reset == [TOYOTA]
    assert TOYOTA not in _status(library)
    assert _rows(library, "SELECT count(*) FROM bib_candidates WHERE asin = ?", [TOYOTA])[0][0] == 0

    # 引き直さないまま再照合しても、古い訂正で取った候補から採用し直さない
    run_rematch(library)
    assert _rows(library, "SELECT count(*) FROM bib_matches WHERE asin = ?", [TOYOTA])[0][0] == 0

    ndl.calls.clear()
    run_enrich(library, ndl.client(), where=f"asin = '{TOYOTA}'")
    assert ndl.calls == [TOYOTA_QUERY]
    assert _rows(library, "SELECT source, status FROM bib_fetches WHERE asin = ?", [TOYOTA]) == [("title", "found")]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("B1,978-4-8222-5085-1\n", "invalid ISBN"),
        ("B1,\nB1,9784822250850\n", "duplicate ASIN B1"),
        (",9784822250850\n", "empty ASIN"),
        ("B1,9784822250850,extra\n", "expected 2 columns"),
    ],
)
def test_overrides_csv_is_validated_before_touching_the_db(
    library: Path, tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        run_enrich(library, _standard_ndl().client(), overrides_path=_write_overrides(tmp_path, body))
    assert _rows(library, "SELECT count(*) FROM bib_overrides")[0][0] == 0


def test_overrides_csv_requires_the_header(tmp_path: Path) -> None:
    path = tmp_path / "o.csv"
    path.write_text("B1,9784822250850\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        load_overrides_csv(path)


def test_overrides_csv_accepts_bom_and_case_insensitive_header(tmp_path: Path) -> None:
    path = tmp_path / "o.csv"
    path.write_text("﻿ASIN,ISBN\nB1, 978-4-8222-5085-0 \nB2,\n", encoding="utf-8")
    assert load_overrides_csv(path) == {"B1": "9784822250850", "B2": None}


# --- 再照合 --------------------------------------------------------------------------------------------


def test_rematch_redoes_matching_from_saved_candidates_only_for_found_books(library: Path) -> None:
    ndl = FakeOpenSearch([(TSUGE_QUERY, rss([TSUGE_HARDCOVER, TSUGE_BUNKO]))])
    run_enrich(library, ndl.client())
    assert _status(library) == {HIMO: "not_found", TOYOTA: "not_found", TSUGE: "found"}

    con = connect(library)
    try:
        # 照合結果を壊しておき、見つからない本には候補を差し込んでおく
        con.execute("UPDATE bib_matches SET candidate_ids = ['broken'], isbn = NULL")
        con.execute("INSERT INTO bib_candidates VALUES (?, 'R100000002-I000001376735', 1, ?)", [TOYOTA, TOYOTA_ITEM])
    finally:
        con.close()

    summary = run_rematch(library)
    assert (summary.books, summary.changed, summary.lost) == (1, 1, 0)
    assert _rows(library, "SELECT asin, candidate_ids, isbn FROM bib_matches") == [
        (TSUGE, ["R100000002-I030280980"], "9784065190678")
    ]


def test_rematch_removes_matches_that_saved_candidates_no_longer_support(library: Path) -> None:
    run_enrich(library, _standard_ndl().client(), where=f"asin = '{TOYOTA}'")
    other = item_xml("R100000002-I025245537", "トヨタ生産方式の原点")
    con = connect(library)
    try:
        con.execute("UPDATE bib_candidates SET item_xml = ?", [other])
    finally:
        con.close()

    summary = run_rematch(library)
    assert (summary.books, summary.changed, summary.lost) == (1, 1, 1)
    assert _rows(library, "SELECT count(*) FROM bib_matches")[0][0] == 0
    assert _status(library) == {TOYOTA: "found"}


# --- 書き込みと中断 ------------------------------------------------------------------------------------


def test_ctrl_c_saves_books_fetched_so_far(library: Path) -> None:
    ndl = _standard_ndl()

    def interrupt_on_third_request(count: int) -> None:
        if count == 3:
            raise KeyboardInterrupt

    ndl.on_call = interrupt_on_third_request
    summary = run_enrich(library, ndl.client())
    assert summary.interrupted
    assert summary.fetched == 2
    assert _status(library) == {HIMO: "found", TOYOTA: "found"}


def test_lock_conflict_on_write_is_retried(library: Path) -> None:
    attempts: list[Path] = []
    sleeps: list[float] = []

    def flaky_connect(db_path: Path):
        attempts.append(db_path)
        # 開始時の書き込み(スキーマと訂正)は通し、取得結果の書き込みで 2 回衝突させる
        if len(attempts) in (2, 3):
            raise DatabaseLockedError("locked")
        return connect(db_path)

    summary = run_enrich(library, _standard_ndl().client(), connector=flaky_connect, sleep=sleeps.append)
    assert summary.fetched == 3
    assert len(attempts) == 4
    assert sleeps == [1.0, 2.0]
    assert len(_status(library)) == 3


def test_each_batch_is_written_while_the_run_continues(library: Path) -> None:
    # 取得中は DB を開いていないので、別の接続から途中の書き込みが見える
    ndl = _standard_ndl()
    seen: dict[int, dict[str, str]] = {}
    ndl.on_call = lambda count: seen.setdefault(count, _status(library))
    run_enrich(library, ndl.client(), batch_size=1)
    assert seen[1] == {}
    assert seen[2] == {HIMO: "found"}
    assert seen[3] == {HIMO: "found", TOYOTA: "found"}


def test_batch_that_stays_locked_is_kept_and_written_with_the_next_batch(library: Path) -> None:
    attempts: list[Path] = []

    def connect_locked_for_first_batch(db_path: Path):
        attempts.append(db_path)
        # 開始時の書き込みは通し、1 冊目の書き込みを 30 秒ぶん(7 回)待っても空かないようにする
        if 2 <= len(attempts) <= 8:
            raise DatabaseLockedError("locked")
        return connect(db_path)

    ndl = _standard_ndl()
    seen: dict[int, dict[str, str]] = {}
    ndl.on_call = lambda count: seen.setdefault(count, _status(library))
    summary = run_enrich(
        library, ndl.client(), connector=connect_locked_for_first_batch, sleep=lambda _: None, batch_size=1
    )
    assert summary.fetched == 3
    # 1 冊目は諦めた書き込みのあともバッファに残り、2 冊目と一緒に書かれる
    assert seen[2] == {}
    assert seen[3] == {HIMO: "found", TOYOTA: "found"}
    assert len(_status(library)) == 3


def test_after_giving_up_a_write_the_next_try_waits_for_another_batch(tmp_path: Path) -> None:
    books = [{"asin": f"B00000LCK{i}", "title": f"本{i}", "authors": "著者"} for i in range(1, 7)]
    db = tmp_path / "lock.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "lock.json", _kindle_rows(books)), db)
    state = {"fetched": 0}
    waits_at: list[int] = []

    class Recorder(enrich.Reporter):
        def book(self, index, total, target, result) -> None:
            state["fetched"] = index

        def waiting_for_lock(self) -> None:
            waits_at.append(state["fetched"])

    def connect_locked_until_done(db_path: Path):
        if 0 < state["fetched"] < 6:
            raise DatabaseLockedError("locked")
        return connect(db_path)

    summary = run_enrich(db, FakeOpenSearch().client(), connector=connect_locked_until_done,
                         sleep=lambda _: None, batch_size=2, reporter=Recorder())
    assert summary.fetched == 6
    # 2 冊ごとに試し直す。諦めた直後から 1 冊ごとに試すと、1 冊ごとに 30 秒待つ
    assert waits_at == [2, 4]
    assert len(_status(db)) == 6


def test_final_write_that_stays_locked_raises(library: Path) -> None:
    calls = []

    def always_locked_after_start(db_path: Path):
        calls.append(db_path)
        if len(calls) > 1:
            raise DatabaseLockedError("locked")
        return connect(db_path)

    with pytest.raises(DatabaseLockedError):
        run_enrich(library, _standard_ndl().client(), connector=always_locked_after_start, sleep=lambda _: None)


def test_import_leaves_bibliographic_data_and_views_hide_removed_books(library: Path, tmp_path: Path) -> None:
    run_enrich(library, _standard_ndl().client())
    before = _rows(library, "SELECT * FROM bib_matches ORDER BY asin")

    import_kindle_json(create_kindle_json(tmp_path / "fewer.json", _kindle_rows(BOOKS[:2])), library)

    assert _rows(library, "SELECT * FROM bib_matches ORDER BY asin") == before
    assert [r[0] for r in _rows(library, "SELECT asin FROM v_books WHERE bib_match IS NOT NULL ORDER BY asin")] == [
        HIMO,
        TOYOTA,
    ]


def test_fetch_book_turns_client_errors_into_error_results() -> None:
    class Failing:
        interval = 0.0

        def search(self, params: dict[str, str]):
            raise NdlError("HTTP 503 from NDL Search")

    result = enrich.fetch_book(Failing(), enrich.Book("B1", "本", "著者"))
    assert (result.status, result.error, result.candidates, result.match) == (
        "error",
        "HTTP 503 from NDL Search",
        [],
        None,
    )


# --- 取得の実行の細部 -----------------------------------------------------------------------------------


def _numbered_library(tmp_path: Path, count: int) -> Path:
    # 書名の末尾の数字は巻数と読まれるので、かなで区別する
    books = [{"asin": f"B0000NUM{i:02d}", "title": f"本{chr(0x3042 + i)}", "authors": "著者"} for i in range(count)]
    db = tmp_path / "numbered.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "numbered.json", _kindle_rows(books)), db)
    return db


def test_a_success_between_failures_resets_the_consecutive_error_count(tmp_path: Path) -> None:
    db = _numbered_library(tmp_path, 9)
    down = urllib.error.URLError("down")
    ndl = FakeOpenSearch([({"title": f"本{chr(0x3042 + i)}", "creator": "著者"}, down) for i in range(9) if i != 4])
    summary = run_enrich(db, ndl.client())
    assert not summary.aborted
    assert summary.counts == {"error": 8, "not_found": 1}


def test_run_stops_when_ndl_asks_to_wait_too_long(library: Path) -> None:
    ndl = _standard_ndl()
    ndl.routes.insert(0, (TOYOTA_QUERY, http_error(429, retry_after="3600")))
    summary = run_enrich(library, ndl.client())
    assert (summary.aborted, summary.retry_after, summary.fetched) == ("retry_later", 3600.0, 1)
    assert ndl.calls == [HIMO_QUERY, TOYOTA_QUERY]
    assert _status(library) == {HIMO: "found"}


@pytest.mark.parametrize(("total", "status"), [(500, "found"), (501, "incomplete")])
def test_exactly_500_results_count_as_seen_in_full(library: Path, total: int, status: str) -> None:
    ndl = FakeOpenSearch([(HIMO_QUERY, rss([_himo("3")], total=total)),
                          ({"title": "理想のヒモ生活 3", "creator": "日月 ネコ"}, rss([_himo("3")], total=total))])
    run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'")
    assert _status(library) == {HIMO: status}


def test_only_ndl_paper_books_are_saved_as_candidates(library: Path) -> None:
    audio = item_xml("R100000002-I000000901", "トヨタ生産方式", categories=("録音資料", "記録メディア"))
    digital = item_xml("R100000002-I000000902", "トヨタ生産方式", categories=("図書", "デジタル"))
    other_provider = item_xml("R100000136-I000000903", "トヨタ生産方式")
    ndl = FakeOpenSearch([(TOYOTA_QUERY, rss([audio, digital, other_provider, TOYOTA_ITEM]))])
    run_enrich(library, ndl.client(), where=f"asin = '{TOYOTA}'")
    assert _rows(library, "SELECT candidate_id FROM bib_candidates") == [("R100000002-I000001376735",)]


def test_the_same_query_is_not_sent_twice_for_a_book(library: Path) -> None:
    # 書名だけの段の絞り直しは、書名と著者の段の検索と同じ条件になる
    ndl = FakeOpenSearch([(HIMO_QUERY, rss([], total=800)),
                          ({"title": "理想のヒモ生活 3", "creator": "日月 ネコ"}, rss([], total=600)),
                          ({"title": "理想のヒモ生活"}, rss([], total=900))])
    run_enrich(library, ndl.client(), where=f"asin = '{HIMO}'")
    assert ndl.calls == [HIMO_QUERY, {"title": "理想のヒモ生活 3", "creator": "日月 ネコ"}, {"title": "理想のヒモ生活"}]
    assert _status(library) == {HIMO: "incomplete"}


def test_invalid_where_is_reported_before_overrides_are_applied(library: Path, tmp_path: Path) -> None:
    run_enrich(library, _standard_ndl().client(), where=f"asin = '{TOYOTA}'")
    with pytest.raises(ValueError, match="Invalid --where"):
        run_enrich(library, _standard_ndl().client(), where="titel LIKE 'トヨタ%'",
                   overrides_path=_write_overrides(tmp_path, f"{TOYOTA},9784478460375\n"))
    assert _rows(library, "SELECT count(*) FROM bib_overrides")[0][0] == 0
    assert _status(library) == {TOYOTA: "found"}


def test_split_edition_titles_are_not_searched(tmp_path: Path) -> None:
    books = [{"asin": "B0000SPLIT", "title": "理想のヒモ生活【分冊版】　12", "authors": "日月 ネコ"}]
    db = tmp_path / "split.duckdb"
    import_kindle_json(create_kindle_json(tmp_path / "split.json", _kindle_rows(books)), db)
    ndl = FakeOpenSearch()
    run_enrich(db, ndl.client())
    assert ndl.calls == []
    assert _rows(db, "SELECT status, stages FROM bib_fetches") == [
        ("not_found", '[{"stage": "skipped", "reason": "split_edition"}]')
    ]
