# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

kindb は公式 Kindle.zip を DuckDB に取り込み、CLI から Kindle 蔵書と読書セッションを検索・集計するローカルツール。Python パッケージとして実装。

設計・スキーマ・スコープの詳細は @docs/kindb-v0.1-plan.md を参照。

## Commands

```bash
# Install (editable)
pip install -e ".[dev]"

# Run CLI
kindb <subcommand>

# Lint
ruff check .

# Test
pytest
pytest tests/test_import.py::test_specific -v  # single test

# Lint + Test
ruff check . && pytest
```
