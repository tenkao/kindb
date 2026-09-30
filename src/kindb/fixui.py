"""手動訂正の Web UI(`kindb fix`)。127.0.0.1 だけで待ち受け、訂正の CSV を編集して反映する。

反映は `run_enrich` に訂正の CSV と対象の ASIN を渡すだけで、書誌情報の書き込みは enrich と同じ経路を通る。
"""

from __future__ import annotations

import csv
import importlib.resources
import json
import logging
import os
import re
import secrets
import socket
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

from kindb import sqlguard
from kindb.db import DatabaseLockedError, connect
from kindb.enrich import EnrichLockedError, FetchResult, Reporter, Target, load_overrides_csv, run_enrich
from kindb.matching import isbn_checksum_ok, normalize_isbn
from kindb.ndl import NdlClient, parse_item

logger = logging.getLogger(__name__)

# ASIN は英数字だけ。反映の対象を --where の SQL に埋め込むので、この形以外は受け付けない
_ASIN = re.compile(r"^[A-Za-z0-9]{1,20}$")
_MAX_BODY = 1_000_000
_LIST_LIMIT = 500
# 要求の行の制御文字を \xNN に、\ を \\ にする。標準ライブラリの log_message と同じ扱い(端末のエスケープシーケンスを
# 書かず、元の \xNN と区別する)。log_request を上書きしたので、その変換を通らない
_CONTROL_CHARS = {c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))} | {ord("\\"): "\\\\"}
_MISSING = object()

# 一覧のタブごとに出す状態の区分(label_of の値)。要確認には、反映待ちの本も足す。
# 通信の失敗(error)は次の enrich が自動で引き直すので、要確認に含めない
_VIEW_LABELS: dict[str, frozenset[str] | None] = {
    "review": frozenset({"not_found", "incomplete", "work", "unmatched"}),
    "corrected": frozenset({"isbn", "not_in_ndl", "excluded"}),
    "all": None,
}


