"""The NL-to-SQL pipeline: question -> prompt -> LLM -> validate -> execute (-> repair)."""

import time
from dataclasses import asdict, dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.config import settings
from app.llm import LLMClient, LLMError, parse_answer
from app.models import metadata
from app.prompts import build_system_prompt, build_user_prompt
from app.safety import UnsafeQueryError, validate_sql

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

    def to_dict(self) -> dict:
        return asdict(self)


class QueryAssistant:
    def __init__(self, llm: LLMClient, schema_detail: str = "full"):
        self.llm = llm
        self.schema_detail = schema_detail

    def ask(self, question: str, history: list[dict] | None = None, role: str = "hr_admin") -> AskResult:
        started = time.perf_counter()
        result = self._ask(question.strip(), history or [], role)
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _ask(self, question: str, history: list[dict], role: str) -> AskResult:
        if role not in ROLE_EXCLUSIONS:
            return AskResult(status="error", question=question, error=f"Unknown role '{role}'.")
        if not question:
            return AskResult(status="error", question=question, error="Please ask a question.")

        excluded = ROLE_EXCLUSIONS[role]
        allowed = {t.name for t in metadata.sorted_tables} - set(excluded)
        dialect = db.dialect_name()
        system = build_system_prompt(
            db.build_schema_context(exclude_tables=excluded, detail=self.schema_detail),
            dialect,
            settings.max_rows,
        )
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
                    answer.sql, dialect=dialect, allowed_tables=allowed, max_rows=settings.max_rows
                )
                columns, rows, truncated = db.run_query(validated.sql)
            except UnsafeQueryError as e:
                failed_sql, error = answer.sql, str(e)
                continue
            except SQLAlchemyError as e:
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

        return AskResult(
            status="error",
            question=question,
            sql=failed_sql,
            error=f"Could not produce a valid query: {error}",
            attempts=attempts,
        )


def _db_error_message(e: SQLAlchemyError) -> str:
    original = getattr(e, "orig", None)
    return str(original or e).splitlines()[0]
