"""The NL-to-SQL pipeline: question -> prompt -> LLM -> validate -> execute (-> repair)."""

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field

from sqlalchemy.exc import DisconnectionError, InterfaceError, SQLAlchemyError

from app import db
from app.config import settings
from app.llm import LLMClient, LLMError, parse_answer
from app.models import metadata
from app.prompts import build_system_prompt, build_user_prompt
from app.safety import UnsafeQueryError, validate_sql

logger = logging.getLogger("hr_assistant")

# Role-based access: tables each role is NOT allowed to query.
ROLE_EXCLUSIONS: dict[str, list[str]] = {
    "hr_admin": [],
    "manager": settings.restricted_tables,
}


@dataclass
class AskResult:
    status: str  # "ok" | "clarification" | "error"
    question: str
    explanation: str = ""
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    clarification: str | None = None
    error: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    model: str | None = None  # which LLM answered (when the client reports it)

    def to_dict(self) -> dict:
        return asdict(self)


class QueryAssistant:
    def __init__(self, llm: LLMClient, schema_detail: str = "full"):
        self.llm = llm
        self.schema_detail = schema_detail

    def ask(self, question: str, history: list[dict] | None = None, role: str = "hr_admin") -> AskResult:
        started = time.perf_counter()
        result = self._ask(question.strip(), history or [], role)
        if result.attempts:
            result.model = getattr(self.llm, "active_model", None)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _ask(self, question: str, history: list[dict], role: str) -> AskResult:
        if role not in ROLE_EXCLUSIONS:
            return AskResult(status="error", question=question, error=f"Unknown role '{role}'.")
        if not question:
            return AskResult(status="error", question=question, error="Please ask a question.")

        excluded = ROLE_EXCLUSIONS[role]
        known = {t.name for t in metadata.sorted_tables}
        allowed = known - set(excluded)
        dialect = db.dialect_name()
        try:
            schema = db.build_schema_context(exclude_tables=excluded, detail=self.schema_detail)
        except SQLAlchemyError as e:  # database down on first use
            return AskResult(status="error", question=question, error=_infrastructure_message(e))
        system = build_system_prompt(schema, dialect, settings.max_rows)
        history = history[-settings.history_turns:]

        failed_sql: str | None = None
        error: str | None = None
        attempts = 0
        for attempts in range(1, settings.max_repair_attempts + 2):
            user = build_user_prompt(question, history, failed_sql, error)
            try:
                answer = parse_answer(self.llm.generate(system, user))
            except LLMError as e:
                return AskResult(status="error", question=question, error=str(e), attempts=attempts)

            if answer.sql is None:
                return AskResult(
                    status="clarification",
                    question=question,
                    explanation=answer.explanation,
                    clarification=answer.clarification or answer.explanation,
                    attempts=attempts,
                )

            try:
                validated = validate_sql(
                    answer.sql, dialect=dialect, allowed_tables=allowed, known_tables=known,
                    max_rows=settings.max_rows,
                )
                columns, rows, truncated = db.run_query(validated.sql)
            except UnsafeQueryError as e:
                failed_sql, error = answer.sql, str(e)
                continue
            except SQLAlchemyError as e:
                if _is_infrastructure_error(e):
                    # Not something the LLM can fix: don't burn repair calls on it.
                    return AskResult(
                        status="error", question=question, sql=answer.sql,
                        error=_infrastructure_message(e), attempts=attempts,
                    )
                failed_sql, error = answer.sql, _db_error_message(e)
                continue

            return AskResult(
                status="ok",
                question=question,
                explanation=answer.explanation,
                sql=validated.sql,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                attempts=attempts,
            )

        # The detailed error went to the LLM for repair; users get a friendly message instead of DB internals.
        logger.warning("Giving up after %d attempts for %r: %s", attempts, question, error)
        return AskResult(
            status="error",
            question=question,
            sql=failed_sql,
            error="I couldn't write a working query for that question. Try rephrasing it or asking something more specific.",
            attempts=attempts,
        )


def _db_error_message(e: SQLAlchemyError) -> str:
    original = getattr(e, "orig", None)
    return str(original or e).splitlines()[0]


# MySQL error codes that mean "the database is unreachable, refused us, or timed out".
MYSQL_TIMEOUT = 3024
MYSQL_INFRA_CODES = {1045, 2002, 2003, 2006, 2013, MYSQL_TIMEOUT}


# SQLite result codes: interrupted by our timeout, busy/locked, cannot open the file.
SQLITE_INFRA_CODES = {sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN}


def _sqlite_code(e: SQLAlchemyError) -> int | None:
    original = getattr(e, "orig", None)
    code = getattr(original, "sqlite_errorcode", None) if isinstance(original, sqlite3.Error) else None
    return code & 0xFF if isinstance(code, int) else None  # primary code without extended bits


def _mysql_code(e: SQLAlchemyError) -> int | None:
    args = getattr(getattr(e, "orig", None), "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def _is_infrastructure_error(e: SQLAlchemyError) -> bool:
    if isinstance(e, (InterfaceError, DisconnectionError)) or getattr(e, "connection_invalidated", False):
        return True
    if _mysql_code(e) in MYSQL_INFRA_CODES:
        return True
    return _sqlite_code(e) in SQLITE_INFRA_CODES


def _infrastructure_message(e: SQLAlchemyError) -> str:
    if _mysql_code(e) == MYSQL_TIMEOUT or _sqlite_code(e) == sqlite3.SQLITE_INTERRUPT:
        return "The query took too long and was stopped. Try a narrower question."
    return "The database is not reachable right now. Please try again later."
