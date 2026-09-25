"""Regression tests for review findings: guardrail bypasses, failover, timeouts and edge cases."""

import dataclasses
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import db, llm as llm_module, main
from app.config import settings
from app.llm import GeminiClient, LLMError, parse_answer
from app.models import metadata
from app.safety import UnsafeQueryError, validate_sql
from app.service import QueryAssistant

ALL_TABLES = {t.name for t in metadata.sorted_tables}
MANAGER_TABLES = ALL_TABLES - {"salaries"}


def check(sql, tables=MANAGER_TABLES, dialect="mysql"):
    return validate_sql(sql, dialect=dialect, allowed_tables=tables, known_tables=ALL_TABLES, max_rows=100)


# --- Guardrails -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        # A CTE named like a restricted table in one scope used to whitelist the real table elsewhere.
        "SELECT * FROM salaries WHERE EXISTS (WITH salaries AS (SELECT 1) SELECT 1)",
        "WITH salaries AS (SELECT * FROM salaries) SELECT * FROM salaries",
        "WITH employees AS (SELECT 1) SELECT * FROM employees",
    ],
)
def test_cte_cannot_shadow_real_tables(sql):
    with pytest.raises(UnsafeQueryError, match="may not reuse a table name"):
        check(sql)


def test_cte_with_new_name_is_fine():
    assert check("WITH current_staff AS (SELECT * FROM employees) SELECT COUNT(*) FROM current_staff").tables == [
        "employees"
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT @@version",
        "SELECT @@global.datadir",
        "SELECT @counter",
        "SELECT USER()",
        "SELECT CURRENT_USER()",
        "SELECT DATABASE()",
        "SELECT VERSION()",
        "SELECT CONNECTION_ID()",
    ],
)
def test_server_introspection_is_blocked(sql):
    with pytest.raises(UnsafeQueryError):
        check(sql)


def test_unterminated_string_is_rejected_not_crash():
    with pytest.raises(UnsafeQueryError, match="could not be parsed"):
        check("SELECT 'abc FROM employees")


@pytest.mark.parametrize(
    "sql, expected_tail",
    [
        ("SELECT first_name FROM employees UNION SELECT name FROM departments LIMIT 100000", "LIMIT 100"),
        ("WITH c AS (SELECT * FROM employees) SELECT * FROM c", "LIMIT 100"),
        ("SELECT * FROM employees LIMIT 5, 100000", "LIMIT 100 OFFSET 5"),
    ],
)
def test_limit_capped_on_compound_queries(sql, expected_tail):
    assert check(sql).sql.endswith(expected_tail)


# --- Roles ----------------------------------------------------------------------------------

def test_unknown_role_rejected_without_llm_call(fake_llm):
    llm = fake_llm()
    result = QueryAssistant(llm).ask("Average salary?", role="intern")
    assert result.status == "error" and "Unknown role" in result.error
    assert llm.calls == []


def test_api_rejects_unknown_role():
    client = TestClient(main.app)
    assert client.post("/api/query", json={"question": "x", "role": "intern"}).status_code == 422
    assert client.get("/api/schema?role=intern").status_code == 422


def test_api_history_fields_are_bounded():
    client = TestClient(main.app)
    body = {"question": "x", "history": [{"question": "q" * 501}]}
    assert client.post("/api/query", json=body).status_code == 422


# --- Database execution ---------------------------------------------------------------------

def test_run_query_truncates_at_max_rows():
    _, rows, truncated = db.run_query("SELECT id FROM employees", max_rows=5)
    assert len(rows) == 5 and truncated is True
    _, rows, truncated = db.run_query("SELECT id FROM departments", max_rows=8)
    assert len(rows) == 8 and truncated is False


def test_app_connection_cannot_write():
    before = db.run_query("SELECT COUNT(*) FROM departments")[1][0][0]
    with pytest.raises(OperationalError, match="readonly"):
        db.run_query("DELETE FROM departments")
    assert db.run_query("SELECT COUNT(*) FROM departments")[1][0][0] == before


def test_sqlite_query_timeout(monkeypatch, fake_llm):
    monkeypatch.setattr(db, "settings", dataclasses.replace(settings, query_timeout_ms=200))
    endless = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) AS n FROM r"
    llm = fake_llm({"sql": endless, "explanation": ""})
    result = QueryAssistant(llm).ask("count forever")
    assert result.status == "error"
    assert "took too long" in result.error
    assert len(llm.calls) == 1  # timeouts are not sent back for "repair"


def test_database_outage_does_not_trigger_repair(monkeypatch, fake_llm):
    def down(sql):
        raise OperationalError("SELECT 1", {}, Exception(2003, "Can't connect to MySQL server"))

    monkeypatch.setattr(db, "run_query", down)
    llm = fake_llm({"sql": "SELECT COUNT(*) FROM employees", "explanation": ""})
    result = QueryAssistant(llm).ask("headcount")
    assert result.status == "error" and "not reachable" in result.error
    assert len(llm.calls) == 1


# --- Pipeline edge cases --------------------------------------------------------------------

def test_history_is_trimmed(fake_llm):
    llm = fake_llm({"sql": "SELECT 1", "explanation": ""})
    turns = [{"question": f"Question number {i}?", "sql": None} for i in range(settings.history_turns + 2)]
    QueryAssistant(llm).ask("next", history=turns)
    _, user = llm.calls[0]
    assert "Question number 0?" not in user
    assert f"Question number {settings.history_turns + 1}?" in user


