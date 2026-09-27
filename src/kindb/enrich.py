"""書誌情報の取得(kindb enrich)と再照合(kindb rematch)。

取得は数時間かかるので、その間は DB を開かない。一定冊数ごとに短い書き込みトランザクションでまとめて書き、
読み取り系コマンドや MCP サーバが DB を開けるようにする。状態の意味と遷移は docs/spec.md にある。
"""

from __future__ import annotations

import csv
import json
import os
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import duckdb

from kindb import sqlguard
from kindb.db import DatabaseLockedError, connect, create_schema, enrich_lock_path
from kindb.matching import (
    Book,
    Match,
    adoptable_records,
    decide_isbn_match,
    decide_match,
    is_candidate,
    isbn_checksum_ok,
    normalize_isbn,
    parse_kindle_title,
    search_stages,
)
from kindb.ndl import MAX_RESULTS, NdlClient, NdlError, NdlRecord, NdlRetryLater, SearchResponse, parse_item

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_INCOMPLETE = "incomplete"
STATUS_EXCLUDED = "excluded"
STATUS_ERROR = "error"

# この冊数ごとに書き込む。中断しても失うのは最大でこの冊数の取得結果だけ
BATCH_SIZE = 20
# 連続してこの冊数で通信に失敗したら、回線や取得先の障害とみなして止める。全冊を error にしないため
MAX_CONSECUTIVE_ERRORS = 5
# 書き込み時にロックが衝突したときの待ち時間の上限(秒)。途中の書き込みは短く諦めて次の書き込みでまとめ直し、
# 最後の書き込みだけ長く待つ。MCP サーバの問い合わせは短いので、ふつうは数秒で空く
BATCH_LOCK_WAIT = 30.0
FINAL_LOCK_WAIT = 300.0

_BIB_TABLES = ("bib_fetches", "bib_candidates", "bib_matches", "bib_subjects", "bib_notes")
_MISSING = object()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EnrichLockedError(Exception):
    """別の kindb enrich か rematch が同じ DB で動いている。"""


