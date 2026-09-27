"""NDL サーチの応答を組み立てるテスト用の関数と、通信しない OpenSearch の偽物。

実際の応答の形は tests/fixtures/ndl/ にある NDL サーチの応答(国立国会図書館蔵書、CC BY)に合わせている。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from kindb.ndl import NAMESPACES, NdlClient, NdlRecord, parse_item

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ndl"

_RSS_OPEN = (
    "<rss " + " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items()) + ' version="2.0">'
)


def item_xml(
    record_id: str = "R100000002-I000000001",
    title: str = "書名",
    *,
    volume: str | None = None,
    series: tuple[str, ...] = (),
    edition: str | None = None,
    isbn: str | None = None,
    ndc: tuple[str, str] | None = None,
    subjects: tuple[str, ...] = (),
    descriptions: tuple[str, ...] = (),
    categories: tuple[str, ...] = ("図書", "紙"),
    publishers: tuple[str, ...] = ("出版社",),
    issued: str | None = "2020.1",
    extent: str | None = "200p",
    creators: tuple[str, ...] = (),
) -> str:
    """ndc は (版, 記号)。版が "" なら型が dcndl:NDC の記号になる。"""
    lines = [
        "<item>",
        f"  <title>{escape(title)}</title>",
        f"  <link>https://ndlsearch.ndl.go.jp/books/{record_id}</link>",
        *(f"  <category>{escape(c)}</category>" for c in categories),
        f"  <dc:title>{escape(title)}</dc:title>",
        *(f"  <dc:creator>{escape(c)}</dc:creator>" for c in creators),
    ]
    if volume is not None:
        lines.append(f"  <dcndl:volume>{escape(volume)}</dcndl:volume>")
    if edition is not None:
        lines.append(f"  <dcndl:edition>{escape(edition)}</dcndl:edition>")
    lines += [f"  <dcndl:seriesTitle>{escape(s)}</dcndl:seriesTitle>" for s in series]
    lines += [f"  <dc:publisher>{escape(p)}</dc:publisher>" for p in publishers]
    if issued is not None:
        lines.append(f"  <dcterms:issued>{escape(issued)}</dcterms:issued>")
    if extent is not None:
        lines.append(f"  <dc:extent>{escape(extent)}</dc:extent>")
    if isbn is not None:
        lines.append(f'  <dc:identifier xsi:type="dcndl:ISBN">{escape(isbn)}</dc:identifier>')
    lines += [f"  <dc:subject>{escape(s)}</dc:subject>" for s in subjects]
    if ndc is not None:
        lines.append(f'  <dc:subject xsi:type="dcndl:NDC{ndc[0]}">{escape(ndc[1])}</dc:subject>')
    lines += [f"  <dc:description>{escape(d)}</dc:description>" for d in descriptions]
    lines.append('  <rdfs:seeAlso rdf:resource="https://example.com/library/opac?id=1"/>')
    lines.append("</item>")
    return "\n".join(lines)


def record(record_id: str = "R100000002-I000000001", title: str = "書名", **kwargs: object) -> NdlRecord:
    return parse_item(item_xml(record_id, title, **kwargs))


def rss(items: list[str], total: int | None = None) -> str:
    count = len(items) if total is None else total
    return "\n".join(
        [
            _RSS_OPEN,
            "<channel>",
            "<title>test</title>",
            f"<openSearch:totalResults>{count}</openSearch:totalResults>",
            *items,
            "</channel>",
            "</rss>",
        ]
    )


Route = tuple[dict[str, str], str]


class FakeOpenSearch:
    """検索条件(dpid と cnt を除く)が一致した応答を返す。登録のない検索は 0 件を返す。

    応答に Exception を登録すると、その検索で送出する。呼ばれた検索条件は calls に残る。
    """

    def __init__(self, routes: list[tuple[dict[str, str], str | BaseException]] | None = None) -> None:
        self.routes = list(routes or [])
        self.calls: list[dict[str, str]] = []
        self.user_agents: list[str] = []
        self.on_call: Callable[[int], None] | None = None

    def add(self, params: dict[str, str], response: str | BaseException) -> None:
        self.routes.append((params, response))

    def fetch(self, url: str, user_agent: str) -> str:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        params = {k: v for k, v in query.items() if k not in ("dpid", "cnt")}
        self.calls.append(params)
        self.user_agents.append(user_agent)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        for route_params, response in self.routes:
            if route_params == params:
                if isinstance(response, BaseException):
                    raise response
                return response
        return rss([])

    def client(self) -> NdlClient:
        return NdlClient(interval=0.0, fetch=self.fetch, sleep=lambda _: None)


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://ndlsearch.ndl.go.jp/api/opensearch", code, "error", headers, None)
