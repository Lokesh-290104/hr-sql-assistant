"""Database access: engine, schema context for the LLM, and guarded query execution."""

import datetime as dt
import sqlite3
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, String, create_engine, func, make_url, select
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.models import metadata

# Low-cardinality text columns whose real values are shown to the LLM, so it writes
# WHERE status = 'On Leave' instead of guessing 'on_leave'.
MAX_SAMPLE_VALUES = 15
# Largest string/blob SQLite may build, so one query can't allocate gigabytes.
SQLITE_MAX_VALUE_BYTES = 1_000_000


@lru_cache
def get_engine() -> Engine:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        # Open the SQLite file read-only, the same guarantee the MySQL app user gives.
        uri = Path(url.database).resolve().as_uri() + "?mode=ro"  # as_uri() percent-encodes the path

        def connect() -> sqlite3.Connection:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQLITE_MAX_VALUE_BYTES)
            return conn

        # QueuePool hands each connection to one thread at a time. (A bare "sqlite://" URL
        # would default to SingletonThreadPool, which closes connections still in use.)
        return create_engine("sqlite://", creator=connect, poolclass=QueuePool)
    return create_engine(url, pool_pre_ping=True)


def dialect_name(engine: Engine | None = None) -> str:
    """sqlglot dialect name for the connected database."""
    name = (engine or get_engine()).dialect.name
    return {"postgresql": "postgres"}.get(name, name)


def _categorical_values(engine: Engine) -> dict[tuple[str, str], list[str]]:
    values: dict[tuple[str, str], list[str]] = {}
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            for col in table.columns:
                if not isinstance(col.type, String) or col.primary_key:
                    continue
                distinct = conn.execute(select(func.count(func.distinct(col)))).scalar()
                if distinct and distinct <= MAX_SAMPLE_VALUES:
                    rows = conn.execute(select(col).distinct().order_by(col)).scalars()
                    values[(table.name, col.name)] = [v for v in rows if v is not None]
    return values


@lru_cache
def _cached_categorical_values() -> dict[tuple[str, str], list[str]]:
    return _categorical_values(get_engine())


def warm_schema_cache() -> None:
    _cached_categorical_values()


def build_schema_context(
    exclude_tables: list[str] | None = None,
    detail: str = "full",
    sample_values: dict[tuple[str, str], list[str]] | None = None,
) -> str:
    """Describe the schema as compact text for the prompt.

    detail="full"    types, keys, foreign keys, comments and real category values
    detail="minimal" table and column names only (baseline for the evaluation)
    """
    exclude = set(exclude_tables or [])
    if detail == "full" and sample_values is None:
        sample_values = _cached_categorical_values()

    lines: list[str] = []
    for table in metadata.sorted_tables:
        if table.name in exclude:
            continue
        if detail == "minimal":
            lines.append(f"{table.name}({', '.join(c.name for c in table.columns)})")
            continue

        header = f"TABLE {table.name}"
        if table.comment:
            header += f"  -- {table.comment}"
        lines.append(header)
        for col in table.columns:
            parts = [f"  {col.name} {col.type.compile()}"]
            if col.primary_key:
                parts.append("PRIMARY KEY")
            for fk in col.foreign_keys:
                if fk.column.table.name not in exclude:
                    parts.append(f"REFERENCES {fk.target_fullname}")
            if not col.nullable and not col.primary_key:
                parts.append("NOT NULL")
            notes = []
            if col.comment:
                notes.append(col.comment)
            values = (sample_values or {}).get((table.name, col.name))
            if values and not all(f"'{v}'" in (col.comment or "") for v in values):
                notes.append("values: " + ", ".join(f"'{v}'" for v in values))
            line = " ".join(parts)
            if notes:
                line += "  -- " + "; ".join(notes)
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_query(sql: str, max_rows: int | None = None) -> tuple[list[str], list[list], bool]:
    """Execute an already-validated SELECT. Returns (columns, rows, truncated).

    Defence in depth on top of SQL validation: the transaction is always rolled
    back, and on MySQL a server-side execution time limit is applied.
    """
    max_rows = max_rows or settings.max_rows
    engine = get_engine()
    with engine.connect() as conn:
        raw = None
        if engine.dialect.name == "mysql":
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={int(settings.query_timeout_ms)}")
        elif engine.dialect.name == "sqlite":
            # SQLite has no server-side timeout; abort from a progress callback instead.
            raw = conn.connection.dbapi_connection
            deadline = time.monotonic() + settings.query_timeout_ms / 1000
            raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            result = conn.exec_driver_sql(sql)
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
        finally:
            if raw is not None:
                raw.set_progress_handler(None, 0)
            conn.rollback()
    truncated = len(fetched) > max_rows
    rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
    return columns, rows, truncated