@contextmanager
def _exclusive_run(db_path: Path) -> Iterator[None]:
    """enrich と rematch を DB ごとに 1 つだけ動かす。

    2 つ動くと、NDL への問い合わせが直列でなくなり、片方が選んだときの訂正や状態で、もう片方が書いた訂正や
    照合結果を上書きする。DuckDB のロックは書き込みの瞬間しか持たないので、実行の間ずっと持つ flock を別に使う。
    """
    import fcntl  # Windows にはないので、ここで読み込む(動かす前提は macOS と Linux)

    path = enrich_lock_path(db_path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise EnrichLockedError(
                f"Another kindb enrich or rematch is running on {db_path}. Wait for it to finish, then retry."
            ) from e
        yield
    finally:
        os.close(fd)


# --- 手動訂正 -------------------------------------------------------------------------------------------


def load_overrides_csv(path: str | Path) -> dict[str, str | None]:
    """ASIN と ISBN の 2 列の CSV を読む。ISBN が空の行は「照合しない」。違反はまとめて ValueError にする。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Overrides CSV not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        raise ValueError(f"Overrides CSV is empty: {path}")
    header = [h.strip().lower() for h in rows[0]]
    if header != ["asin", "isbn"]:
        raise ValueError(f"Overrides CSV header must be 'asin,isbn': got {','.join(rows[0])}")

    errors: list[str] = []
    overrides: dict[str, str | None] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != 2:
            errors.append(f"line {line_no}: expected 2 columns, got {len(row)}")
            continue
        asin, isbn = row[0].strip(), row[1].strip()
        if not asin:
            errors.append(f"line {line_no}: empty ASIN")
            continue
        if asin in overrides:
            errors.append(f"line {line_no}: duplicate ASIN {asin}")
            continue
        if not isbn:
            overrides[asin] = None
            continue
        # 打ち間違いの ISBN で引くと「見つからない」として残り気づきにくいので、検査数字まで確かめる
        if not isbn_checksum_ok(isbn):
            errors.append(f"line {line_no}: invalid ISBN {isbn} for {asin}")
            continue
        overrides[asin] = normalize_isbn(isbn)
    if errors:
        raise ValueError("; ".join(errors))
    return overrides


@dataclass
class OverrideChanges:
    reset: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    unknown_asins: list[str] = field(default_factory=list)
    total: int = 0


def apply_overrides(
    con: duckdb.DuckDBPyConnection, overrides: dict[str, str | None], source_path: Path, now: datetime
) -> OverrideChanges:
    """訂正を全件置き換え、内容が変わった本の状態を戻す。呼び出し側のトランザクションの中で使う。"""
    old = dict(con.execute("SELECT asin, isbn FROM bib_overrides").fetchall())
    changes = OverrideChanges(total=len(overrides))
    for asin in sorted(set(old) | set(overrides)):
        before = old.get(asin, _MISSING)
        after = overrides.get(asin, _MISSING)
        if before == after:
            continue
        # 保存済みの候補に訂正先の紙版が入っているとは限らず、--where の外で再照合されたときに古い訂正で
        # 取った候補から採用し直さないよう、候補まで消す
        for table in _BIB_TABLES:
            con.execute(f"DELETE FROM {table} WHERE asin = ?", [asin])
        if after is None:
            con.execute(
                "INSERT INTO bib_fetches (asin, status, fetched_at) VALUES (?, ?, ?)",
                [asin, STATUS_EXCLUDED, now],
            )
            changes.excluded.append(asin)
        else:
            changes.reset.append(asin)

    con.execute("DELETE FROM bib_overrides")
    if overrides:
        con.executemany("INSERT INTO bib_overrides (asin, isbn) VALUES (?, ?)", list(overrides.items()))
    known = {row[0] for row in con.execute("SELECT asin FROM books").fetchall()}
    changes.unknown_asins = sorted(set(overrides) - known)
    _ensure_metadata_row(con)
    con.execute(
        "UPDATE bib_metadata SET overrides_source_path = ?, overrides_updated_at = ?",
        [str(source_path.resolve()), now],
    )
    return changes


# --- 取得 ----------------------------------------------------------------------------------------------


@dataclass
class FetchResult:
    asin: str
    status: str
    source: str | None
    stages: list[dict[str, object]]
    candidates: list[NdlRecord]
    match: Match | None
    error: str | None = None


def fetch_book(client: NdlClient, book: Book, override_isbn: str | None = None) -> FetchResult:
    """1 冊を NDL サーチで引いて照合する。通信に失敗したら status が error の結果を返す。"""
    try:
        if override_isbn:
            return _fetch_by_isbn(client, book, override_isbn)
        return _fetch_by_title(client, book)
    except NdlError as e:
        return FetchResult(book.asin, STATUS_ERROR, None, [], [], None, error=str(e))


def _candidates(response: SearchResponse) -> list[NdlRecord]:
    seen: set[str] = set()
    result = []
    for record in response.records:
        if is_candidate(record) and record.id not in seen:
            seen.add(record.id)
            result.append(record)
    return result


def _fetch_by_isbn(client: NdlClient, book: Book, isbn: str) -> FetchResult:
    params = {"isbn": isbn}
    response = client.search(params)
    stages = [{"stage": "isbn", "params": params, "total": response.total}]
    candidates = _candidates(response)
    match = decide_isbn_match(candidates)
    if match is None:
        return FetchResult(book.asin, STATUS_NOT_FOUND, "isbn", stages, [], None)
    return FetchResult(book.asin, STATUS_FOUND, "isbn", stages, candidates, match)


def _fetch_by_title(client: NdlClient, book: Book) -> FetchResult:
    if parse_kindle_title(book.title, book.series_title).split_edition:
        # 分冊版、単話、合本の番号は紙版の巻と対応しないので、照合できない本のために NDL へ問い合わせない
        skipped = [{"stage": "skipped", "reason": "split_edition"}]
        return FetchResult(book.asin, STATUS_NOT_FOUND, "title", skipped, [], None)
    cache: dict[tuple[tuple[str, str], ...], SearchResponse] = {}
    log: list[dict[str, object]] = []
    unresolved = False

    def search(stage: str, params: dict[str, str]) -> SearchResponse:
        key = tuple(sorted(params.items()))
        if key not in cache:
            cache[key] = client.search(params)
        response = cache[key]
        log.append({"stage": stage, "params": params, "total": response.total})
        return response

    for stage in search_stages(book):
        resolved: SearchResponse | None = None
        for params in (stage.params, *stage.refinements):
            response = search(stage.name, params)
            # 総件数が 500 件以下の応答だけを「全部見た」とみなす。途中の検索が超過したことは段の結論に影響しない
            if response.total <= MAX_RESULTS:
                resolved = response
                break
        if resolved is None:
            unresolved = True
            continue
        candidates = _candidates(resolved)
        # 同じ作品の別の巻しか残らない段では止めず、次の段へ進む
        if adoptable_records(book, candidates):
            return FetchResult(
                book.asin, STATUS_FOUND, "title", log, candidates, decide_match(book, candidates)
            )
    status = STATUS_INCOMPLETE if unresolved else STATUS_NOT_FOUND
    return FetchResult(book.asin, status, "title", log, [], None)


# --- 書き込み ------------------------------------------------------------------------------------------

Connector = Callable[[Path], duckdb.DuckDBPyConnection]


def _write_connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return connect(db_path)


def _connect_with_retry(
    db_path: Path,
    max_wait: float,
    *,
    connector: Connector = _write_connect,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[], None] | None = None,
) -> duckdb.DuckDBPyConnection:
    waited = 0.0
    delay = 1.0
    while True:
        try:
            return connector(db_path)
        except DatabaseLockedError:
            if waited >= max_wait:
                raise
            if on_wait and waited == 0.0:
                on_wait()
            pause = min(delay, max_wait - waited)
            sleep(pause)
            waited += pause
            delay = min(delay * 2, 10.0)


def _ensure_metadata_row(con: duckdb.DuckDBPyConnection) -> None:
    if con.execute("SELECT count(*) FROM bib_metadata").fetchone()[0] == 0:
        con.execute("INSERT INTO bib_metadata DEFAULT VALUES")


def _delete_book_rows(con: duckdb.DuckDBPyConnection, asins: list[str], tables: Iterable[str]) -> None:
    if not asins:
        return
    for table in tables:
        con.execute(f"DELETE FROM {table} WHERE list_contains(?, asin)", [asins])


def _insert_match(con: duckdb.DuckDBPyConnection, asin: str, match: Match, now: datetime) -> None:
    con.execute(
        """INSERT INTO bib_matches
           (asin, method, candidate_ids, isbn, paper_issued, publisher, pages, bib_series,
            ndc, ndc_edition, matched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            asin,
            match.method,
            list(match.candidate_ids),
            match.isbn,
            match.paper_issued,
            match.publisher,
            match.pages,
            match.bib_series,
            match.ndc,
            match.ndc_edition,
            now,
        ],
    )
    if match.subjects:
        con.executemany(
            "INSERT INTO bib_subjects (asin, subject_order, subject) VALUES (?, ?, ?)",
            [(asin, i, s) for i, s in enumerate(match.subjects, start=1)],
        )
    if match.notes:
        con.executemany(
            "INSERT INTO bib_notes (asin, note_order, note) VALUES (?, ?, ?)",
            [(asin, i, n) for i, n in enumerate(match.notes, start=1)],
        )


