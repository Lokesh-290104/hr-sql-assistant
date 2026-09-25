"""Coverage for error handlers and edge cases found in the ship-time test audit."""

import dataclasses
import datetime as dt
import json
import sqlite3
from decimal import Decimal
from functools import lru_cache

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from app import db, kpis as kpis_module, llm as llm_module, main, service as service_module
from app.config import settings
from app.llm import FailoverClient, LLMError, OpenRouterClient, ProviderError
from app.models import metadata
from app.service import QueryAssistant


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)


def db_down(*args, **kwargs):
    raise OperationalError("SELECT 1", {}, Exception(2003, "Can't connect to MySQL server"))


class BrokenEngine:
    def connect(self):
        db_down()


# --- KPIs -------------------------------------------------------------------------------------

def test_kpis_on_empty_database_have_no_division_error_and_no_rating(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    metadata.create_all(engine)
    monkeypatch.setattr(kpis_module, "get_engine", lambda: engine)
    try:
        kpis = {k["key"]: k for k in kpis_module.compute_kpis(dt.date(2026, 9, 25))}
    finally:
        engine.dispose()

    assert kpis["headcount"]["value"] == 0 and kpis["headcount"]["hint"] == "0 on leave"
    assert kpis["attrition"]["value"] == 0.0
    assert kpis["open_roles"]["value"] == 0
    assert kpis["avg_rating"]["value"] is None
    assert kpis["avg_rating"]["hint"] == "no reviews yet"


def test_kpis_use_latest_review_period_and_12_month_window():
    kpis = {k["key"]: k for k in kpis_module.compute_kpis(dt.date(2026, 9, 25))}
    latest = db.run_query("SELECT MAX(review_period) FROM performance_reviews")[1][0][0]
    assert kpis["avg_rating"]["hint"] == latest
    assert 1 <= kpis["avg_rating"]["value"] <= 5
    left = db.run_query("SELECT COUNT(*) FROM employees WHERE termination_date >= '2025-09-25'")[1][0][0]
    assert kpis["attrition"]["hint"] == f"{left} people left"


def test_kpis_endpoint_returns_503_when_database_down(client, monkeypatch):
    monkeypatch.setattr(main, "compute_kpis", db_down)
    response = client.get("/api/kpis")
    assert response.status_code == 503
    assert "not reachable" in response.json()["detail"]


# --- Health / roles / examples / lifespan -----------------------------------------------------

def test_health_degraded_when_database_down(client, monkeypatch):
    monkeypatch.setattr(main.db, "get_engine", lambda: BrokenEngine())
    monkeypatch.setattr(main.db, "dialect_name", lambda: "mysql")
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded" and response.json()["database"] == "unreachable"


def test_health_reports_active_model_once_assistant_exists(client, monkeypatch):
    class Stub:
        llm = type("L", (), {"active_model": "fallback-model"})()

    cached = lru_cache(lambda: Stub())
    cached()  # populate the cache, as a first /api/query would
    monkeypatch.setattr(main, "get_assistant", cached)
    assert client.get("/api/health").json()["model"] == "fallback-model"


def test_roles_endpoint(client):
    roles = client.get("/api/roles").json()
    assert roles["hr_admin"]["restricted_tables"] == []
    assert "salaries" in roles["manager"]["restricted_tables"]


def test_examples_default_to_manager_and_reject_unknown_role(client):
    assert client.get("/api/examples").json() == client.get("/api/examples?role=manager").json()
    assert client.get("/api/examples?role=intern").status_code == 422


def test_startup_survives_database_outage(monkeypatch):
    monkeypatch.setattr(main.db, "warm_schema_cache", db_down)
    with TestClient(main.app) as c:  # runs the lifespan
        assert c.get("/api/roles").status_code == 200


# --- Pipeline edge cases ----------------------------------------------------------------------

def test_result_reports_answering_model(fake_llm):
    llm = fake_llm({"sql": "SELECT 1 AS one", "explanation": ""})
    llm.active_model = "model-x"
    result = QueryAssistant(llm).ask("one")
    assert result.status == "ok" and result.model == "model-x"
    assert result.to_dict()["model"] == "model-x"


def test_blank_question_is_rejected_without_llm_call(fake_llm):
    llm = fake_llm()
    llm.active_model = "model-x"
    result = QueryAssistant(llm).ask("   ")
    assert result.status == "error" and "ask a question" in result.error
    assert llm.calls == [] and result.model is None and result.attempts == 0


def test_database_down_while_building_schema(monkeypatch, fake_llm):
    monkeypatch.setattr(db, "build_schema_context", db_down)
    llm = fake_llm()
    result = QueryAssistant(llm).ask("headcount")
    assert result.status == "error" and "not reachable" in result.error
    assert llm.calls == []


def test_clarification_falls_back_to_explanation(fake_llm):
    llm = fake_llm({"sql": None, "explanation": "I can only answer HR questions.", "clarification": None})
    result = QueryAssistant(llm).ask("What's the weather?")
    assert result.status == "clarification"
    assert result.clarification == "I can only answer HR questions."


def test_repairs_exhausted_keeps_last_failed_sql(fake_llm):
    bad = {"sql": "SELECT nope FROM employees", "explanation": ""}
    llm = fake_llm(*[bad] * (settings.max_repair_attempts + 1))
    result = QueryAssistant(llm).ask("x")
    assert result.status == "error" and result.attempts == settings.max_repair_attempts + 1
    assert result.sql == "SELECT nope FROM employees"
    assert "couldn't write a working query" in result.error


@pytest.mark.parametrize(
    "error, infra, message",
    [
        (InterfaceError("s", {}, Exception("gone")), True, "not reachable"),
        (OperationalError("s", {}, Exception(3024, "max execution time exceeded")), True, "took too long"),
        (OperationalError("s", {}, Exception(1054, "Unknown column")), False, None),
    ],
)
def test_infrastructure_error_classification(error, infra, message):
    assert service_module._is_infrastructure_error(error) is infra
    if message:
        assert message in service_module._infrastructure_message(error)


def test_sqlite_busy_is_infrastructure():
    orig = sqlite3.OperationalError("database is locked")
    orig.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error = OperationalError("s", {}, orig)
    assert service_module._is_infrastructure_error(error)
    assert "not reachable" in service_module._infrastructure_message(error)


# --- LLM clients ------------------------------------------------------------------------------

def openrouter(handler, models=("m1", "m2")):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenRouterClient(api_key="test-key", models=list(models), http=http)


@pytest.mark.parametrize(
    "body",
    [
        [1, 2, 3],  # JSON but not an object
        {"choices": []},
        {"unexpected": True},
        {"choices": [{"message": None}]},
    ],
)
def test_openrouter_unexpected_shapes_fail_over(body):
    def handler(request):
        if json.loads(request.content)["model"] == "m1":
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = openrouter(handler)
    assert client.generate("s", "u") == "{}"
    assert client.active_model == "m2"


def test_openrouter_non_json_body_and_null_content():
    def handler(request):
        if json.loads(request.content)["model"] == "m1":
            return httpx.Response(200, text="<html>gateway</html>")
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    assert openrouter(handler).generate("s", "u") == ""


def test_openrouter_error_body_without_code_counts_as_502():
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "boom"}})

    with pytest.raises(LLMError, match="unavailable"):
        openrouter(handler, models=("m1",)).generate("s", "u")


def test_failover_client_requires_models_and_deduplicates():
    with pytest.raises(LLMError, match="No LLM models configured"):
        FailoverClient([])
    assert FailoverClient(["a", "b", "a"]).models == ["a", "b"]


# --- DB helpers -------------------------------------------------------------------------------

def test_jsonable_and_minimal_schema_context():
    assert db._jsonable(Decimal("1.50")) == 1.5
    assert db._jsonable(dt.date(2026, 1, 2)) == "2026-01-02"
    assert db._jsonable(b"ab\xff") == "ab�"
    minimal = db.build_schema_context(exclude_tables=["salaries"], detail="minimal")
    assert "employees(id, first_name" in minimal and "salaries" not in minimal
    full = db.build_schema_context(exclude_tables=["departments"], sample_values={})
    assert "REFERENCES departments" not in full  # FKs to hidden tables are not leaked
