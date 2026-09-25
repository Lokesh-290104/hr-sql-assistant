"""SQL guardrails for LLM-generated queries.

The LLM is treated as untrusted input. Every query is parsed into an AST with
sqlglot (never checked with regexes) and must pass all of these rules:

1. Exactly one statement.
2. Read-only: the statement is a SELECT / UNION / WITH query, and no node anywhere
   in the tree writes, changes schema, locks rows or runs admin commands.
3. No dangerous functions (SLEEP, BENCHMARK, LOAD_FILE, ...).
4. Only known HR tables, and only the ones the caller's role may see.
5. A row LIMIT is always present and capped.

The database connection should additionally use a SELECT-only user, so the
database itself is the last line of defence.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class UnsafeQueryError(ValueError):
    """The generated SQL was rejected. The message is safe to show to the user and the LLM."""


FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "TruncateTable",
        "Grant", "Revoke", "Command", "Set", "Use", "Into", "Lock", "Transaction", "Commit",
        "Rollback", "Pragma", "LoadData", "Copy", "Kill", "Describe", "Show",
    )
    if hasattr(exp, name)
)

FORBIDDEN_FUNCTIONS = {
    "sleep", "pg_sleep", "benchmark", "load_file", "get_lock", "release_lock",
    "sys_exec", "sys_eval", "master_pos_wait", "source_pos_wait",
}


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    tables: list[str]


def validate_sql(
    sql: str,
    *,
    dialect: str,
    allowed_tables: set[str],
    max_rows: int,
) -> ValidatedQuery:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise UnsafeQueryError("No SQL was generated.")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        raise UnsafeQueryError(f"SQL could not be parsed: {str(e).splitlines()[0]}") from None

    if len(statements) != 1:
        raise UnsafeQueryError("Only a single SQL statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeQueryError("Only read-only SELECT queries are allowed.")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQueryError(f"Forbidden operation in query: {node.key.upper()}.")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in FORBIDDEN_FUNCTIONS:
                raise UnsafeQueryError(f"Function {name.upper()} is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:  # e.g. table-valued functions
            raise UnsafeQueryError("Only plain table references are allowed.")
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeQueryError(f"Access to '{table.sql()}' is not allowed.")
        if name in cte_names:
            continue
        if name not in allowed:
            raise UnsafeQueryError(f"Table '{table.name}' does not exist or is not accessible for your role.")
        tables.add(name)

    tree = _enforce_limit(tree, max_rows)
    return ValidatedQuery(sql=tree.sql(dialect=dialect), tables=sorted(tables))


def _enforce_limit(tree: exp.Query, max_rows: int) -> exp.Query:
    limit = tree.args.get("limit")
    if limit is None:
        return tree.limit(max_rows)
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= max_rows:
        return tree
    limit.set("expression", exp.Literal.number(max_rows))
    return tree
