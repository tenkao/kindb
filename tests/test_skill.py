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
from kindb.importer import import_official_zip
from tests.create_official_fixture import create_official_zip

SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"
# 手順の箇条書きの中にある、字下げされたブロックも拾う
_SQL_BLOCK = re.compile(r"^([ \t]*)```sql\n(.*?)^\1```", re.MULTILINE | re.DOTALL)

runner = CliRunner()


def _sql_examples() -> list[pytest.ParameterSet]:
    text = SKILL_MD.read_text(encoding="utf-8")
    return [
        pytest.param(textwrap.dedent(m.group(2)).strip(), id=f"SKILL.md:{text.count(chr(10), 0, m.start()) + 1}")
        for m in _SQL_BLOCK.finditer(text)
    ]


def test_skill_md_sql_examples_are_found() -> None:
    # 抽出が壊れて 0 件になると、下の parametrize が何も検証せずに通るため
    assert _sql_examples()


@pytest.fixture
def library(imported_db: Path, tmp_path: Path) -> Path:
    import_official_zip(create_official_zip(tmp_path / "Kindle.zip"), imported_db)
    return imported_db


@pytest.mark.parametrize("sql", _sql_examples())
def test_skill_md_sql_example_runs_via_kindb_query(library: Path, sql: str) -> None:
    result = runner.invoke(app, ["query", sql, "--db", str(library)])
    assert result.exit_code == 0, result.stderr
    assert isinstance(json.loads(result.stdout), list)
