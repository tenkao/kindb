"""SQL の文字列検査。`kindb query` の制約と `kindb enrich --where` の検査で共有する。

判定は構文解析をしない文字列ベースの近似で、依存を増やさないことを優先している(理由は docs/spec.md)。
"""

from __future__ import annotations

import re

_ALLOWED_SQL = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN|PRAGMA)\b", re.IGNORECASE)
_LIMIT_AT_END = re.compile(r"\blimit\s+\d+\s*(?:offset\s+\d+\s*)?$", re.IGNORECASE)
_AGGREGATE_EXPR = re.compile(
    r"^(count|sum|avg|min|max)\s*\([^()]*\)\s*(?:(?:as\s+)?[a-z_][a-z0-9_]*)?$",
    re.IGNORECASE,
)
_LIMIT_REQUIRED_SQL = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def is_allowed_statement(sql: str) -> bool:
    return bool(_ALLOWED_SQL.match(sql))


def requires_limit(sql: str) -> bool:
    return bool(_LIMIT_REQUIRED_SQL.match(sql))


def has_safe_limit_or_aggregate(sql: str) -> bool:
    stripped = strip_literals_and_comments(sql)
    return _has_top_level_limit(stripped) or _is_simple_aggregate_query(stripped)


def has_multiple_statements(sql: str) -> bool:
    normalized = strip_literals_and_comments(sql).strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return ";" in normalized


def strip_literals_and_comments(sql: str) -> str:
    chars: list[str] = []
    i = 0
    in_single_quote = False
    in_double_quote = False
    while i < len(sql):
        char = sql[i]
        next_char = sql[i + 1] if i + 1 < len(sql) else ""

        if in_single_quote:
            if char == "'" and next_char == "'":
                chars.extend("  ")
                i += 2
                continue
            if char == "'":
                in_single_quote = False
            chars.append(" ")
            i += 1
            continue

        if in_double_quote:
            if char == '"' and next_char == '"':
                chars.extend("  ")
                i += 2
                continue
            if char == '"':
                in_double_quote = False
            chars.append(" ")
            i += 1
            continue

        if char == "-" and next_char == "-":
            chars.extend("  ")
            i += 2
            while i < len(sql) and sql[i] not in "\r\n":
                chars.append(" ")
                i += 1
            continue

        if char == "/" and next_char == "*":
            chars.extend("  ")
            i += 2
            while i < len(sql):
                if sql[i] == "*" and i + 1 < len(sql) and sql[i + 1] == "/":
                    chars.extend("  ")
                    i += 2
                    break
                chars.append(" ")
                i += 1
            continue

        if char == "'":
            in_single_quote = True
            chars.append(" ")
        elif char == '"':
            in_double_quote = True
            chars.append(" ")
        else:
            chars.append(char)
        i += 1

    return "".join(chars)


def _has_top_level_limit(sql: str) -> bool:
    normalized = sql.strip()
    while normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return bool(_LIMIT_AT_END.search(normalized))


def _is_simple_aggregate_query(sql: str) -> bool:
    if _has_top_level_group_by(sql) or _has_top_level_set_operation(sql):
        return False

    select_start = _find_top_level_keyword(sql, "select")
    if select_start is None:
        return False
    from_start = _find_top_level_keyword(sql, "from", select_start + len("select"))
    if from_start is None:
        return False

    select_clause = sql[select_start + len("select") : from_start]
    expressions = [expr.strip() for expr in _split_top_level_csv(select_clause)]
    return bool(expressions) and all(_AGGREGATE_EXPR.match(expr) for expr in expressions)


def _has_top_level_group_by(sql: str) -> bool:
    group_start = _find_top_level_keyword(sql, "group")
    if group_start is None:
        return False
    return _find_top_level_keyword(sql, "by", group_start + len("group")) is not None


def _has_top_level_set_operation(sql: str) -> bool:
    return any(_find_top_level_keyword(sql, keyword) is not None for keyword in ("union", "intersect", "except"))


def _find_top_level_keyword(sql: str, keyword: str, start: int = 0) -> int | None:
    depth = 0
    keyword_lower = keyword.lower()
    i = start
    while i < len(sql):
        char = sql[i]
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            i += 1
            continue
        if depth == 0 and sql[i : i + len(keyword)].lower() == keyword_lower:
            before = sql[i - 1] if i > 0 else " "
            after = sql[i + len(keyword)] if i + len(keyword) < len(sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return i
        i += 1
    return None


def _split_top_level_csv(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    start = 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


def escape_like(term: str) -> str:
    """LIKE / ILIKE のパターンに埋める語の % と _ と \\ を、ESCAPE '\\' 用にエスケープする。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
