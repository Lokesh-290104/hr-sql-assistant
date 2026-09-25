import datetime as dt
import json
import os
import tempfile
from pathlib import Path

# Point the app at a throwaway SQLite database before any app module reads settings.
_DB_PATH = Path(tempfile.gettempdir()) / f"hr_assistant_test_{os.getpid()}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["GEMINI_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "gemini"

import pytest  # noqa: E402

from scripts.seed import seed  # noqa: E402

TODAY = dt.date(2026, 9, 25)


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    seed(os.environ["DATABASE_URL"], n_employees=120, today=TODAY)
    yield
    from app.db import get_engine

    get_engine().dispose()
    _DB_PATH.unlink(missing_ok=True)


class FakeLLM:
    """Returns scripted replies in order and records every prompt it was sent."""

    def __init__(self, *replies: dict | str):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)


@pytest.fixture
def fake_llm():
    return FakeLLM
