"""NDL サーチ OpenSearch の呼び出しと、応答(RSS)の解析。

依存を足すと constraints.txt の再生成とツールの再インストールが要るため、標準ライブラリだけで書く。
"""

from __future__ import annotations

import importlib.metadata
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable

BASE_URL = "https://ndlsearch.ndl.go.jp/api/opensearch"
# NDL 自身の書誌だけを引く。JPRO の電子書籍や、同じ紙版でも内容の食い違うほかの提供元の書誌を除ける
# (docs/bibinfo-requirements.md「下調べで確かめた事実」)
DPID = "iss-ndl-opac"
# 1 回で取れる最大件数。501 件目以降は idx を使っても取れない(外部提供インタフェース仕様書 第 1.4 版 §4)
MAX_RESULTS = 500


def _package_version() -> str:
    try:
        return importlib.metadata.version("kindb")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


USER_AGENT = f"kindb/{_package_version()} (+https://github.com/tenkao/kindb)"
# 1.2 秒間隔では約 20 件目で HTTP 429 が返り、3 秒間隔では返らなかった(標本測定)
DEFAULT_INTERVAL = 3.0
MAX_429_RETRIES = 3
MAX_RETRY_AFTER = 600.0

NAMESPACES = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "openSearch": "http://a9.com/-/spec/opensearchrss/1.0/",
    "dcndl": "http://ndl.go.jp/dcndl/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
_XSI_TYPE = f"{{{NAMESPACES['xsi']}}}type"
_ITEM = re.compile(r"<item>.*?</item>", re.DOTALL)
# 所蔵館へのリンクの一覧で、照合には使わない。人気の本では 1 件の半分以上を占めるため、保存する前に除く
_SEE_ALSO = re.compile(r"[ \t]*<rdfs:seeAlso\b[^>]*/>[ \t]*\r?\n?")
_NDC_TYPE = re.compile(r"^dcndl:NDC(\d*)$")
_WRAPPER_OPEN = "<rss " + " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items()) + ">"


class NdlError(Exception):
    """NDL サーチから結果を得られなかった(通信の失敗、HTTP エラー、解析できない応答)。"""


@dataclass(frozen=True)
class NdlRecord:
    """NDL サーチの 1 件の書誌。`xml` は保存する形の <item> で、ほかの属性はそこから解析する。"""

    id: str
    xml: str
    categories: tuple[str, ...]
    title: str
    volume: str | None
    series: tuple[str, ...]
    edition: str | None
    publishers: tuple[str, ...]
    issued: str | None
    extent: str | None
    isbns: tuple[str, ...]
    subjects: tuple[str, ...]
    # (版, 記号)。版は "10" / "9" / "8"、版の書かれていない `dcndl:NDC` は ""
    ndcs: tuple[tuple[str, str], ...]
    descriptions: tuple[str, ...]

    @property
    def is_paper_book(self) -> bool:
        # dpid を付けても録音資料などが混ざるので、図書かつ紙のものだけを紙版の候補にする
        return "図書" in self.categories and "紙" in self.categories


@dataclass(frozen=True)
class SearchResponse:
    total: int
    records: tuple[NdlRecord, ...]


def storable_item_xml(item_xml: str) -> str:
    return _SEE_ALSO.sub("", item_xml)


def parse_item(item_xml: str) -> NdlRecord:
    """保存した <item> を解析する。名前空間の宣言は応答の <rss> にあるので、同じ宣言で包んでから読む。"""
    try:
        root = ET.fromstring(_WRAPPER_OPEN + item_xml + "</rss>")
    except ET.ParseError as e:
        raise NdlError(f"Unparseable NDL record: {e}") from e
    item = root.find("item")
    if item is None:
        raise NdlError("NDL record has no <item>")

    def texts(tag: str) -> tuple[str, ...]:
        values = []
        for element in item.findall(tag, NAMESPACES):
            value = (element.text or "").strip()
            if value:
                values.append(value)
        return tuple(values)

    def first(tag: str) -> str | None:
        values = texts(tag)
        return values[0] if values else None

    link = first("link") or ""
    record_id = link.rstrip("/").rsplit("/", 1)[-1]
    if not record_id:
        raise NdlError("NDL record has no <link>")

    isbns: list[str] = []
    for element in item.findall("dc:identifier", NAMESPACES):
        if element.get(_XSI_TYPE) == "dcndl:ISBN" and (element.text or "").strip():
            isbns.append(element.text.strip())

    subjects: list[str] = []
    ndcs: list[tuple[str, str]] = []
    for element in item.findall("dc:subject", NAMESPACES):
        value = (element.text or "").strip()
        if not value:
            continue
        subject_type = element.get(_XSI_TYPE)
        if subject_type is None:
            # 型のない dc:subject を NDLSH とみなす。SRU の dcndl 形式で 1 件照らした結果に基づく仮定
            subjects.append(value)
            continue
        ndc = _NDC_TYPE.match(subject_type)
        if ndc:
            ndcs.append((ndc.group(1), value))

    return NdlRecord(
        id=record_id,
        xml=item_xml,
        categories=texts("category"),
        title=first("dc:title") or first("title") or "",
        volume=first("dcndl:volume"),
        series=texts("dcndl:seriesTitle"),
        edition=first("dcndl:edition"),
        publishers=texts("dc:publisher"),
        issued=first("dcterms:issued"),
        extent=first("dc:extent"),
        isbns=tuple(isbns),
        subjects=tuple(subjects),
        ndcs=tuple(ndcs),
        descriptions=texts("dc:description"),
    )


def parse_response(xml_text: str) -> SearchResponse:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise NdlError(f"Unparseable NDL response: {e}") from e
    total_text = root.findtext("channel/openSearch:totalResults", namespaces=NAMESPACES)
    if total_text is None or not total_text.strip().isdigit():
        raise NdlError("NDL response has no totalResults")
    records = tuple(parse_item(storable_item_xml(m.group(0))) for m in _ITEM.finditer(xml_text))
    return SearchResponse(total=int(total_text), records=records)


# (URL, User-Agent) を受け取り本文を返す。HTTP エラーは urllib.error.HTTPError で送出する
Fetcher = Callable[[str, str], str]


def _urllib_fetch(url: str, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


class NdlClient:
    """OpenSearch を直列で呼ぶ。前回の問い合わせから interval 秒以上空け、429 は待って再試行する。"""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        fetch: Fetcher = _urllib_fetch,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.interval = interval
        self._fetch = fetch
        self._sleep = sleep
        self._clock = clock
        self._user_agent = user_agent
        self._last_request: float | None = None
        self.request_count = 0

    def search(self, params: dict[str, str]) -> SearchResponse:
        query = {"dpid": DPID, **params, "cnt": str(MAX_RESULTS)}
        url = f"{BASE_URL}?{urllib.parse.urlencode(query)}"
        return parse_response(self._get(url))

    def _wait_for_interval(self) -> None:
        if self._last_request is None:
            return
        remaining = self.interval - (self._clock() - self._last_request)
        if remaining > 0:
            self._sleep(remaining)

    def _get(self, url: str) -> str:
        for attempt in range(MAX_429_RETRIES + 1):
            self._wait_for_interval()
            try:
                self.request_count += 1
                return self._fetch(url, self._user_agent)
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == MAX_429_RETRIES:
                    raise NdlError(f"HTTP {e.code} from NDL Search") from e
                wait = _retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                self._sleep(min(wait if wait is not None else 60.0 * (attempt + 1), MAX_RETRY_AFTER))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise NdlError(f"Could not reach NDL Search: {e}") from e
            finally:
                self._last_request = self._clock()
        raise AssertionError("unreachable")
