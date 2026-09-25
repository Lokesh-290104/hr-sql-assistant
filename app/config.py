"""Application settings, loaded from environment variables / .env."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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
            os.getenv("MYSQL_APP_USER", "hr_readonly"),
            os.environ["MYSQL_APP_PASSWORD"],
            os.getenv("MYSQL_DATABASE", "hr_assistant"),
        )
    return "sqlite:///hr_demo.db"


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
    llm_timeout_ms: int = int(os.getenv("LLM_TIMEOUT_MS", "30000"))
    max_rows: int = int(os.getenv("MAX_ROWS", "200"))
    query_timeout_ms: int = int(os.getenv("QUERY_TIMEOUT_MS", "5000"))
    # How many times the LLM may fix its own SQL after a validation/DB error.
    max_repair_attempts: int = int(os.getenv("MAX_REPAIR_ATTEMPTS", "2"))
    # Conversation turns sent back to the LLM for follow-up questions.
    history_turns: int = int(os.getenv("HISTORY_TURNS", "3"))
    # Tables a "manager" role may not query (compensation data is HR-admin only).
    restricted_tables: list[str] = field(
        default_factory=lambda: _csv(os.getenv("RESTRICTED_TABLES", "salaries"))
    )


settings = Settings()
