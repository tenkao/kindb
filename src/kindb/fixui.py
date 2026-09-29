"""手動訂正の Web UI(`kindb fix`)。127.0.0.1 だけで待ち受け、訂正の CSV を編集して反映する。

反映は `run_enrich` に訂正の CSV と対象の ASIN を渡すだけで、書誌情報の書き込みは enrich と同じ経路を通る。
"""

from __future__ import annotations

import csv
import importlib.resources
import json
import os
import re
import secrets
import tempfile
import traceback
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

from kindb.db import DatabaseLockedError, connect
from kindb.enrich import EnrichLockedError, FetchResult, Reporter, Target, load_overrides_csv, run_enrich
from kindb.matching import isbn_checksum_ok, normalize_isbn
from kindb.ndl import NdlClient, parse_item

# ASIN は英数字だけ。反映の対象を --where の SQL に埋め込むので、この形以外は受け付けない
_ASIN = re.compile(r"^[A-Za-z0-9]{1,20}$")
_MAX_BODY = 1_000_000
_LIST_LIMIT = 500
_MISSING = object()

# 要確認の本: 見つからない、保留、作品だけ、照合が消えた(rematch で候補が合わなくなった)本。通信の失敗(error)は
# 次の enrich が自動で引き直すので含めない
_REVIEW_CONDITION = (
    "bib_status IN ('not_found', 'incomplete') OR bib_match = 'work' "
    "OR (bib_status = 'found' AND bib_match IS NULL)"
)


def resolve_overrides_path(db_path: Path, explicit: str | None) -> Path:
    """編集する訂正の CSV。指定がなければ、enrich が最後に使った CSV、それもなければ DB の隣の overrides.csv。"""
    if explicit:
        return Path(explicit).expanduser()
    with closing(connect(db_path, read_only=True)) as con:
        row = con.execute("SELECT overrides_source_path FROM bib_metadata").fetchone()
    if row and row[0] and Path(row[0]).exists():
        return Path(row[0])
    return db_path.parent / "overrides.csv"


def _db_overrides(con: duckdb.DuckDBPyConnection) -> dict[str, str | None]:
    return dict(con.execute("SELECT asin, isbn FROM bib_overrides").fetchall())


def load_current_overrides(csv_path: Path, db_path: Path) -> dict[str, str | None]:
    """今の訂正。CSV があればそれ(利用者が手で書き足した行も含む)、なければ DB に反映済みの訂正。"""
    if csv_path.exists():
        return load_overrides_csv(csv_path)
    with closing(connect(db_path, read_only=True)) as con:
        return _db_overrides(con)


