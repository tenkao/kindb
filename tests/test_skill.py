"""Contract tests for the SQL examples in SKILL.md.

SKILL.md はリポジトリの外で AI が読む手順書で、例の SQL はそのまま kindb query や MCP に渡される。
ビューや列、query の制約を変えたときに、例が壊れていないことをここで守る。
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kindb.cli import app
from kindb.enrich import run_enrich
from kindb.importer import import_official_zip
from tests.create_official_fixture import create_official_zip
from tests.ndl_fixtures import FakeOpenSearch, item_xml, rss

SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"
# 手順の箇条書きの中にある、字下げされたブロックも拾う
_SQL_BLOCK = re.compile(r"^([ \t]*)```sql\n(.*?)^\1```", re.MULTILINE | re.DOTALL)
# 大文字、~~~、情報文字列つきなど、上の抽出が拾えない書き方も含めて SQL ブロックの開始を数える
_ANY_SQL_FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*sql\b", re.MULTILINE | re.IGNORECASE)

runner = CliRunner()


def _sql_examples() -> list[pytest.ParameterSet]:
    text = SKILL_MD.read_text(encoding="utf-8")
    return [
        pytest.param(textwrap.dedent(m.group(2)).strip(), id=f"SKILL.md:{text.count(chr(10), 0, m.start()) + 1}")
        for m in _SQL_BLOCK.finditer(text)
    ]


def test_every_sql_block_in_skill_md_is_checked() -> None:
    # 抽出から漏れたブロックは、下の parametrize で何も検証されないまま通るため
    examples = _sql_examples()
    assert examples
    assert len(examples) == len(_ANY_SQL_FENCE.findall(SKILL_MD.read_text(encoding="utf-8")))


@pytest.fixture
def library(imported_db: Path, tmp_path: Path) -> Path:
    import_official_zip(create_official_zip(tmp_path / "Kindle.zip"), imported_db)
    # 件名、NDC、刊行年、出版社の例が空の結果で通ってしまわないよう、書誌情報を 1 冊に付ける
    ndl = FakeOpenSearch(
        [
            (
                {"title": "テストの本", "creator": "山田太郎"},
                rss([item_xml("R100000002-I000000001", "テストの本", isbn="978-4-06-519067-8", ndc=("10", "336"),
                              subjects=("経営管理",), publishers=("講談社",), issued="2020.4")]),
            )
        ]
    )
    run_enrich(imported_db, ndl.client())
    return imported_db


@pytest.mark.parametrize("sql", _sql_examples())
def test_skill_md_sql_example_runs_via_kindb_query(library: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(library)])
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert isinstance(rows, list)
    if any(column in sql for column in ("subjects", "ndc_label", "paper_issued")) and "B000000000" not in sql:
        assert rows, "書誌情報の例は fixture で 1 行以上返るはず"
