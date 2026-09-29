"""手動訂正の Web UI(kindb fix)のテスト。NDL には通信しない。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from kindb import fixui
from kindb.cli import app
from kindb.enrich import EnrichLockedError, run_enrich
from kindb.fixui import FixServer, apply_changes, book_detail, list_books, load_current_overrides
from kindb.matching import normalize_isbn
from tests.ndl_fixtures import FakeOpenSearch, item_xml, rss
from tests.test_enrich import (
    HIMO,
    HIMO_QUERY,
    TOYOTA,
    TOYOTA_QUERY,
    TSUGE,
    TSUGE_BUNKO,
    TSUGE_HARDCOVER,
    TSUGE_QUERY,
    _enrich_lock_held_elsewhere,
    _himo,
    _rows,
)
from tests.test_enrich import library as library  # noqa: F401 - fixture を使い回す

runner = CliRunner()
TOYOTA_ISBN = "9784478460375"
TOYOTA_BY_ISBN = item_xml(
    "R100000002-I000009999999", "トヨタ生産方式 : 脱規模の経営をめざして", isbn="978-4-478-46037-5"
)


def _fetched(library: Path) -> None:
    # HIMO と TSUGE は見つかり、TOYOTA は見つからない
    ndl = FakeOpenSearch([(HIMO_QUERY, rss([_himo("3")])), (TSUGE_QUERY, rss([TSUGE_HARDCOVER, TSUGE_BUNKO]))])
    run_enrich(library, ndl.client())


def _factory(ndl: FakeOpenSearch):
    return lambda: ndl.client()


def _csv(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "overrides.csv"
    path.write_text("asin,isbn\n" + body, encoding="utf-8")
    return path


# --- 一覧と詳細 -----------------------------------------------------------------------------------------


def test_review_list_shows_books_needing_attention_and_overridden_books(library: Path) -> None:
    _fetched(library)
    review = list_books(library, {})
    assert [b["asin"] for b in review["books"]] == [TOYOTA]
    assert review["books"][0]["status"] == "not_found"

    # 訂正のある本は、照合できていても要確認に出す
    with_override = list_books(library, {HIMO: "9784040000003"})
    assert {b["asin"] for b in with_override["books"]} == {HIMO, TOYOTA}

    assert list_books(library, {}, view="all")["total"] == 3
    assert [b["asin"] for b in list_books(library, {}, view="all", query="つげ")["books"]] == [TSUGE]


def test_detail_returns_candidates_with_13_digit_isbns_and_the_current_match(library: Path) -> None:
    _fetched(library)
    detail = book_detail(library, TSUGE, {})
    cands = {c["id"]: c for c in detail["candidates"]}
    assert cands["R100000002-I000001657059"]["isbns"] == [normalize_isbn("4-06-201085-6")]
    assert cands["R100000002-I030280980"]["matched"] is True
    assert cands["R100000002-I000001657059"]["matched"] is False
    assert detail["book"]["match"] == "edition"


def test_unknown_asin_in_detail_is_a_lookup_error(library: Path) -> None:
    with pytest.raises(LookupError):
        book_detail(library, "B0000000ZZ", {})


# --- 反映 -----------------------------------------------------------------------------------------------


def test_apply_writes_the_csv_and_fetches_only_the_changed_book_by_isbn(library: Path, tmp_path: Path) -> None:
    _fetched(library)
    ndl = FakeOpenSearch([({"isbn": TOYOTA_ISBN}, rss([TOYOTA_BY_ISBN]))])
    csv_path = tmp_path / "new" / "overrides.csv"

    result = apply_changes(library, csv_path, {TOYOTA: "978-4-478-46037-5"}, _factory(ndl))

    assert csv_path.read_text(encoding="utf-8") == f"asin,isbn\n{TOYOTA},{TOYOTA_ISBN}\n"
    assert ndl.calls == [{"isbn": TOYOTA_ISBN}]
    assert result.changed == [TOYOTA]
    assert [(r["asin"], r["status"], r["match"]) for r in result.results] == [(TOYOTA, "found", "isbn")]
    assert _rows(library, "SELECT method, isbn FROM bib_matches WHERE asin = ?", [TOYOTA]) == [("isbn", TOYOTA_ISBN)]


def test_picking_an_isbn_10_candidate_matches_it_by_isbn(library: Path, tmp_path: Path) -> None:
    _fetched(library)
    hardcover_isbn = book_detail(library, TSUGE, {})["candidates"][0]["isbns"][0]
    ndl = FakeOpenSearch([({"isbn": hardcover_isbn}, rss([TSUGE_HARDCOVER]))])
    apply_changes(library, _csv(tmp_path), {TSUGE: hardcover_isbn}, _factory(ndl))
    assert _rows(library, "SELECT method, candidate_ids FROM bib_matches WHERE asin = ?", [TSUGE]) == [
        ("isbn", ["R100000002-I000001657059"])
    ]


def test_removing_an_override_refetches_the_book_by_title(library: Path, tmp_path: Path) -> None:
    csv_path = _csv(tmp_path, f"{TOYOTA},{TOYOTA_ISBN}\n")
    ndl = FakeOpenSearch([({"isbn": TOYOTA_ISBN}, rss([TOYOTA_BY_ISBN]))])
    run_enrich(library, ndl.client(), overrides_path=csv_path)

    ndl = FakeOpenSearch()
    result = apply_changes(library, csv_path, {TOYOTA: None}, _factory(ndl))
    assert csv_path.read_text(encoding="utf-8") == "asin,isbn\n"
    assert ndl.calls[0] == TOYOTA_QUERY
    assert result.changed == [TOYOTA]
    assert _rows(library, "SELECT count(*) FROM bib_overrides")[0][0] == 0


def test_apply_keeps_other_rows_and_also_applies_rows_edited_by_hand(library: Path, tmp_path: Path) -> None:
    # 蔵書にない ASIN の行と除外の行は残す。手で書き足した行(TSUGE の除外)は、DB に未反映なので一緒に反映する
    csv_path = _csv(tmp_path, f"B0NOTOWNED,{TOYOTA_ISBN}\n{TSUGE},\n")
    assert fixui.pending_csv_changes(csv_path, library) == [TSUGE, "B0NOTOWNED"]
    ndl = FakeOpenSearch([({"isbn": TOYOTA_ISBN}, rss([TOYOTA_BY_ISBN]))])

    result = apply_changes(library, csv_path, {TOYOTA: TOYOTA_ISBN}, _factory(ndl))

    assert csv_path.read_text(encoding="utf-8") == (
        f"asin,isbn\nB0NOTOWNED,{TOYOTA_ISBN}\n{TSUGE},\n{TOYOTA},{TOYOTA_ISBN}\n"
    )
    assert result.changed == [TOYOTA, TSUGE, "B0NOTOWNED"]
    assert _rows(library, "SELECT asin, status FROM bib_fetches ORDER BY asin") == [
        (TOYOTA, "found"), (TSUGE, "excluded")
    ]
    assert fixui.pending_csv_changes(csv_path, library) == []


def test_apply_with_nothing_changed_writes_the_csv_without_fetching(library: Path, tmp_path: Path) -> None:
    ndl = FakeOpenSearch()
    csv_path = tmp_path / "overrides.csv"
    result = apply_changes(library, csv_path, {}, _factory(ndl))
    assert result.changed == [] and ndl.calls == []
    assert csv_path.read_text(encoding="utf-8") == "asin,isbn\n"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({TOYOTA: "978-4-478-46037-4"}, "Invalid ISBN"),
        ({"B0000000ZZ": TOYOTA_ISBN}, "not in the library"),
        ({"B0' OR '1'='1": TOYOTA_ISBN}, "Invalid ASIN"),
        ({TOYOTA: 9784478460375}, "Invalid ISBN"),
        (["not", "a", "dict"], "must be an object"),
    ],
)
def test_apply_rejects_bad_input_before_writing_anything(
    library: Path, tmp_path: Path, changes: object, message: str
) -> None:
    csv_path = tmp_path / "overrides.csv"
    ndl = FakeOpenSearch()
    with pytest.raises(ValueError, match=message):
        apply_changes(library, csv_path, changes, _factory(ndl))
    assert not csv_path.exists() and ndl.calls == []


def test_apply_reports_another_enrich_instead_of_waiting(library: Path, tmp_path: Path) -> None:
    ndl = FakeOpenSearch([({"isbn": TOYOTA_ISBN}, rss([TOYOTA_BY_ISBN]))])
    with _enrich_lock_held_elsewhere(library):
        with pytest.raises(EnrichLockedError):
            apply_changes(library, _csv(tmp_path), {TOYOTA: TOYOTA_ISBN}, _factory(ndl))
    assert ndl.calls == []


def test_current_overrides_come_from_the_db_when_the_csv_does_not_exist(library: Path, tmp_path: Path) -> None:
    csv_path = _csv(tmp_path, f"{TOYOTA},{TOYOTA_ISBN}\n")
    run_enrich(library, FakeOpenSearch().client(), where="FALSE", overrides_path=csv_path)
    assert load_current_overrides(tmp_path / "missing.csv", library) == {TOYOTA: TOYOTA_ISBN}


# --- サーバ ---------------------------------------------------------------------------------------------


@pytest.fixture
def server(library: Path, tmp_path: Path) -> Iterator[FixServer]:
    _fetched(library)
    ndl = FakeOpenSearch([({"isbn": TOYOTA_ISBN}, rss([TOYOTA_BY_ISBN]))])
    srv = FixServer(("127.0.0.1", 0), library, tmp_path / "overrides.csv", _factory(ndl))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _request(srv: FixServer, path: str, *, token: str | None = None, host: str | None = None,
             body: object = None, content_type: str = "application/json") -> tuple[int, str]:
    headers = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token is not None:
        headers["X-Kindb-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_page_embeds_the_token_and_the_api_requires_it(server: FixServer) -> None:
    status, page = _request(server, "/")
    assert status == 200 and server.token in page
    assert _request(server, "/api/state")[0] == 403
    assert _request(server, "/api/state", token="wrong")[0] == 403
    status, body = _request(server, "/api/state", token=server.token)
    assert status == 200 and json.loads(body)["pending"] == []


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_localhost_and_loopback_hosts_are_accepted(server: FixServer, host: str) -> None:
    assert _request(server, "/", host=f"{host}:{server.port}")[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1"])
def test_other_host_headers_are_rejected(server: FixServer, host: str) -> None:
    # DNS リバインディングで、他のサイトからページのトークンを読まれないようにする
    assert _request(server, "/", host=host.format(port=server.port))[0] == 403


def test_apply_over_http(server: FixServer, library: Path) -> None:
    status, body = _request(server, "/api/apply", token=server.token, body={"changes": {TOYOTA: TOYOTA_ISBN}})
    assert status == 200, body
    assert json.loads(body)["changed"] == [TOYOTA]
    status, body = _request(server, f"/api/books/{TOYOTA}", token=server.token)
    assert json.loads(body)["book"]["match"] == "isbn"


def test_apply_requires_json(server: FixServer) -> None:
    status, body = _request(server, "/api/apply", token=server.token, body={"changes": {}}, content_type="text/plain")
    assert status == 400 and "application/json" in body


def test_lookup_and_validation_errors_are_4xx(server: FixServer) -> None:
    assert _request(server, "/api/books/B0000000ZZ", token=server.token)[0] == 404
    assert _request(server, "/api/books?view=bogus", token=server.token)[0] == 400
    assert _request(server, "/api/apply", token=server.token, body={"changes": {TOYOTA: "123"}})[0] == 400


# --- CLI ------------------------------------------------------------------------------------------------


def test_fix_command_prints_the_url_and_stops_on_ctrl_c(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(FixServer, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    csv_path = tmp_path / "mine.csv"
    result = runner.invoke(app, ["fix", "--db", str(library), "--overrides", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert "Serving the fix page at http://127.0.0.1:" in result.stdout
    assert str(csv_path) in result.stdout
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def test_fix_command_refuses_a_broken_csv(library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("must not open a browser"))
    broken = tmp_path / "broken.csv"
    broken.write_text("asin,isbn\nB0000000A1,123\n", encoding="utf-8")
    result = runner.invoke(app, ["fix", "--db", str(library), "--overrides", str(broken)])
    assert result.exit_code == 1
    assert "invalid ISBN" in result.stderr
    assert broken.read_text(encoding="utf-8") == "asin,isbn\nB0000000A1,123\n"


def test_fix_defaults_to_the_csv_enrich_last_used(library: Path, tmp_path: Path) -> None:
    csv_path = _csv(tmp_path)
    run_enrich(library, FakeOpenSearch().client(), where="FALSE", overrides_path=csv_path)
    assert fixui.resolve_overrides_path(library, None) == csv_path.resolve()
    csv_path.unlink()
    assert fixui.resolve_overrides_path(library, None) == library.parent / "overrides.csv"

