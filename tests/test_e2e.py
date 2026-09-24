"""End-to-end tests that run the installed `kindb` command in a separate process.

CliRunner では見えない、エントリポイント、終了コード、stdout と stderr の分離、パイプへの JSON 出力を確かめる。
利用者の主な流れだけを少数で通し、個々の分岐は下位のテストに任せる。

KINDB_E2E_ARTIFACT_DIR を指定すると、実行したコマンドと結果をテストごとの JSON に書き出す。
実行ごとに変わる一時パスと取り込み時刻を置き換え、表の余白と罫線の長さを詰めるので、実行間で diff を取れる。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.create_fixture import create_kindle_json
from tests.create_official_fixture import create_official_zip

KINDB = Path(sys.executable).parent / "kindb"
# import-official は zip を tempfile の一時ディレクトリに展開し、エラーにそのパスが出る
_EXTRACT_DIR = re.compile(re.escape(tempfile.gettempdir()) + r"/tmp\w+")
_IMPORT_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+")


class KindbSession:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.db = tmp_path / "library.duckdb"
        self.records: list[dict[str, Any]] = []
        # 色付けは数字に ANSI コードを挟み、Python の警告は stderr に混ざるので、呼び出し元の設定を持ち込まない
        dropped = {"FORCE_COLOR", "TTY_COMPATIBLE", "PYTHONWARNINGS"}
        inherited = {k: v for k, v in os.environ.items() if k not in dropped}
        # --db を付け忘れても利用者の ~/.kindb に触れないよう、既定の DB も一時ディレクトリに向ける
        self.env = {**inherited, "KINDB_DB_PATH": str(tmp_path / "unused.duckdb"), "COLUMNS": "200"}

    def run(self, *args: str | Path, input: str | None = None) -> subprocess.CompletedProcess[str]:
        argv = [str(KINDB), *map(str, args), "--db", str(self.db)]
        proc = subprocess.run(argv, input=input, capture_output=True, text=True, env=self.env, timeout=60)
        self.records.append(
            {
                "argv": ["kindb", *(self._mask(a) for a in argv[1:])],
                "exit_code": proc.returncode,
                "stdout": self._mask(proc.stdout),
                "stderr": self._mask(proc.stderr),
            }
        )
        return proc

    def query(self, sql: str) -> list[dict[str, Any]]:
        proc = self.run("query", sql)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def _mask(self, text: str) -> str:
        text = _EXTRACT_DIR.sub("<extract>", text.replace(str(self.tmp_path), "<tmp>"))
        text = _IMPORT_TIMESTAMP.sub("<imported_at>", text)
        # 表の幅は一時パスの長さで変わる
        return re.sub(r" {2,}", " ", re.sub(r"([━─])[━─]+", r"\1", text))


@pytest.fixture
def kindb(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[KindbSession]:
    assert KINDB.exists(), f"{KINDB} がない。uv sync でプロジェクトをインストールする"
    session = KindbSession(tmp_path)
    yield session
    artifact_dir = os.environ.get("KINDB_E2E_ARTIFACT_DIR")
    if artifact_dir:
        path = Path(artifact_dir) / f"{request.node.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_library_lifecycle(kindb: KindbSession, tmp_path: Path) -> None:
    kindle_json = create_kindle_json(tmp_path / "kindle.json")
    kindle_zip = create_official_zip(tmp_path / "Kindle.zip")

    imported = kindb.run("import", kindle_json)
    assert imported.returncode == 0, imported.stderr
    assert "Import complete: 5 books" in imported.stdout
    assert imported.stderr == ""

    official = kindb.run("import-official", kindle_zip)
    assert official.returncode == 0, official.stderr
    assert "Genres: 4" in official.stdout

    status = kindb.run("status")
    assert status.returncode == 0, status.stderr
    assert "Official import" in status.stdout

    search = kindb.run("search", "山田")
    assert search.returncode == 0, search.stderr
    assert "B000TEST01" in search.stdout
    assert "B000TEST03" in search.stdout
    assert "Showing 2 of 2 results." in search.stdout

    # AI が Skill の手順で行う問い合わせ: パイプした JSON をそのまま読めること
    assert kindb.query("SELECT count(*) AS n FROM v_books") == [{"n": 5}]
    genre_counts_sql = "SELECT genre, book_count FROM v_genre_counts ORDER BY book_count DESC, genre LIMIT 10"
    genre_counts = kindb.query(genre_counts_sql)
    assert genre_counts == [{"genre": "Fiction", "book_count": 2}, {"genre": "Fantasy", "book_count": 1}]
    assert kindb.query(
        "SELECT asin, series_title, series_asin, series_position FROM v_books "
        "WHERE series_title IS NOT NULL ORDER BY asin LIMIT 10"
    ) == [
        {"asin": "B000TEST01", "series_title": "Series Alpha", "series_asin": "B07D4FP6XQ", "series_position": 1},
        {"asin": "B000TEST02", "series_title": "Series Without Asin", "series_asin": None, "series_position": None},
    ]

    authors = kindb.run("authors", "-n", "1")
    assert authors.returncode == 0, authors.stderr
    assert "山田太郎" in authors.stdout
    assert "Showing 1 of 7 authors. Use -n 0 to show all." in authors.stdout

    recent = kindb.run("recent", "-n", "1")
    assert recent.returncode == 0, recent.stderr
    assert "B000TEST02" in recent.stdout
    assert "B000TEST01" not in recent.stdout

    # kindle.json を取り込み直しても、zip 由来のデータは残る
    assert kindb.run("import", kindle_json).returncode == 0
    assert kindb.query(genre_counts_sql) == genre_counts

    deleted = kindb.run("delete", "--yes")
    assert deleted.returncode == 0, deleted.stderr
    assert not kindb.db.exists()

    after_delete = kindb.run("status")
    assert after_delete.returncode == 1
    assert "No database found" in after_delete.stderr


def test_bad_inputs_and_unsafe_queries_leave_library_intact(kindb: KindbSession, tmp_path: Path) -> None:
    kindle_zip = create_official_zip(tmp_path / "Kindle.zip")
    assert kindb.run("import", create_kindle_json(tmp_path / "kindle.json")).returncode == 0
    assert kindb.run("import-official", kindle_zip).returncode == 0
    snapshot_sql = "SELECT asin, title, genres FROM v_books ORDER BY asin LIMIT 100"
    snapshot = kindb.query(snapshot_sql)

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{bad", encoding="utf-8")
    invalid = kindb.run("import", bad_json)
    assert invalid.returncode == 1
    assert "Invalid JSON" in invalid.stderr
    assert invalid.stdout == ""

    book = {"title": "Dup", "authors": "Author", "acquiredTime": 1704067200000, "readStatus": "UNKNOWN"}
    duplicate_json = create_kindle_json(
        tmp_path / "duplicate.json", [{**book, "asin": "B000DUP01"}, {**book, "asin": "B000DUP01"}]
    )
    duplicate = kindb.run("import", duplicate_json)
    assert duplicate.returncode == 1
    assert "B000DUP01" in duplicate.stderr

    broken_zip = tmp_path / "Broken.zip"
    with zipfile.ZipFile(kindle_zip) as source, zipfile.ZipFile(broken_zip, "w") as dest:
        for name in source.namelist():
            is_genres = "CustomerGenres_FE" in name
            dest.writestr(name, "ASIN,Bad\nB000TEST01,Fiction\n" if is_genres else source.read(name))
    broken = kindb.run("import-official", broken_zip)
    assert broken.returncode == 1
    # "Genre" だけだと、エラーに含まれる CSV のパス(CustomerGenres_FE)にも一致する
    assert "missing Genre" in broken.stderr

    for sql, message in [
        ("DELETE FROM books", "Only SELECT"),
        ("SELECT 1; UPDATE books SET title = 'hacked'", "Only a single SQL statement"),
        ("SELECT asin FROM v_books", "must include a top-level LIMIT"),
    ]:
        rejected = kindb.run("query", sql)
        assert rejected.returncode == 1, sql
        assert message in rejected.stderr, sql
        assert rejected.stdout == "", sql

    assert kindb.query(snapshot_sql) == snapshot