def test_llm_error_during_repair(fake_llm):
    llm = fake_llm({"sql": "SELECT full_name FROM employees", "explanation": ""}, "not json")
    result = QueryAssistant(llm).ask("List employees")
    assert result.status == "error" and result.attempts == 2 and "JSON" in result.error


def test_parse_answer_edge_cases():
    with pytest.raises(LLMError, match="JSON object"):
        parse_answer("[1, 2]")
    with pytest.raises(LLMError, match="empty answer"):
        parse_answer('{"sql": null, "explanation": "", "clarification": null}')
    assert parse_answer('Sure: {"sql": "SELECT 1", "explanation": "x"} ok').sql == "SELECT 1"


# --- Gemini failover ------------------------------------------------------------------------

class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def make_client(behaviour, models=("a", "b", "c")):
    """GeminiClient without the SDK: behaviour maps model -> list of results/exceptions per call."""
    calls = []

    def generate_content(model, contents, config):
        calls.append(model)
        outcome = behaviour[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)

    client = object.__new__(GeminiClient)
    client._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    client._types = SimpleNamespace(
        GenerateContentConfig=lambda **k: None, AutomaticFunctionCallingConfig=lambda **k: None, HttpOptions=lambda **k: None
    )
    client._api_error = FakeAPIError
    client.models = list(models)
    client._preferred = models[0]
    client._lock = __import__("threading").Lock()
    return client, calls


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)


def test_overloaded_model_retried_once_then_fallback_is_preferred():
    client, calls = make_client({"a": [FakeAPIError(503), FakeAPIError(503)], "b": ["{}", "{}"]})
    assert client.generate("s", "u") == "{}"
    assert calls == ["a", "a", "b"]
    assert client.active_model == "b"
    client.generate("s", "u")
    assert calls[-1] == "b"  # the working model is tried first next time


def test_quota_exhausted_skips_model_without_retry():
    client, calls = make_client({"a": [FakeAPIError(429)], "b": ["{}"]})
    client.generate("s", "u")
    assert calls == ["a", "b"]


def test_network_error_falls_back():
    client, calls = make_client({"a": [TimeoutError("slow")], "b": ["{}"]})
    assert client.generate("s", "u") == "{}"


def test_all_models_out_of_quota_gives_clear_error():
    client, _ = make_client({m: [FakeAPIError(429)] for m in "abc"})
    with pytest.raises(LLMError, match="quota"):
        client.generate("s", "u")


def test_deadline_stops_trying(monkeypatch):
    monkeypatch.setattr(llm_module, "settings", dataclasses.replace(settings, llm_deadline_ms=1000))
    ticks = iter([0, 0, 0.5, 2, 2, 2])
    monkeypatch.setattr(llm_module.time, "monotonic", lambda: next(ticks))
    client, calls = make_client({"a": [FakeAPIError(404)], "b": ["{}"]})
    with pytest.raises(LLMError, match="too long"):
        client.generate("s", "u")
    assert calls == ["a"]


def test_health_reports_database():
    client = TestClient(main.app)
    assert client.get("/api/health").json()["database"] == "ok"


# --- Red-team follow-ups ---------------------------------------------------------------------

def test_cte_in_inner_scope_does_not_hide_outer_unknown_table():
    # Outer sqlite_master is a real (non-HR) table; the inner CTE of the same name must not whitelist it.
    with pytest.raises(UnsafeQueryError, match="sqlite_master"):
        check("SELECT * FROM sqlite_master WHERE 1 IN (WITH sqlite_master AS (SELECT 1 AS x) SELECT x FROM sqlite_master)")


def test_memory_heavy_sqlite_functions_blocked():
    with pytest.raises(UnsafeQueryError, match="RANDOMBLOB"):
        check("SELECT randomblob(1000000000)", dialect="sqlite")


def test_column_named_like_an_error_is_still_repaired(fake_llm):
    llm = fake_llm(
        {"sql": "SELECT interrupted FROM employees", "explanation": ""},
        {"sql": "SELECT COUNT(*) AS n FROM employees", "explanation": ""},
    )
    result = QueryAssistant(llm).ask("count")
    assert result.status == "ok" and result.attempts == 2


def test_concurrent_queries_do_not_crash():
    from concurrent.futures import ThreadPoolExecutor

    def work(_):
        return db.run_query("SELECT COUNT(*) FROM attendance")[1][0][0]

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(work, range(200)))
    assert len(set(results)) == 1


@pytest.mark.parametrize(
    "sql",
    [
        # Reused aliases once made the real salaries table look like a CTE reference.
        "SELECT a.* FROM salaries AS a JOIN (SELECT 1 AS x) AS a ON TRUE",
        "SELECT * FROM salaries JOIN (SELECT 1 AS z) AS salaries ON TRUE",
        "WITH c AS (SELECT 1 AS x) SELECT a.* FROM c AS a JOIN salaries AS a",
    ],
)
def test_alias_reuse_cannot_hide_restricted_table(sql):
    with pytest.raises(UnsafeQueryError, match="salaries"):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT * FROM employees) SELECT * FROM (SELECT * FROM x) q",
        "WITH x AS (SELECT id FROM employees) SELECT (SELECT COUNT(*) FROM x) AS n",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    ],
)
def test_legitimate_cte_patterns_still_allowed(sql):
    assert check(sql).sql
