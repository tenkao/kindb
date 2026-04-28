# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

kindb は Chrome 拡張などで取得した `kindle.json` を DuckDB に取り込み、Claude Desktop(MCP 経由) や Claude Code / CLI から Kindle 蔵書を検索・集計するローカルツール。Python パッケージとして実装。

設計・スキーマ・スコープの詳細は @docs/kindb-v0.2-plan.md を参照。
使い方・主要ビュー・MCP 設定は @README.md を参照。
生成 AI 向けクエリガイドは @SKILL.md を参照。

## Commands

```bash
# Create virtualenv
# macOS + Python 3.13: use venv/ instead of .venv/ because hidden .pth files are skipped by site.py.
python3 -m venv venv
source venv/bin/activate

# Install (editable)
pip install -e ".[dev]"

# Run CLI
kindb <subcommand>

# Lint
ruff check .

# Test
pytest
pytest tests/test_import.py::test_import_creates_db -v

# Lint + Test
ruff check . && pytest
```
