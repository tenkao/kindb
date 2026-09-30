"""kindb CLI."""

from __future__ import annotations

import functools
import json
import logging
import math
import sys
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from kindb import sqlguard
from kindb.db import DatabaseLockedError, connect, enrich_lock_path, ensure_schema, get_db_path, wal_path
from kindb.enrich import EnrichLockedError, FetchResult, Reporter, Target, run_enrich, run_rematch
from kindb.fixui import FixServer, load_current_overrides, resolve_overrides_path
from kindb.importer import import_kindle_json, import_official_zip
from kindb.ndl import DEFAULT_INTERVAL, NdlClient

app = typer.Typer(help="Kindle library manager powered by DuckDB.")
# 書名やパスなどのデータを rich のマークアップや絵文字の記法として解釈させない。解釈すると [Paperback] は黙って消え、
# [/i] で落ち、:smile: は絵文字に化ける。色を付けるラベルは _styled で明示する
console = Console(markup=False, emoji=False)
# パスを含む行は端末幅で折り返さない(soft_wrap)。折り返すとパスの途中に改行が入り、grep や AI の読み取りで切れる。
# console 全体に付けると表のタイトルが中央寄せされなくなるので、表を出さない err_console だけ全体に付け、
# console ではパスを出す行ごとに付ける
err_console = Console(stderr=True, markup=False, emoji=False, soft_wrap=True)


def _styled(label: str, style: str, rest: str = "") -> Text:
    return Text.assemble((label, style), rest)


def _print_error(message: str) -> None:
    err_console.print(_styled("Error:", "red", f" {message}"))


def _report_locked_db(func: Callable[..., None]) -> Callable[..., None]:
    # MCP サーバの問い合わせや別の kindb と重なると起きうるので、トレースバックではなく再実行を促す 1 行にする
    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            func(*args, **kwargs)
        except (DatabaseLockedError, EnrichLockedError) as e:
            _print_error(str(e))
            raise typer.Exit(1)

    return wrapper


def _reject_empty_path(value: str | None) -> str | None:
    # get_db_path は空文字を「指定なし」とみなすので、設定し忘れた変数(--db "$UNSET")で実際の蔵書 DB を黙って使う。
    # 手動テストで一時 DB のつもりが実 DB に対して動いたため。--log-file も同じ誤りを同じ文言で知らせる
    if value is not None and not value.strip():
        raise typer.BadParameter("must not be empty (is the shell variable holding the path set?)")
    return value


def _db_option() -> Path:
    return typer.Option(
        None, "--db", callback=_reject_empty_path, help="Database path (default: ~/.kindb/kindle.duckdb)"
    )


# 詳細ログ(kindb.* の logger)は標準エラーに出す。標準出力は 1 冊ごとの結果と kindb query の JSON に使うので混ぜない
_logger = logging.getLogger("kindb")
# 端末に出した行を --log-file にも残すための logger。-v の標準エラーに二重に出さないよう kindb から切り離す
_output_log = logging.getLogger("kindb.output")
_output_log.propagate = False
# handler が 1 つもないと、WARNING 以上が logging.lastResort から標準エラーにもう一度出る(--log-file なしのとき)。
# pytest は propagate しない logger にも自分の handler を付けるので、テストでは起きない
_output_log.addHandler(logging.NullHandler())


class _ConsoleLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.levelno >= logging.ERROR:
                err_console.print(_styled("Error:", "red", f" {message}"))
            elif record.levelno >= logging.WARNING:
                err_console.print(_styled("Warning:", "yellow", f" {message}"))
            else:
                err_console.print(Text(f"  {message}", style="dim"))
        except Exception:  # noqa: BLE001 - logging の流儀で、出力の失敗で処理を止めない
            self.handleError(record)