def write_overrides_csv(path: Path, overrides: dict[str, str | None]) -> None:
    """訂正の CSV を書く。途中で失敗しても元の CSV が壊れないよう、一時ファイルに書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["asin", "isbn"])
            for asin, isbn in overrides.items():
                writer.writerow([asin, isbn or ""])
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _changed_asins(new: dict[str, str | None], old: dict[str, str | None]) -> list[str]:
    return sorted(a for a in set(new) | set(old) if new.get(a, _MISSING) != old.get(a, _MISSING))


def pending_csv_changes(csv_path: Path, db_path: Path) -> list[str]:
    """CSV にあって DB にまだ反映していない訂正の ASIN(利用者が CSV を手で書き換えたとき)。"""
    current = load_current_overrides(csv_path, db_path)
    with closing(connect(db_path, read_only=True)) as con:
        return _changed_asins(current, _db_overrides(con))


def list_books(
    db_path: Path, overrides: dict[str, str | None], *, view: str = "review", query: str = ""
) -> dict[str, object]:
    conditions = []
    params: list[object] = []
    if view == "review":
        conditions.append(f"(({_REVIEW_CONDITION}) OR list_contains(?, asin))")
        params.append(list(overrides))
    elif view != "all":
        raise ValueError(f"Unknown view: {view}")
    if query.strip():
        like = f"%{query.strip()}%"
        conditions.append("(title ILIKE ? OR authors_text ILIKE ? OR asin = ?)")
        params += [like, like, query.strip()]
    where = " AND ".join(conditions) or "TRUE"
    with closing(connect(db_path, read_only=True)) as con:
        total = con.execute(f"SELECT count(*) FROM v_books WHERE {where}", params).fetchone()[0]
        rows = con.execute(
            f"""SELECT asin, title, authors_text, bib_status, bib_match, isbn
                FROM v_books WHERE {where}
                ORDER BY title, asin LIMIT {_LIST_LIMIT}""",
            params,
        ).fetchall()
    books = [
        {
            "asin": asin,
            "title": title,
            "authors": authors,
            "status": status,
            "match": match,
            "isbn": isbn,
            "override": _override_state(overrides, asin),
        }
        for asin, title, authors, status, match, isbn in rows
    ]
    return {"total": total, "books": books}


def _override_state(overrides: dict[str, str | None], asin: str) -> dict[str, object] | None:
    if asin not in overrides:
        return None
    return {"isbn": overrides[asin], "excluded": overrides[asin] is None}


def book_detail(db_path: Path, asin: str, overrides: dict[str, str | None]) -> dict[str, object]:
    with closing(connect(db_path, read_only=True)) as con:
        row = con.execute(
            """SELECT asin, title, authors_text, series_title, product_image_url, acquired_at,
                      bib_status, bib_match, isbn, paper_issued, publisher, pages, ndc, ndc_label
               FROM v_books WHERE asin = ?""",
            [asin],
        ).fetchone()
        if row is None:
            raise LookupError(f"No book with ASIN {asin}")
        fetch = con.execute("SELECT source, fetched_at FROM bib_fetches WHERE asin = ?", [asin]).fetchone()
        match = con.execute("SELECT candidate_ids FROM bib_matches WHERE asin = ?", [asin]).fetchone()
        items = con.execute(
            "SELECT item_xml FROM bib_candidates WHERE asin = ? ORDER BY search_rank", [asin]
        ).fetchall()
    matched_ids = set(match[0]) if match else set()
    candidates = []
    for (xml,) in items:
        record = parse_item(xml)
        isbns = [i for i in (normalize_isbn(x) for x in record.isbns) if i]
        candidates.append(
            {
                "id": record.id,
                "title": record.title,
                "volume": record.volume,
                "edition": record.edition,
                "series": list(record.series),
                "publishers": list(record.publishers),
                "issued": record.issued,
                "extent": record.extent,
                "isbns": isbns,
                "matched": record.id in matched_ids,
                "link": f"https://ndlsearch.ndl.go.jp/books/{record.id}",
            }
        )
    keys = ["asin", "title", "authors", "series_title", "image", "acquired_at", "status", "match", "isbn",
            "paper_issued", "publisher", "pages", "ndc", "ndc_label"]
    book = dict(zip(keys, row))
    book["acquired_at"] = str(book["acquired_at"]) if book["acquired_at"] is not None else None
    book["source"] = fetch[0] if fetch else None
    book["override"] = _override_state(overrides, asin)
    return {"book": book, "candidates": candidates}


@dataclass
class ApplyResult:
    changed: list[str] = field(default_factory=list)
    results: list[dict[str, object]] = field(default_factory=list)
    fetched: int = 0
    failed: int = 0
    aborted: str | None = None
    interrupted: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "results": self.results,
            "fetched": self.fetched,
            "failed": self.failed,
            "aborted": self.aborted,
            "interrupted": self.interrupted,
        }


class _CollectingReporter(Reporter):
    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def book(self, index: int, total: int, target: Target, result: FetchResult) -> None:
        self.results.append(
            {
                "asin": result.asin,
                "title": target.book.title,
                "status": result.status,
                "match": result.match.method if result.match else None,
                "isbn": result.match.isbn if result.match else None,
                "error": result.error,
            }
        )


def apply_changes(
    db_path: Path, csv_path: Path, changes: dict[str, object], client_factory: Callable[[], NdlClient]
) -> ApplyResult:
    """UI の変更(ASIN → ISBN、None は訂正を取り消す)を CSV に書き、訂正が変わった本を引き直す。"""
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object of ASIN to ISBN")
    normalized: dict[str, str | None] = {}
    for asin, isbn in changes.items():
        if not isinstance(asin, str) or not _ASIN.match(asin):
            raise ValueError(f"Invalid ASIN: {asin!r}")
        if isbn is None:
            normalized[asin] = None
            continue
        if not isinstance(isbn, str) or not isbn_checksum_ok(isbn):
            raise ValueError(f"Invalid ISBN for {asin}: {isbn!r}")
        normalized[asin] = normalize_isbn(isbn)

    with closing(connect(db_path, read_only=True)) as con:
        known = {row[0] for row in con.execute("SELECT asin FROM books").fetchall()}
    unknown = sorted(set(normalized) - known)
    if unknown:
        raise ValueError(f"ASINs not in the library: {', '.join(unknown)}")

    # 反映は DB が使用中だと最大 5 分待つ。UI が無言で固まらないよう、先に書き込めるかだけを確かめて断る
    connect(db_path).close()

    overrides = load_current_overrides(csv_path, db_path)
    for asin, isbn in normalized.items():
        if isbn is None:
            overrides.pop(asin, None)
        else:
            overrides[asin] = isbn
    write_overrides_csv(csv_path, overrides)

    with closing(connect(db_path, read_only=True)) as con:
        changed = _changed_asins(overrides, _db_overrides(con))
    result = ApplyResult(changed=changed)
    if not changed:
        return result
    # 手で CSV に書いた ASIN が英数字でなくても訂正としては保存する。引き直しの対象の SQL にだけ入れない
    in_scope = [a for a in changed if _ASIN.match(a)]
    where = "asin IN ({})".format(", ".join(f"'{a}'" for a in in_scope)) if in_scope else "FALSE"
    reporter = _CollectingReporter()
    summary = run_enrich(db_path, client_factory(), where=where, overrides_path=csv_path, reporter=reporter)
    result.results = reporter.results
    result.fetched = summary.fetched
    result.failed = summary.counts.get("error", 0)
    result.aborted = summary.aborted
    result.interrupted = summary.interrupted
    return result


class FixServer(HTTPServer):
    """1 本ずつ要求を処理する(反映を直列にし、同じプロセスで DB を開く接続を重ねないため)。"""

    def __init__(
        self,
        address: tuple[str, int],
        db_path: Path,
        csv_path: Path,
        client_factory: Callable[[], NdlClient],
    ) -> None:
        super().__init__(address, _Handler)
        self.db_path = db_path
        self.csv_path = csv_path
        self.client_factory = client_factory
        # 他のサイトのページから API を呼ばれないよう、ページに埋めたこの値をヘッダで求める
        self.token = secrets.token_urlsafe(24)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


def _page() -> str:
    return (importlib.resources.files("kindb") / "data" / "fix.html").read_text(encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: FixServer

    def log_message(self, format: str, *args: object) -> None:
        # 要求ごとのアクセスログは出さない。反映の結果はページに出る
        pass

    # --- 応答 ---

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # --- 検査 ---

    def _host_allowed(self) -> bool:
        # DNS リバインディング(攻撃者のドメインを 127.0.0.1 に向ける)で、ページのトークンを読まれないようにする
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Kindb-Token", ""), self.server.token)

    # --- 振り分け ---

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header.")
            return
        url = urlparse(self.path)
        if method == "GET" and url.path == "/":
            page = _page().replace("__KINDB_TOKEN__", self.server.token)
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not url.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
            return
        if not self._token_ok():
            self._error(HTTPStatus.FORBIDDEN, "Missing or wrong token. Reload the page.")
            return
        try:
            self._api(method, url.path, parse_qs(url.query))
        except (DatabaseLockedError, EnrichLockedError) as e:
            self._error(HTTPStatus.CONFLICT, str(e))
        except LookupError as e:
            self._error(HTTPStatus.NOT_FOUND, str(e))
        except ValueError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:  # noqa: BLE001 - ページに理由を出し、端末には調べられるようトレースバックを残す
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        server = self.server
        if method == "GET" and path == "/api/state":
            overrides = load_current_overrides(server.csv_path, server.db_path)
            self._json(
                HTTPStatus.OK,
                {
                    "db": str(server.db_path),
                    "overrides_path": str(server.csv_path),
                    "overrides_exists": server.csv_path.exists(),
                    "overrides": overrides,
                    "pending": pending_csv_changes(server.csv_path, server.db_path),
                },
            )
        elif method == "GET" and path == "/api/books":
            overrides = load_current_overrides(server.csv_path, server.db_path)
            view = query.get("view", ["review"])[0]
            text = query.get("q", [""])[0]
            self._json(HTTPStatus.OK, list_books(server.db_path, overrides, view=view, query=text))
        elif method == "GET" and path.startswith("/api/books/"):
            asin = unquote(path[len("/api/books/"):])
            overrides = load_current_overrides(server.csv_path, server.db_path)
            self._json(HTTPStatus.OK, book_detail(server.db_path, asin, overrides))
        elif method == "POST" and path == "/api/apply":
            body = self._read_json()
            result = apply_changes(server.db_path, server.csv_path, body.get("changes"), server.client_factory)
            self._json(HTTPStatus.OK, result.to_json())
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def _read_json(self) -> dict:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("Request body is empty or too large")
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")
        return data
