"""Second ship-audit pass: eval helpers, Gemini error mapping, config builders and remaining edge cases."""

import dataclasses
import datetime as dt
from functools import lru_cache
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlglot.errors import OptimizeError

from app import config, main, safety
from app.config import settings
from app.llm import GeminiClient, LLMError, ProviderError, parse_answer
from app.models import metadata
from app.prompts import build_system_prompt
from app.safety import UnsafeQueryError, validate_sql
from eval.run_eval import _normalize, gold_rows, results_match
from scripts.seed import _half_years, _weekdays, generate

ALL_TABLES = {t.name for t in metadata.sorted_tables}


# --- eval/run_eval.py (pure helpers only; no LLM) ---------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [(True, 1), (3, 3.0), (2.345, 2.35), ("4.5", 4.5), (None, None), ("  Engineering ", "engineering")],
)
def test_eval_normalize(value, expected):
    assert _normalize(value) == expected


def test_results_match_ignores_order_and_extra_columns():
    gold = [["Sales", 10], ["Engineering", 20]]
    assert results_match(gold, [[20.0, "engineering", "x"], [10, "SALES", "y"]])
    assert results_match([], [])
    assert not results_match(gold, gold[:1])  # row count differs
    assert not results_match(gold, [["Sales", 11], ["Engineering", 20]])  # a value differs


def test_gold_rows_transpiles_mysql_to_sqlite():
    # IFNULL and backticks are MySQL spellings; the test database is SQLite.
    assert gold_rows("SELECT IFNULL(NULL, COUNT(*)) AS n FROM `departments`") == [[8]]


# --- GeminiClient._call (client object mocked; no network) -----------------------------------

def make_gemini(generate_content):
    from google import genai

    client = GeminiClient.__new__(GeminiClient)
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    client._types = genai.types
    client._api_error = genai.errors.APIError
    return client


def test_gemini_call_sends_json_config_and_returns_text():
    seen = {}

    def generate_content(model, contents, config):
        seen.update(model=model, contents=contents, config=config)
        return SimpleNamespace(text=None)

    assert make_gemini(generate_content)._call("gem-1", "SYS", "USER", 5000) == ""
    assert seen["model"] == "gem-1" and seen["contents"] == "USER"
    assert seen["config"].system_instruction == "SYS"
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].temperature == 0


def test_gemini_api_error_maps_to_provider_error():
    from google import genai

    def generate_content(**kwargs):
        raise genai.errors.APIError(429, {"error": {"message": "quota"}})

    with pytest.raises(ProviderError) as info:
        make_gemini(generate_content)._call("gem-1", "s", "u", 5000)
    assert info.value.code == 429


def test_gemini_requires_key(monkeypatch):
    import app.llm as llm_module

    monkeypatch.setattr(llm_module, "settings", dataclasses.replace(settings, gemini_api_key=""))
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient()


# --- Safety ----------------------------------------------------------------------------------

def test_table_valued_function_is_rejected():
    with pytest.raises(UnsafeQueryError, match="plain table references"):
        validate_sql("SELECT * FROM json_each('[1]')", dialect="sqlite", allowed_tables=ALL_TABLES, max_rows=10)


def test_scope_analysis_failure_is_rejected(monkeypatch):
    def fail(tree):
        raise OptimizeError("bad scope")

    monkeypatch.setattr(safety, "traverse_scope", fail)
    with pytest.raises(UnsafeQueryError, match="could not be analysed: bad scope"):
        validate_sql("SELECT * FROM employees", dialect="mysql", allowed_tables=ALL_TABLES, max_rows=10)


# --- LLM output parsing / prompts ------------------------------------------------------------

def test_parse_answer_braces_with_invalid_json_inside():
    with pytest.raises(LLMError, match="valid JSON"):
        parse_answer("Here you go: {sql: SELECT 1} thanks")


def test_parse_answer_clarification_only():
    answer = parse_answer('{"sql": "  ", "clarification": " Which period? "}')
    assert answer.sql is None and answer.clarification == "Which period?" and answer.explanation == ""


def test_system_prompt_dialect_labels():
    assert "single read-only MySQL 8 SQL query" in build_system_prompt("S", "mysql", 50, dt.date(2026, 1, 1))
    unknown = build_system_prompt("S", "duckdb", 50, dt.date(2026, 1, 1))
    assert "single read-only duckdb SQL query" in unknown
    assert "Today's date is 2026-01-01" in unknown and "at most 50 rows" in unknown


# --- Config builders -------------------------------------------------------------------------

def test_int_setting_is_clamped_to_minimum(monkeypatch):
    monkeypatch.setenv("SHIP_TEST_INT", "-5")
    assert config._int("SHIP_TEST_INT", 10, 1) == 1
    monkeypatch.delenv("SHIP_TEST_INT")
    assert config._int("SHIP_TEST_INT", 10, 1) == 10


def test_mysql_url_escapes_password(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", "db.local")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    url = config.mysql_url("app", "p@ss:w/rd", "hr")
    assert url == "mysql+pymysql://app:p%40ss%3Aw%2Frd@db.local:3307/hr"


def test_default_database_url_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///explicit.db")
    assert config._default_database_url() == "sqlite:///explicit.db"

    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("MYSQL_APP_PASSWORD", "secret")
    monkeypatch.delenv("MYSQL_APP_USER", raising=False)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    monkeypatch.delenv("MYSQL_HOST", raising=False)
    monkeypatch.delenv("MYSQL_PORT", raising=False)
    assert config._default_database_url() == "mysql+pymysql://hr_readonly:secret@localhost:3306/hr_assistant"

    monkeypatch.delenv("MYSQL_APP_PASSWORD")
    fallback = config._default_database_url()
    assert fallback.startswith("sqlite:///") and fallback.endswith("/hr_demo.db")


def test_csv_helper_strips_blanks():
    assert config._csv(" a, ,b ,") == ["a", "b"]


# --- API -------------------------------------------------------------------------------------

def test_health_model_name_under_openrouter(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, llm_provider="openrouter", openrouter_models=["or/model-1", "or/model-2"]))
    monkeypatch.setattr(main, "get_assistant", lru_cache(lambda: None))  # never built yet
    body = TestClient(main.app).get("/api/health").json()
    assert body["provider"] == "openrouter" and body["model"] == "or/model-1"


def test_static_assets_are_served():
    client = TestClient(main.app)
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/missing.js").status_code == 404


# --- Seed data (no database writes) -----------------------------------------------------------

def test_seed_helpers_and_generation_are_deterministic():
    assert [d.weekday() for d in _weekdays(dt.date(2026, 9, 25), dt.date(2026, 9, 28))] == [4, 0]
    assert _half_years(dt.date(2025, 1, 1), dt.date(2026, 9, 25)) == [
        ("2025-H1", dt.date(2025, 6, 30)), ("2025-H2", dt.date(2025, 12, 31)), ("2026-H1", dt.date(2026, 6, 30)),
    ]
    today = dt.date(2026, 9, 25)
    a, b = generate(40, today), generate(40, today)
    assert a["employees"] == b["employees"]
    ids = {e["id"] for e in a["employees"]}
    assert all(e["manager_id"] is None or e["manager_id"] in ids for e in a["employees"])
    assert all(r["employee_id"] in ids for r in a["salaries"])