class _LogFileHandler(logging.FileHandler):
    """書けなくなったら(ディスクの空きがないなど)、1 回だけ知らせて以後は書かない。取得や反映は続ける。

    logging の既定では、書けなかったレコードごとにトレースバックを標準エラーに出し、進捗の行が読めなくなるため。
    """

    _broken = False

    def emit(self, record: logging.LogRecord) -> None:
        if not self._broken:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        if not self._broken:
            self._broken = True
            err_console.print(_styled("Warning:", "yellow", f" Stopped writing the log file: {sys.exc_info()[1]}"))

    def close(self) -> None:
        try:
            super().close()
        except OSError:
            # 書けなかった残りを閉じる前に flush し、同じ失敗がもう一度起きる。本来の終了コードを置き換えないよう抑える
            pass


@contextmanager
def _logging(verbose: bool, log_file: str | None) -> Iterator[None]:
    """-v なしでは WARNING 以上(429 の待ちなど)だけを端末に出す。ログファイルには INFO 以上と端末に出した行を書く。"""
    console_handler = _ConsoleLogHandler(logging.INFO if verbose else logging.WARNING)
    file_handler = None
    if log_file:
        try:
            # 追記する。中断して再開した回も同じファイルに続けて残す
            file_handler = _LogFileHandler(log_file, encoding="utf-8")
        except OSError as e:
            _print_error(f"Cannot open the log file: {e}")
            raise typer.Exit(1)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    attached = [(_logger, console_handler)]
    if file_handler is not None:
        attached += [(_logger, file_handler), (_output_log, file_handler)]
    previous_level = _logger.level
    _logger.setLevel(logging.INFO)
    for logger, handler in attached:
        logger.addHandler(handler)
    try:
        yield
    except typer.Exit:
        raise
    except (DatabaseLockedError, EnrichLockedError) as e:
        # 端末には _report_locked_db が出す。ログファイルにも、止まった理由を残す
        _output_log.error("%s", e)
        raise
    except Exception:
        # 端末には typer がトレースバックを出す。ログファイルにも残す
        _output_log.exception("Stopped by an unexpected error")
        raise
    finally:
        for logger, handler in attached:
            logger.removeHandler(handler)
        if file_handler is not None:
            file_handler.close()
        _logger.setLevel(previous_level)


def _say(line: str | Text, *, err: bool = False, level: int = logging.INFO, soft_wrap: bool | None = None) -> None:
    """端末に出し、--log-file があればそこにも残す。"""
    (err_console if err else console).print(line, soft_wrap=soft_wrap)
    _output_log.log(level, "%s", line.plain if isinstance(line, Text) else line)


def _say_warning(message: str) -> None:
    err_console.print(_styled("Warning:", "yellow", f" {message}"))
    _output_log.warning("%s", message)


def _say_error(message: str) -> None:
    _print_error(message)
    _output_log.error("%s", message)


def _log_file_option() -> Optional[str]:
    return typer.Option(
        None,
        "--log-file",
        callback=_reject_empty_path,
        help="Also append the output and the --verbose details, with timestamps, to this file",
    )


def _list_limit_option() -> int:
    # Claude Code の Bash は既定 30,000 文字を超える出力を切り詰めるため、一覧は既定で件数を絞る
    return typer.Option(50, "--limit", "-n", min=0, help="Maximum rows to show (0 = all)")


def _print_shown_total(shown: int, total: int, noun: str) -> None:
    # 切り詰めに気づけるよう、表示件数と総件数を必ず出す
    message = f"Showing {shown} of {total} {noun}."
    if shown < total:
        message += " Use -n 0 to show all."
    console.print(message)


def _add_column(table: Table, name: str, **kwargs: object) -> None:
    # rich の既定は単語で折り返し、収まらない語を「…」で切る。空白のない日本語の書名は丸ごと 1 語になり、
    # Claude Code の Bash(COLUMNS を子プロセスに渡さず、パイプなので 80 桁)では途中で切れるため、文字単位で折り返す
    table.add_column(name, overflow="fold", **kwargs)


def _require_db(db: str | None) -> Path:
    db_path = get_db_path(db)
    if not db_path.exists():
        err_console.print(_styled("No database found.", "yellow", " Run 'kindb import' first."))
        raise typer.Exit(1)
    ensure_schema(db_path)
    return db_path