def label_of(status: str | None, match: str | None, source: str | None) -> str:
    """一覧と詳細に出す状態の区分。表示名と次にすることは fix.html が持つ。

    タブの絞り込みと画面の表示を同じ区分で決めるため、区分はここだけで決める。
    """
    if status is None:
        return "unfetched"
    if status == "found":
        # 照合結果のない found は、rematch で候補が合わなくなった本
        return match or "unmatched"
    if status == "not_found" and source == "isbn":
        # 訂正の ISBN で引いて見つからなかった本。書名で見つからなかった本とは、次にすることが違う
        return "not_in_ndl"
    return status


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
    # シンボリックリンクなら実体を書き換える。リンクのまま置き換えると、リンクが通常のファイルになり実体は古いまま残る
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["asin", "isbn"])
            for asin, isbn in overrides.items():
                writer.writerow([asin, isbn or ""])
        # mkstemp は 0600 で作るので、元のファイルの権限を引き継ぐ。新しく作るときは umask に従う通常の権限にする
        if path.exists():
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
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
    db_path: Path,
    overrides: dict[str, str | None],
    *,
    view: str = "review",
    query: str = "",
    staged: list[str] | tuple[str, ...] = (),
) -> dict[str, object]:
    """一覧の本。staged は画面でためた変更の ASIN で、CSV の未反映の訂正と合わせて要確認に出す。"""
    if view not in _VIEW_LABELS:
        raise ValueError(f"Unknown view: {view}")
    for asin in staged:
        if not _ASIN.fullmatch(asin):
            raise ValueError(f"Invalid ASIN: {asin!r}")
    condition = "TRUE"
    params: list[object] = []
    if query.strip():
        like = f"%{sqlguard.escape_like(query.strip())}%"
        condition = "(b.title ILIKE ? ESCAPE '\\' OR b.authors_text ILIKE ? ESCAPE '\\' OR b.asin = ?)"
        params = [like, like, query.strip()]
    with closing(connect(db_path, read_only=True)) as con:
        pending = set(_changed_asins(overrides, _db_overrides(con)))
        # タブの絞り込みは label_of で決めるので、検索で絞った本をすべて読んでから Python で絞る(蔵書は数千冊)
        rows = con.execute(
            f"""SELECT b.asin, b.title, b.authors_text, b.bib_status, b.bib_match, b.isbn, f.source
                FROM v_books b LEFT JOIN bib_fetches f ON f.asin = b.asin
                WHERE {condition}
                ORDER BY b.title, b.asin""",
            params,
        ).fetchall()
    labels = _VIEW_LABELS[view]
    waiting = pending | set(staged)
    books = []
    for asin, title, authors, status, match, isbn, source in rows:
        label = label_of(status, match, source)
        if labels is not None and label not in labels and not (view == "review" and asin in waiting):
            continue
        books.append(
            {
                "asin": asin,
                "title": title,
                "authors": authors,
                "label": label,
                "isbn": isbn,
                "pending": asin in pending,
            }
        )
    return {"total": len(books), "books": books[:_LIST_LIMIT]}


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
        applied = _db_overrides(con)
    matched_ids = set(match[0]) if match else set()
    candidates = []
    for (xml,) in items:
        record = parse_item(xml)
        # 13 桁にした値で検査数字を確かめる。反映は検査数字の合わない ISBN を断るので、その候補は選べないと示す
        isbns = [{"isbn": i, "valid": isbn_checksum_ok(i)} for i in (normalize_isbn(x) for x in record.isbns) if i]
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
    book["label"] = label_of(book["status"], book["match"], book["source"])
    book["override"] = _override_state(overrides, asin)
    book["pending"] = overrides.get(asin, _MISSING) != applied.get(asin, _MISSING)
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
    """結果はページに返す。端末(-v。失敗した本は -v なしでも)とログファイルには、enrich と同じ形の行を出す。"""

    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def start(self, targets: int, interval: float) -> None:
        logger.info("Applying: fetching %d books from NDL Search", targets)

    def waiting_for_lock(self) -> None:
        logger.info("Database is in use by another process; waiting to write...")

    def book(self, index: int, total: int, target: Target, result: FetchResult) -> None:
        # 通信に失敗した本は、429 の待ちと同じく -v なしでも端末に出す。ログファイルでは WARNING で探せる
        level = logging.WARNING if result.error else logging.INFO
        error = f" - {result.error}" if result.error else ""
        logger.log(level, "[%d/%d] %s %s %s%s", index, total, result.asin, result.outcome, target.book.title, error)
        self.results.append(
            {
                "asin": result.asin,
                "title": target.book.title,
                "status": result.status,
                "match": result.match.method if result.match else None,
                "label": label_of(result.status, result.match.method if result.match else None, result.source),
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
        if not isinstance(asin, str) or not _ASIN.fullmatch(asin):
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
    original = csv_path.read_bytes() if csv_path.exists() else None
    write_overrides_csv(csv_path, overrides)

    with closing(connect(db_path, read_only=True)) as con:
        changed = _changed_asins(overrides, _db_overrides(con))
    result = ApplyResult(changed=changed)
    if not changed:
        return result
    # 手で CSV に書いた ASIN が英数字でなくても訂正としては保存する。引き直しの対象の SQL にだけ入れない
    in_scope = [a for a in changed if _ASIN.fullmatch(a)]
    where = "asin IN ({})".format(", ".join(f"'{a}'" for a in in_scope)) if in_scope else "FALSE"
    reporter = _CollectingReporter()
    try:
        summary = run_enrich(db_path, client_factory(), where=where, overrides_path=csv_path, reporter=reporter)
    except EnrichLockedError:
        # 別の enrich か rematch が動いていて、訂正を DB に入れる前に断られた。CSV だけが先に進まないよう戻す
        _restore(csv_path, original)
        raise
    result.results = reporter.results
    result.fetched = summary.fetched
    result.failed = summary.counts.get("error", 0)
    result.aborted = summary.aborted
    result.interrupted = summary.interrupted
    if summary.aborted == "retry_later":
        logger.warning(
            "Stopped applying: NDL Search asked to wait %.0f seconds; retry after that", summary.retry_after
        )
    elif summary.aborted:
        logger.warning("Stopped applying after repeated failures to reach NDL Search; retry later")
    return result


def _restore(path: Path, original: bytes | None) -> None:
    if original is None:
        path.resolve().unlink(missing_ok=True)
    else:
        path.resolve().write_bytes(original)


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

    def server_bind(self) -> None:
        port = self.server_address[1]
        if port:
            # HTTPServer は SO_REUSEADDR を付けるので、同じ番号を 0.0.0.0 で待ち受ける他のサービスがあっても
            # 127.0.0.1 に bind でき、そのサービスへのループバックの接続を横取りする。
            # 同じ番号のワイルドカードに bind できるかを先に試す
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("0.0.0.0", port))
        super().server_bind()

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
    # 要求を 1 本ずつ処理するので、何も送らない接続が 1 本あるだけで他の要求がすべて止まらないよう、待ちを区切る
    timeout = 10
    # エラーで応答するときの理由。要求の 1 行に添える
    _error_message: str | None = None

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # 要求ごとの 1 行は、-v とログファイルにだけ出す。反映の結果はページに出る。
        # 応答の本文を送る前に呼ばれるので、ページが結果を受け取った時点でこの行は出ている
        code = code.value if isinstance(code, HTTPStatus) else code
        reason = f" ({self._error_message})" if self._error_message else ""
        logger.info("%s", f"{self.requestline} {code}{reason}".translate(_CONTROL_CHARS))

    def log_message(self, format: str, *args: object) -> None:
        # log_error(待ち切れた接続など)は画面の操作と対応しないので出さない。要求の行は log_request が出す
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
        self._error_message = message
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
        except Exception as e:  # noqa: BLE001 - ページに理由を出し、端末とログファイルには調べられるようトレースバックを残す
            logger.exception("Unexpected error while handling %s %s", method, url.path)
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
            # 画面でためた変更はサーバが知らないので、要確認に足す ASIN を画面から受け取る
            staged = [a for a in query.get("staged", [""])[0].split(",") if a]
            self._json(HTTPStatus.OK, list_books(server.db_path, overrides, view=view, query=text, staged=staged))
        elif method == "GET" and path.startswith("/api/books/"):
            asin = unquote(path[len("/api/books/"):])
            overrides = load_current_overrides(server.csv_path, server.db_path)
            self._json(HTTPStatus.OK, book_detail(server.db_path, asin, overrides))
        elif method == "POST" and path == "/api/apply":
            body = self._read_json()
            result = apply_changes(server.db_path, server.csv_path, body.get("changes"), server.client_factory)
            self._json(HTTPStatus.OK, result.to_json())
            if result.interrupted:
                # 反映中の Ctrl-C は run_enrich が受け止めて結果を返す。結果を返してから、サーバも止める
                raise KeyboardInterrupt
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