def write_results(
    con: duckdb.DuckDBPyConnection,
    results: list[FetchResult],
    now: datetime,
    *,
    run_started_at: datetime | None = None,
    run_fetched: int | None = None,
) -> None:
    """取得結果を 1 つのトランザクションで書く。import と同じく BEGIN → DELETE → INSERT → COMMIT → CHECKPOINT。"""
    con.execute("BEGIN TRANSACTION")
    try:
        replaced = [r for r in results if r.status != STATUS_ERROR]
        errors = [r for r in results if r.status == STATUS_ERROR]
        previous: dict[str, str] = {}
        if errors:
            previous = dict(
                con.execute(
                    "SELECT asin, status FROM bib_fetches WHERE list_contains(?, asin)", [[r.asin for r in errors]]
                ).fetchall()
            )
        # 引き直しで通信に失敗した本は、前回の状態、候補、照合結果を残す。一時的な失敗で書誌情報を失わないため
        errors = [r for r in errors if previous.get(r.asin) in (None, STATUS_ERROR)]
        _delete_book_rows(con, [r.asin for r in replaced], _BIB_TABLES)
        _delete_book_rows(con, [r.asin for r in errors], ("bib_fetches",))

        fetch_rows = [
            (r.asin, r.status, r.source, json.dumps(r.stages, ensure_ascii=False), r.error, now)
            for r in replaced + errors
        ]
        if fetch_rows:
            con.executemany(
                """INSERT INTO bib_fetches (asin, status, source, stages, error, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                fetch_rows,
            )
        candidate_rows = [
            (r.asin, record.id, rank, record.xml)
            for r in replaced
            for rank, record in enumerate(r.candidates, start=1)
        ]
        if candidate_rows:
            con.executemany(
                "INSERT INTO bib_candidates (asin, candidate_id, search_rank, item_xml) VALUES (?, ?, ?, ?)",
                candidate_rows,
            )
        for r in replaced:
            if r.match is not None:
                _insert_match(con, r.asin, r.match, now)
        _ensure_metadata_row(con)
        con.execute(
            "UPDATE bib_metadata SET last_enrich_at = ?, last_enrich_fetched = ?",
            [run_started_at or now, run_fetched if run_fetched is not None else len(results)],
        )
        con.execute("COMMIT")
    except Exception:
        _rollback_quietly(con)
        raise
    con.execute("CHECKPOINT")


def _rollback_quietly(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("ROLLBACK")
    except duckdb.Error:
        pass


# --- 対象の選択 ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    book: Book
    status: str | None
    override_isbn: str | None


def validate_where(where: str) -> None:
    if sqlguard.has_multiple_statements(f"SELECT asin FROM v_books WHERE {where}"):
        raise ValueError("--where must be a single condition on v_books (no ';').")


def select_targets(
    con: duckdb.DuckDBPyConnection,
    *,
    where: str | None,
    retry_missing: bool,
    refresh: bool,
    skip_fetched_since: datetime | None = None,
) -> list[Target]:
    """skip_fetched_since は、中断した引き直しの開始日時。それ以降に取得した本は引き直し済みとして飛ばす。"""
    condition = f"({where})" if where else "TRUE"
    try:
        rows = con.execute(
            f"""SELECT b.asin, b.title, b.authors_text, b.series_title, f.status,
                       o.asin IS NOT NULL AS has_override, o.isbn, f.fetched_at
                FROM v_books b
                LEFT JOIN bib_fetches f ON f.asin = b.asin
                LEFT JOIN bib_overrides o ON o.asin = b.asin
                WHERE b.asin IN (SELECT asin FROM v_books WHERE {condition})
                ORDER BY b.asin"""
        ).fetchall()
    except duckdb.Error as e:
        raise ValueError(f"Invalid --where condition: {e}") from e

    targets = []
    for asin, title, authors_text, series_title, status, has_override, isbn, fetched_at in rows:
        # 訂正で ISBN を空にした本は、状態によらず取得しない
        if (has_override and isbn is None) or status == STATUS_EXCLUDED:
            continue
        if skip_fetched_since is not None and fetched_at is not None and fetched_at >= skip_fetched_since:
            continue
        if status in (None, STATUS_ERROR):
            pass
        elif status in (STATUS_NOT_FOUND, STATUS_INCOMPLETE):
            if not (retry_missing or refresh):
                continue
        elif status == STATUS_FOUND:
            if not refresh:
                continue
        targets.append(Target(Book(asin, title, authors_text, series_title), status, isbn))
    return targets


# --- 実行 ----------------------------------------------------------------------------------------------


@dataclass
class EnrichSummary:
    targets: int = 0
    fetched: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    interrupted: bool = False
    # 取得を途中で止めた理由。"network"(連続した通信の失敗)か "retry_later"(長い Retry-After)
    aborted: str | None = None
    retry_after: float | None = None
    overrides: OverrideChanges | None = None
    # 中断した引き直しを続けたときの、その引き直しの開始日時
    resumed_from: datetime | None = None


class Reporter:
    """進捗の出力先。CLI が差し替える。"""

    def start(self, targets: int, interval: float) -> None: ...

    def book(self, index: int, total: int, target: Target, result: FetchResult) -> None: ...

    def waiting_for_lock(self) -> None: ...

    def resuming(self, started_at: datetime) -> None: ...


def run_enrich(db_path: Path, client: NdlClient, **options: object) -> EnrichSummary:
    """取得を実行する。同じ DB で別の enrich か rematch が動いていれば EnrichLockedError。"""
    with _exclusive_run(db_path):
        return _run_enrich_locked(db_path, client, **options)


def _run_enrich_locked(
    db_path: Path,
    client: NdlClient,
    *,
    where: str | None = None,
    limit: int | None = None,
    overrides_path: Path | None = None,
    retry_missing: bool = False,
    refresh: bool = False,
    reporter: Reporter | None = None,
    connector: Connector = _write_connect,
    sleep: Callable[[float], None] = time.sleep,
    batch_size: int = BATCH_SIZE,
) -> EnrichSummary:
    reporter = reporter or Reporter()
    if where is not None:
        validate_where(where)
    # 訂正の CSV も --where も、DB を書き換える前に確かめる
    overrides = load_overrides_csv(overrides_path) if overrides_path else None
    summary = EnrichSummary()

    def open_for_write(max_wait: float) -> duckdb.DuckDBPyConnection:
        return _connect_with_retry(
            db_path, max_wait, connector=connector, sleep=sleep, on_wait=reporter.waiting_for_lock
        )

    started_at = _now()
    # 引き直し(--refresh / --retry-missing)は状態だけでは続きが分からないので、開始日時を残して再開に使う
    refetching = refresh or retry_missing

    # 0. スキーマを最新にし、訂正を置き換える(短い書き込みトランザクション)
    with closing(open_for_write(FINAL_LOCK_WAIT)) as con:
        create_schema(con)
        if where is not None:
            try:
                con.execute(f"SELECT asin FROM v_books WHERE ({where}) LIMIT 0")
            except duckdb.Error as e:
                raise ValueError(f"Invalid --where condition: {e}") from e
        if overrides is not None:
            con.execute("BEGIN TRANSACTION")
            try:
                summary.overrides = apply_overrides(con, overrides, overrides_path, _now())
                con.execute("COMMIT")
            except Exception:
                _rollback_quietly(con)
                raise
        if refetching:
            pending = con.execute("SELECT min(started_at) FROM bib_pending_refresh").fetchone()[0]
            if pending is None:
                con.execute("INSERT INTO bib_pending_refresh VALUES (?)", [started_at])
            summary.resumed_from = pending
        con.execute("CHECKPOINT")

    # 1. 読み取り専用で対象を選ぶ
    with closing(connect(db_path, read_only=True)) as con:
        targets = select_targets(
            con,
            where=where,
            retry_missing=retry_missing,
            refresh=refresh,
            skip_fetched_since=summary.resumed_from,
        )
    truncated = bool(limit) and len(targets) > limit
    if limit:
        targets = targets[:limit]
    summary.targets = len(targets)
    if summary.resumed_from is not None:
        reporter.resuming(summary.resumed_from)
    reporter.start(len(targets), client.interval)

    # 2〜4. DB を閉じたまま引き、一定冊数ごとにまとめて書く
    buffer: list[FetchResult] = []
    consecutive_errors = 0
    # 次に書き込みを試す時点のバッファの冊数。書き込みを諦めたら、さらに batch_size 冊引いてから試し直す。
    # 諦めた直後から 1 冊ごとに試すと、ロックが長く続くあいだ 1 冊あたり 30 秒ずつ待つことになる
    next_flush_at = batch_size

    def flush(max_wait: float) -> bool:
        if not buffer:
            return True
        try:
            con = open_for_write(max_wait)
        except DatabaseLockedError:
            if max_wait >= FINAL_LOCK_WAIT:
                raise
            return False  # 結果を持ったまま取得を続け、次の書き込みでまとめて書く
        with closing(con):
            write_results(con, buffer, _now(), run_started_at=started_at, run_fetched=summary.fetched)
        buffer.clear()
        return True

    try:
        for index, target in enumerate(targets, start=1):
            try:
                result = fetch_book(client, target.book, target.override_isbn)
            except NdlRetryLater as e:
                summary.aborted, summary.retry_after = "retry_later", e.seconds
                break
            buffer.append(result)
            summary.fetched += 1
            summary.counts[result.status] = summary.counts.get(result.status, 0) + 1
            reporter.book(index, len(targets), target, result)
            consecutive_errors = consecutive_errors + 1 if result.status == STATUS_ERROR else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                summary.aborted = "network"
                break
            if len(buffer) >= next_flush_at:
                next_flush_at = batch_size if flush(BATCH_LOCK_WAIT) else len(buffer) + batch_size
    except KeyboardInterrupt:
        # Ctrl-C でも、取得済みの結果を書いてから終わる。次の実行はここから再開する
        summary.interrupted = True
    finally:
        flush(FINAL_LOCK_WAIT)

    if refetching and not (summary.interrupted or summary.aborted or truncated):
        # 対象をすべて引き直し終えたときだけ、再開の目印を消す。--limit で打ち切った回は、次の回が続きを引く
        with closing(open_for_write(FINAL_LOCK_WAIT)) as con:
            con.execute("DELETE FROM bib_pending_refresh")
            con.execute("CHECKPOINT")
    return summary


# --- 再照合 --------------------------------------------------------------------------------------------


@dataclass
class RematchSummary:
    books: int = 0
    changed: int = 0
    lost: int = 0


def _match_signature(match: Match | None) -> tuple | None:
    if match is None:
        return None
    return (
        match.method,
        tuple(match.candidate_ids),
        match.isbn,
        match.paper_issued,
        match.publisher,
        match.pages,
        match.bib_series,
        match.ndc,
        match.ndc_edition,
        tuple(match.subjects),
        tuple(match.notes),
    )


def run_rematch(
    db_path: Path,
    *,
    connector: Connector = _write_connect,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[], None] | None = None,
) -> RematchSummary:
    """保存済みの候補と訂正だけで照合をやり直す。通信せず、訂正も変えない。対象は状態が found の本だけ。"""
    with _exclusive_run(db_path):
        return _run_rematch_locked(db_path, connector=connector, sleep=sleep, on_wait=on_wait)


def _run_rematch_locked(
    db_path: Path,
    *,
    connector: Connector,
    sleep: Callable[[float], None],
    on_wait: Callable[[], None] | None,
) -> RematchSummary:
    with closing(connect(db_path, read_only=True)) as con:
        # ISBN の訂正で引いた本かどうかは、訂正の表ではなく取得の記録で決める。訂正を変えると取得の記録も
        # 消えるので同じはずだが、書名で引いた候補に ISBN の規則を当てて全巻を採ることがないようにする
        books = con.execute(
            """SELECT f.asin, b.title, b.authors_text, b.series_title, f.source
               FROM bib_fetches f
               JOIN v_books b ON b.asin = f.asin
               WHERE f.status = ?
               ORDER BY f.asin""",
            [STATUS_FOUND],
        ).fetchall()
        candidate_rows = con.execute(
            """SELECT c.asin, c.item_xml
               FROM bib_candidates c
               JOIN bib_fetches f ON f.asin = c.asin AND f.status = ?
               ORDER BY c.asin, c.search_rank""",
            [STATUS_FOUND],
        ).fetchall()
        current = {
            row[0]: row[1:]
            for row in con.execute(
                """SELECT m.asin, m.method, m.candidate_ids, m.isbn, m.paper_issued, m.publisher, m.pages,
                          m.bib_series, m.ndc, m.ndc_edition,
                          coalesce((SELECT list(subject ORDER BY subject_order) FROM bib_subjects s
                                    WHERE s.asin = m.asin), CAST([] AS VARCHAR[])),
                          coalesce((SELECT list(note ORDER BY note_order) FROM bib_notes n
                                    WHERE n.asin = m.asin), CAST([] AS VARCHAR[]))
                   FROM bib_matches m"""
            ).fetchall()
        }

    candidates: dict[str, list[NdlRecord]] = {}
    for asin, item_xml in candidate_rows:
        candidates.setdefault(asin, []).append(parse_item(item_xml))

    summary = RematchSummary(books=len(books))
    matches: dict[str, Match | None] = {}
    for asin, title, authors_text, series_title, source in books:
        records = candidates.get(asin, [])
        if source == "isbn":
            match = decide_isbn_match(records)
        else:
            match = decide_match(Book(asin, title, authors_text, series_title), records)
        matches[asin] = match
        before = current.get(asin)
        before_sig = (
            (before[0], tuple(before[1]), *before[2:9], tuple(before[9]), tuple(before[10])) if before else None
        )
        if before_sig != _match_signature(match):
            summary.changed += 1
        if match is None:
            summary.lost += 1

    now = _now()
    con = _connect_with_retry(db_path, FINAL_LOCK_WAIT, connector=connector, sleep=sleep, on_wait=on_wait)
    with closing(con):
        con.execute("BEGIN TRANSACTION")
        try:
            _delete_book_rows(con, list(matches), ("bib_matches", "bib_subjects", "bib_notes"))
            for asin, match in matches.items():
                if match is not None:
                    _insert_match(con, asin, match, now)
            _ensure_metadata_row(con)
            con.execute(
                "UPDATE bib_metadata SET last_rematch_at = ?, last_rematch_books = ?", [now, summary.books]
            )
            con.execute("COMMIT")
        except Exception:
            _rollback_quietly(con)
            raise
        con.execute("CHECKPOINT")
    return summary
