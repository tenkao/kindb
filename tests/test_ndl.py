"""Tests for the NDL Search OpenSearch client and RSS parsing. 通信はしない。"""

from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from kindb.matching import is_candidate
from kindb.ndl import DPID, MAX_RESULTS, NdlClient, NdlError, parse_item, parse_response
from tests.ndl_fixtures import FIXTURE_DIR, http_error, rss


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_parse_response_reads_ndl_record_fields() -> None:
    response = parse_response(_fixture("isbn_9784822250850.xml"))
    assert response.total == 2
    ndl, cinii = response.records
    assert ndl.id == "R100000002-I026300125"
    assert ndl.title == "HARD THINGS : 答えがない難問と困難にきみはどう立ち向かうか"
    assert ndl.categories == ("図書", "紙")
    assert ndl.publishers == ("日経BP社", "日経BPマーケティング (発売)")
    assert (ndl.issued, ndl.extent) == ("2015.4", "389p")
    assert ndl.isbns == ("978-4-8222-5085-0",)
    # 型のない dc:subject だけを件名にし、NDLC や NDC は件名に混ぜない
    assert ndl.subjects == ("経営管理",)
    assert ndl.ndcs == (("9", "336"),)
    assert "原タイトル: THE HARD THING ABOUT HARD THINGS" in ndl.descriptions
    # 同じ紙版でも、ほかの提供元の書誌は候補にしない(NDC が 335.13 で食い違い、件名に著者名が入る)
    assert cinii.id.startswith("R100000136-")
    assert is_candidate(ndl) and not is_candidate(cinii)


def test_parse_response_keeps_volume_series_and_non_book_categories() -> None:
    records = {r.id: r for r in parse_response(_fixture("title_seikai_no_monsho_2.xml")).records}
    novel = records["R100000002-I000002498252"]
    assert (novel.volume, novel.series, novel.ndcs) == ("2 (ささやかな戦い)", ("ハヤカワ文庫. JA",), (("8", "913.6"),))
    audio = records["R100000002-I000011152152"]
    assert audio.categories == ("録音資料", "記録メディア")
    assert not is_candidate(audio)


def test_parse_response_with_no_results() -> None:
    response = parse_response(_fixture("no_results.xml"))
    assert (response.total, response.records) == (0, ())


def test_stored_item_drops_library_links_and_parses_back_to_the_same_record() -> None:
    raw = _fixture("isbn_9784822250850.xml")
    assert raw.count("<rdfs:seeAlso") > 10
    record = parse_response(raw).records[0]
    assert "rdfs:seeAlso" not in record.xml
    assert record.xml.startswith("<item>") and record.xml.endswith("</item>")
    assert parse_item(record.xml) == record


@pytest.mark.parametrize("body", ["<rss><channel></channel></rss>", "not xml"])
def test_parse_response_rejects_unexpected_bodies(body: str) -> None:
    with pytest.raises(NdlError):
        parse_response(body)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_client_sends_dpid_and_max_count_and_waits_between_requests() -> None:
    clock = _Clock()
    urls: list[str] = []

    def fetch(url: str, user_agent: str) -> str:
        urls.append(url)
        assert user_agent.startswith("kindb/")
        clock.now += 0.5  # 応答に 0.5 秒かかる
        return rss([])

    client = NdlClient(interval=3.0, fetch=fetch, sleep=clock.sleep, clock=clock)
    client.search({"title": "理想のヒモ生活", "creator": "日月 ネコ"})
    client.search({"title": "理想のヒモ生活"})

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(urls[0]).query))
    assert query == {"dpid": DPID, "title": "理想のヒモ生活", "creator": "日月 ネコ", "cnt": str(MAX_RESULTS)}
    # 1 回目は待たず、2 回目は前回の応答から 3 秒空ける
    assert clock.sleeps == [3.0]


def test_client_retries_429_after_retry_after_and_then_succeeds() -> None:
    clock = _Clock()
    attempts = []

    def fetch(url: str, user_agent: str) -> str:
        attempts.append(url)
        if len(attempts) == 1:
            raise http_error(429, retry_after="7")
        return rss([])

    client = NdlClient(interval=3.0, fetch=fetch, sleep=clock.sleep, clock=clock)
    assert client.search({"title": "x"}).total == 0
    assert len(attempts) == 2
    assert clock.sleeps[0] == 7.0


def test_client_gives_up_after_repeated_429() -> None:
    clock = _Clock()

    def fetch(url: str, user_agent: str) -> str:
        raise http_error(429)

    client = NdlClient(interval=3.0, fetch=fetch, sleep=clock.sleep, clock=clock)
    with pytest.raises(NdlError, match="HTTP 429"):
        client.search({"title": "x"})
    # Retry-After がなければ 60 秒、120 秒、180 秒と待ってから諦める
    assert [s for s in clock.sleeps if s >= 60] == [60.0, 120.0, 180.0]


@pytest.mark.parametrize(
    "error",
    [http_error(500), urllib.error.URLError("no route"), TimeoutError("timed out")],
)
def test_client_reports_other_failures_without_retrying(error: Exception) -> None:
    calls = []

    def fetch(url: str, user_agent: str) -> str:
        calls.append(url)
        raise error

    client = NdlClient(interval=0.0, fetch=fetch, sleep=lambda _: None)
    with pytest.raises(NdlError):
        client.search({"title": "x"})
    assert len(calls) == 1
