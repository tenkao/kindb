# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

kindb は Chrome 拡張などで取得した `kindle.json` を DuckDB に取り込み、Claude Desktop(MCP 経由) や Claude Code / CLI から Kindle 蔵書を検索・集計するローカルツール。Python パッケージとして実装。

設計・スキーマ・スコープの詳細は @docs/kindb-v0.2-plan.md と @docs/kindb-v0.3-plan.md を参照。
使い方・主要ビュー・MCP 設定は @README.md を参照。
生成 AI 向けクエリガイドは @SKILL.md を参照。

## Commands

```bash
# Create / update .venv (Python 3.13 pinned by .python-version, dev group included)
# Do not place the project under iCloud-synced dirs (~/Documents etc.): .pth files created there
# get the macOS hidden flag and Python 3.13+ site.py skips them, breaking the editable install.
uv sync

# Run CLI (dev)
uv run kindb <subcommand>

# Global command (editable; rerun after dependency changes or moving the project)
# uv tool install ignores uv.lock, so pin versions via constraints exported from it.
# Never use `uv tool upgrade kindb`: it keeps the constraints stored at install time.
# --python is required: uv tool install does not read the project's .python-version.
# Dependency update procedure: see README.md「依存更新の手順」
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate --format requirements.txt -o constraints.txt \
  && uv tool install --editable . --reinstall --python 3.13 --constraints constraints.txt

# Check .venv and tool env drift (same output = in sync)
uv run python -c "import sys, duckdb; print(sys.version.split()[0], duckdb.__version__)"
~/.local/share/uv/tools/kindb/bin/python -c "import sys, duckdb; print(sys.version.split()[0], duckdb.__version__)"

# Lint
uv run ruff check .

# Test
uv run pytest
uv run pytest tests/test_import.py::test_import_creates_db -v

# Lint + Test
uv run ruff check . && uv run pytest
```
