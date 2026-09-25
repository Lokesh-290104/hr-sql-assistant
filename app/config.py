"""Application settings, loaded from environment variables / .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MYSQL_DATABASE = "hr_assistant"
DEFAULT_MYSQL_APP_USER = "hr_readonly"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int(name: str, default: int, minimum: int) -> int:
    return max(minimum, int(os.getenv(name, str(default))))


def mysql_url(user: str, password: str, database: str | None) -> str:
    """Build a MySQL URL from parts, escaping special characters in the password."""
    from sqlalchemy import URL

    return URL.create(
        "mysql+pymysql",
        username=user,
        password=password,
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        database=database,
    ).render_as_string(hide_password=False)


def _default_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if os.getenv("MYSQL_APP_PASSWORD"):
        return mysql_url(
            os.getenv("MYSQL_APP_USER", DEFAULT_MYSQL_APP_USER),
            os.environ["MYSQL_APP_PASSWORD"],
            os.getenv("MYSQL_DATABASE", DEFAULT_MYSQL_DATABASE),
        )
    # Absolute path, so starting the app from another folder doesn't create an empty database.
    return f"sqlite:///{(PROJECT_ROOT / 'hr_demo.db').as_posix()}"


@dataclass(frozen=True)
class Settings:
    # Connection used to answer questions. Should be a READ-ONLY database user.
    database_url: str = _default_database_url()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    # Tried in order when the main model is overloaded or unavailable.
    gemini_fallback_models: list[str] = field(
        default_factory=lambda: _csv(os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.7-flash,gemini-flash-latest"))
    )
    # Per HTTP call to the LLM, and total across all retries/fallbacks of one generate().
    llm_timeout_ms: int = _int("LLM_TIMEOUT_MS", 30000, 1000)
    llm_deadline_ms: int = _int("LLM_DEADLINE_MS", 60000, 1000)
    max_rows: int = _int("MAX_ROWS", 200, 1)
    query_timeout_ms: int = _int("QUERY_TIMEOUT_MS", 5000, 100)
    # How many times the LLM may fix its own SQL after a validation/DB error.
    max_repair_attempts: int = _int("MAX_REPAIR_ATTEMPTS", 2, 0)
    # Conversation turns sent back to the LLM for follow-up questions.
    history_turns: int = _int("HISTORY_TURNS", 3, 0)
    # Tables a "manager" role may not query (compensation data is HR-admin only).
    restricted_tables: list[str] = field(
        default_factory=lambda: [t.lower() for t in _csv(os.getenv("RESTRICTED_TABLES", "salaries"))]
    )


settings = Settings()