@app.command("import")
@_report_locked_db
def import_cmd(
    json_path: str = typer.Argument(..., help="Path to kindle.json"),
    db: Optional[str] = _db_option(),
) -> None:
    """Import kindle.json into the database."""
    db_path = get_db_path(db)
    try:
        result = import_kindle_json(
            json_path,
            db_path,
            warn=lambda msg: err_console.print(_styled("Warning:", "yellow", f" {msg}")),
        )
        console.print(_styled("Import complete:", "green", f" {result['books_count']} books"))
        console.print(f"Database: {result['db_path']}", soft_wrap=True)
    except (FileNotFoundError, ValueError) as e:
        _print_error(str(e))
        raise typer.Exit(1)


@app.command("import-official")
@_report_locked_db
def import_official_cmd(
    zip_path: str = typer.Argument(..., help="Path to official Kindle.zip"),
    db: Optional[str] = _db_option(),
) -> None:
    """Import optional official Kindle.zip metadata."""
    db_path = get_db_path(db)
    try:
        result = import_official_zip(zip_path, db_path)
        console.print(_styled("Official import complete", "green"))
        console.print(f"Genres: {result['genres_count']}")
        console.print(f"Series: {result['series_count']}")
        console.print(f"Author IDs: {result['author_ids_count']}")
        console.print(f"Author names: {result['author_names_count']}")
        console.print(f"Official ASIN: {result['distinct_asin_count']}")
        console.print(f"Database: {result['db_path']}", soft_wrap=True)
    except (FileNotFoundError, ValueError) as e:
        _print_error(str(e))
        raise typer.Exit(1)


@app.command()
@_report_locked_db
def status(db: Optional[str] = _db_option()) -> None:
    """Show database status."""
    db_path = _require_db(db)

    con = connect(db_path, read_only=True)
    try:
        meta = con.execute(
            "SELECT source_path, source_type, books_count, imported_at FROM import_metadata LIMIT 1"
        ).fetchone()
        books = con.execute("SELECT count(*) FROM books").fetchone()[0]
        authors = con.execute("SELECT count(DISTINCT author_name) FROM book_authors").fetchone()[0]
        images = con.execute("SELECT count(*) FROM books WHERE product_image_url IS NOT NULL").fetchone()[0]
        statuses = con.execute(
            "SELECT read_status, count(*) FROM books GROUP BY read_status ORDER BY read_status"
        ).fetchall()
        official_meta = con.execute(
            """SELECT source_path, source_type, genres_count, series_count, author_ids_count,
                      author_names_count, distinct_asin_count, imported_at
               FROM import_metadata_official
               LIMIT 1"""
        ).fetchone()

        table = Table(title="kindb status")
        _add_column(table, "Item", style="bold")
        _add_column(table, "Value")
        if meta:
            table.add_row("Last import", str(meta[3]))
            table.add_row("Source", meta[0])
            table.add_row("Source type", meta[1])
        table.add_row("Books", str(books))
        table.add_row("Authors", str(authors))
        for read_status, count in statuses:
            table.add_row(f"Read status: {read_status}", str(count))
        table.add_row("With image URL", str(images))
        if official_meta:
            table.add_row("Official import", str(official_meta[7]))
            table.add_row("Official source", official_meta[0])
            table.add_row("Genres (rows)", str(official_meta[2]))
            table.add_row("Series (rows)", str(official_meta[3]))
            table.add_row("Author IDs (rows)", str(official_meta[4]))
            table.add_row("Author names (rows)", str(official_meta[5]))
            table.add_row("Official ASIN (uniq)", str(official_meta[6]))
        for label, value in _bib_status_rows(con):
            table.add_row(label, value)
        table.add_row("Database", str(db_path))
        console.print(table)
    finally:
        con.close()


