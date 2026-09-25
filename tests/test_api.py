import pytest
from fastapi.testclient import TestClient

from app import main
from app.service import QueryAssistant


@pytest.fixture
def client():
    return TestClient(main.app)


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_schema_hides_restricted_tables_for_managers(client):
    admin = {t["name"] for t in client.get("/api/schema?role=hr_admin").json()}
    manager = {t["name"] for t in client.get("/api/schema?role=manager").json()}
    default = {t["name"] for t in client.get("/api/schema").json()}
    assert "salaries" in admin
    assert "salaries" not in manager
    assert default == manager  # least privilege by default


def test_query_endpoint(client, monkeypatch, fake_llm):
    llm = fake_llm({"sql": "SELECT COUNT(*) AS departments FROM departments", "explanation": "Counts departments."})
    monkeypatch.setattr(main, "get_assistant", lambda: QueryAssistant(llm))

    response = client.post("/api/query", json={"question": "How many departments?"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["rows"] == [[8]]


def test_missing_api_key_returns_503(client):
    main.get_assistant.cache_clear()
    response = client.post("/api/query", json={"question": "How many departments?"})
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_rejects_empty_question(client):
    assert client.post("/api/query", json={"question": ""}).status_code == 422


def test_ui_is_served(client):
    assert "HR Query Assistant" in client.get("/").text


def test_examples_are_grouped_and_role_filtered(client):
    admin = {g["group"] for g in client.get("/api/examples?role=hr_admin").json()}
    manager = {g["group"] for g in client.get("/api/examples?role=manager").json()}
    assert "Pay" in admin and "Pay" not in manager
    assert all(g["questions"] for g in client.get("/api/examples?role=hr_admin").json())


def test_kpis(client):
    kpis = {k["key"]: k for k in client.get("/api/kpis").json()}
    assert set(kpis) == {"headcount", "attrition", "open_roles", "avg_rating"}
    assert kpis["headcount"]["value"] > 0
    assert 0 <= kpis["attrition"]["value"] <= 100