def _bib_status_rows(con) -> list[tuple[str, str]]:
    # 蔵書から消えた本の書誌情報はテーブルに残るので、現役の本だけを数える
    rows: list[tuple[str, str]] = []
    not_fetched = con.execute(
        "SELECT count(*) FROM books b WHERE NOT EXISTS (SELECT 1 FROM bib_fetches f WHERE f.asin = b.asin)"
    ).fetchone()[0]
    rows.append(("Bib not fetched", str(not_fetched)))
    for bib_status, count in con.execute(
        """SELECT f.status, count(*) FROM bib_fetches f JOIN books b ON b.asin = f.asin
           GROUP BY f.status ORDER BY f.status"""
    ).fetchall():
        rows.append((f"Bib status: {bib_status}", str(count)))
    for method, count in con.execute(
        """SELECT m.method, count(*) FROM bib_matches m JOIN books b ON b.asin = m.asin
           GROUP BY m.method ORDER BY m.method"""
    ).fetchall():
        rows.append((f"Bib match: {method}", str(count)))
    unmatched = con.execute(
        """SELECT count(*) FROM bib_fetches f JOIN books b ON b.asin = f.asin
           WHERE f.status = 'found' AND NOT EXISTS (SELECT 1 FROM bib_matches m WHERE m.asin = f.asin)"""
    ).fetchone()[0]
    if unmatched:
        # rematch で採用できる候補がなくなった本。--where で絞って引き直せば、直した規則で検索の段からやり直せる。
        # 絞らずに --refresh を案内すると、全冊を数時間かけて引き直してしまう
        hint = "kindb enrich --refresh --where \"bib_status = 'found' AND bib_match IS NULL\""
        rows.append(("Bib found, unmatched", f"{unmatched} (rerun: {hint})"))
    overrides = con.execute(
        "SELECT count(*) FROM bib_overrides o JOIN books b ON b.asin = o.asin"
    ).fetchone()[0]
    pending = con.execute(
        """SELECT p.mode, p.where_clause, p.started_at, (SELECT count(*) FROM bib_refetch_done)
           FROM bib_refetch_pending p LIMIT 1"""
    ).fetchone()
    if pending:
        # 中断した引き直し。同じ指定で実行し直すと続きから引く
        mode, where_clause, started_at, done = pending
        scope = f"--{mode.replace('_', '-')}" + (f" --where {where_clause!r}" if where_clause else "")
        rows.append(("Bib refetch unfinished", f"{scope} since {started_at}, {done} books done"))
    if overrides:
        rows.append(("Bib overrides", str(overrides)))
    meta = con.execute("SELECT last_enrich_at, last_rematch_at FROM bib_metadata LIMIT 1").fetchone()
    if meta and meta[0]:
        rows.append(("Last enrich", str(meta[0])))
    if meta and meta[1]:
        rows.append(("Last rematch", str(meta[1])))
    return rows


@app.command()
@_report_locked_db
def search(
    term: str = typer.Argument(..., help="Search term"),
    limit: int = _list_limit_option(),
    db: Optional[str] = _db_option(),
) -> None:
    """Search books by title, authors, ASIN, read status, or NDL subject."""
    db_path = _require_db(db)

    con = connect(db_path, read_only=True)
    try:
        like = f"%{sqlguard.escape_like(term)}%"
        params: list = [like, like, like, like, like]
        # 件名は表に出さない。列を足すと 80 桁の表で書名がさらに細く折り返されるため
        where = r"""FROM v_books
               WHERE title ILIKE ? ESCAPE '\'
                  OR authors_text ILIKE ? ESCAPE '\'
                  OR asin ILIKE ? ESCAPE '\'
                  OR read_status ILIKE ? ESCAPE '\'
                  OR EXISTS (SELECT 1 FROM bib_subjects bs
                             WHERE bs.asin = v_books.asin AND bs.subject ILIKE ? ESCAPE '\')"""
        total = con.execute(f"SELECT count(*) {where}", params).fetchone()[0]
        if total == 0:
            console.print("No results found.")
            return

        # 表紙 URL は表を折り返して 1 冊を数行に広げるため出さない。必要なら kindb query で選ぶ
        sql = f"SELECT asin, title, authors, read_status, acquired_at {where} ORDER BY title ASC, asin ASC"
        if limit:
            sql += " LIMIT ?"
            params = [*params, limit]
        rows = con.execute(sql, params).fetchall()

        table = Table(title=f"Search: {term}")
        _add_column(table, "ASIN", style="dim")
        _add_column(table, "Title")
        _add_column(table, "Authors")
        _add_column(table, "Status")
        _add_column(table, "Acquired")
        for row in rows:
            table.add_row(row[0], row[1], _format_value(row[2]), row[3], _format_value(row[4]))
        console.print(table)
        _print_shown_total(len(rows), total, "results")
    finally:
        con.close()


@app.command()
@_report_locked_db
def query(
    sql: str = typer.Argument(..., help="SQL query"),
    table: bool = typer.Option(False, "--table", "-t", help="Output as table instead of JSON"),
    allow_unlimited: bool = typer.Option(
        False,
        "--allow-unlimited",
        help="Allow SELECT/WITH queries without a top-level LIMIT.",
    ),
    db: Optional[str] = _db_option(),
) -> None:
    """Run a read-only SQL query."""
    db_path = _require_db(db)

    if not sqlguard.is_allowed_statement(sql):
        _print_error("Only SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, PRAGMA statements are allowed.")
        raise typer.Exit(1)
    if sqlguard.has_multiple_statements(sql):
        _print_error("Only a single SQL statement is allowed.")
        raise typer.Exit(1)
    if not allow_unlimited and sqlguard.requires_limit(sql) and not sqlguard.has_safe_limit_or_aggregate(sql):
        _print_error(
            "SELECT/WITH queries must include a top-level LIMIT, for example "
            "`SELECT title FROM v_books ORDER BY title LIMIT 100 OFFSET 0`. "
            "Use `--allow-unlimited` only when you intentionally want an unlimited result."
        )
        raise typer.Exit(1)

    con = connect(db_path, read_only=True)
    try:
        result = con.execute(sql)
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()

        if table:
            t = Table()
            for col in columns:
                _add_column(t, col)
            for row in rows:
                t.add_row(*[_format_value(v) for v in row])
            console.print(t)
        else:
            data = [dict(zip(columns, row)) for row in rows]
            # rich を通すと端末幅で改行が入り JSON が壊れ、[bold] 等もマークアップとして消えるため素の stdout に書く
            typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    finally:
        con.close()


def _format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value)


@app.command()
@_report_locked_db
def authors(
    limit: int = _list_limit_option(),
    db: Optional[str] = _db_option(),
) -> None:
    """Show authors by book count."""
    sql = """SELECT author_name, book_count
           FROM v_author_counts
           ORDER BY book_count DESC, author_name ASC"""
    _run_table_query(
        db,
        sql + (" LIMIT ?" if limit else ""),
        title="Authors",
        columns=[("Author", {}), ("Books", {"justify": "right"})],
        params=[limit] if limit else None,
        total=("SELECT count(*) FROM v_author_counts", "authors"),
    )


@app.command()
@_report_locked_db
def recent(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of books to show"),
    db: Optional[str] = _db_option(),
) -> None:
    """Show recently acquired books."""
    # 表紙 URL は折り返すと数行にまたがって読み取りにくいため、search と同じく出さない。必要なら kindb query で選ぶ
    _run_table_query(
        db,
        """SELECT asin, title, authors, read_status, acquired_at
           FROM v_books
           ORDER BY acquired_at DESC, asin DESC
           LIMIT ?""",
        title="Recent Books",
        columns=[
            ("ASIN", {"style": "dim"}),
            ("Title", {}),
            ("Authors", {}),
            ("Status", {}),
            ("Acquired", {}),
        ],
        params=[limit],
    )


def _ndl_client(interval: float) -> NdlClient:
    return NdlClient(interval=interval)


class _ConsoleReporter(Reporter):
    def start(self, targets: int, interval: float) -> None:
        _say(
            f"Fetching {targets} books from NDL Search, one request every {interval:g}s or more. "
            "Press Ctrl-C to stop; progress is saved and the next run resumes."
        )

    def book(self, index: int, total: int, target: Target, result: FetchResult) -> None:
        line = Text.assemble(
            (f"[{index}/{total}] ", "dim"), f"{target.book.asin} {result.outcome} {target.book.title}"
        )
        if result.error:
            line.append(f" - {result.error}", style="red")
        # ログファイルで失敗した本を探せるよう、通信に失敗した行は WARNING で残す
        _say(line, level=logging.WARNING if result.error else logging.INFO, soft_wrap=True)

    def waiting_for_lock(self) -> None:
        _say("Database is in use by another process; waiting to write...", err=True)

    def resuming(self, started_at: object, skipped: int) -> None:
        _say(f"Resuming the refetch started at {started_at}; skipping {skipped} books already refetched.")

    def discarding(self, mode: str, where: str, started_at: object) -> None:
        scope = f"--{mode.replace('_', '-')}" + (f" --where {where!r}" if where else "")
        _say(f"Discarding the unfinished refetch ({scope}, started at {started_at}); starting over.")


@app.command()
@_report_locked_db
def enrich(
    where: Optional[str] = typer.Option(
        None, "--where", help="SQL condition on v_books selecting the books to fetch, e.g. \"genres = []\""
    ),
    limit: int = typer.Option(0, "--limit", "-n", min=0, help="Maximum books to fetch in this run (0 = all)"),
    overrides: Optional[str] = typer.Option(
        None, "--overrides", help="CSV with columns asin,isbn. Replaces all saved overrides; empty isbn = do not match"
    ),
    retry_missing: bool = typer.Option(
        False,
        "--retry-missing",
        help="Also refetch books that were not found or left incomplete (unfetched books are included too).",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Refetch the selected books regardless of their state (except excluded). Narrow it with --where.",
    ),
    interval: float = typer.Option(
        DEFAULT_INTERVAL, "--interval", min=DEFAULT_INTERVAL, help="Seconds between requests to NDL Search"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Also show each request to NDL Search and each database save (on stderr)"
    ),
    log_file: Optional[str] = _log_file_option(),
    db: Optional[str] = _db_option(),
) -> None:
    """Fetch bibliographic data from NDL Search and match it to books."""
    if not math.isfinite(interval):
        # nan は「3.0 未満」の検査を通り、待ち時間なしで問い合わせてしまう
        raise typer.BadParameter("must be a finite number of seconds", param_hint="--interval")
    db_path = _require_db(db)
    with _logging(verbose, log_file):
        _run_enrich_command(
            db_path,
            _ndl_client(interval),
            where=where,
            limit=limit or None,
            overrides_path=Path(overrides) if overrides else None,
            retry_missing=retry_missing,
            refresh=refresh,
        )


def _run_enrich_command(db_path: Path, client: NdlClient, **options: object) -> None:
    try:
        summary = run_enrich(db_path, client, reporter=_ConsoleReporter(), **options)
    except (FileNotFoundError, ValueError) as e:
        _say_error(str(e))
        raise typer.Exit(1)

    if summary.overrides is not None:
        changes = summary.overrides
        _say(
            f"Overrides: {changes.total} rows saved; {len(changes.reset)} books reset, "
            f"{len(changes.excluded)} books excluded."
        )
        if changes.unknown_asins:
            _say_warning(f"overrides for ASINs not in the library: {', '.join(changes.unknown_asins)}")
    counts = ", ".join(f"{status} {count}" for status, count in sorted(summary.counts.items()))
    _say(f"Fetched {summary.fetched} of {summary.targets} books" + (f": {counts}." if counts else "."))
    if summary.interrupted:
        line = _styled("Interrupted.", "yellow", " Saved the books fetched so far; rerun to resume.")
        _say(line, err=True, level=logging.WARNING)
        raise typer.Exit(130)
    if summary.aborted == "retry_later":
        _say_error(
            f"NDL Search asked to wait {summary.retry_after:.0f} seconds. "
            "Stopped and saved the books fetched so far; retry after that."
        )
        raise typer.Exit(1)
    if summary.aborted:
        _say_error("Stopped after repeated failures to reach NDL Search. Saved the books fetched so far; retry later.")
        raise typer.Exit(1)
    failed = summary.counts.get("error", 0)
    if failed:
        _say_warning(f"{failed} books could not be fetched from NDL Search; rerun the same command to retry them.")


@app.command()
@_report_locked_db
def rematch(db: Optional[str] = _db_option()) -> None:
    """Redo matching from saved candidates without contacting NDL Search."""
    db_path = _require_db(db)
    summary = run_rematch(
        db_path, on_wait=lambda: err_console.print("Database is in use by another process; waiting to write...")
    )
    console.print(
        f"Rematched {summary.books} books: {summary.changed} changed, {summary.lost} without a match."
    )


@app.command()
@_report_locked_db
def fix(
    db: Optional[str] = _db_option(),
    overrides: Optional[str] = typer.Option(
        None,
        "--overrides",
        help="Overrides CSV to edit (default: the one enrich last used, else overrides.csv next to the database)",
    ),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Port on 127.0.0.1 (0 = any free port)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL without opening a browser"),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Also show each page request, each book applied, and each request to NDL Search (on stderr)",
    ),
    log_file: Optional[str] = _log_file_option(),
) -> None:
    """Open a local web page to fix bibliographic matches by hand."""
    db_path = _require_db(db)
    csv_path = resolve_overrides_path(db_path, overrides)
    with _logging(verbose, log_file):
        try:
            # 壊れた CSV を UI で上書きして手書きの行を失わないよう、起動時に読めることを確かめる
            load_current_overrides(csv_path, db_path)
            server = FixServer(("127.0.0.1", port), db_path, csv_path, lambda: _ndl_client(DEFAULT_INTERVAL))
        except (ValueError, OSError) as e:
            _say_error(str(e))
            raise typer.Exit(1)
        _say(f"Serving the fix page at {server.url}", soft_wrap=True)
        _say(f"Overrides CSV: {csv_path}", soft_wrap=True)
        _say("Press Ctrl-C to stop.")
        if not no_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            _say("Stopped.")
        finally:
            server.server_close()


@app.command()
def delete(
    db: Optional[str] = _db_option(),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete the database."""
    db_path = get_db_path(db)
    if not db_path.exists() and not wal_path(db_path).exists():
        enrich_lock_path(db_path).unlink(missing_ok=True)
        console.print("No database to delete.")
        return

    if not yes:
        confirm = typer.confirm(f"Delete {db_path}?")
        if not confirm:
            console.print("Cancelled.")
            return

    db_path.unlink(missing_ok=True)
    wal_path(db_path).unlink(missing_ok=True)
    enrich_lock_path(db_path).unlink(missing_ok=True)
    console.print(_styled("Deleted:", "green", f" {db_path}"), soft_wrap=True)


def _run_table_query(
    db: str | None,
    sql: str,
    *,
    title: str,
    columns: list[tuple[str, dict[str, str]]],
    params: list | None = None,
    total: tuple[str, str] | None = None,
) -> None:
    """total は (総件数を数える SQL, 件数表示の名詞)。渡すと表の後に表示件数と総件数を出す。"""
    db_path = _require_db(db)
    con = connect(db_path, read_only=True)
    try:
        rows = con.execute(sql, params or []).fetchall()
        if not rows:
            console.print("No data.")
            return

        table = Table(title=title)
        # 列ごとの指定は Table.add_column の引数名で渡す。位置で justify と決め打つと、style の指定が justify に化ける
        for name, options in columns:
            _add_column(table, name, **options)
        for row in rows:
            table.add_row(*[_format_value(v) for v in row])
        console.print(table)
        if total is not None:
            total_sql, noun = total
            _print_shown_total(len(rows), con.execute(total_sql).fetchone()[0], noun)
    finally:
        con.close()


if __name__ == "__main__":
    app()
