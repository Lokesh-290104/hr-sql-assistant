import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import main
from app.ratelimit import SECONDS_PER_DAY, RateLimiter
from app.service import QueryAssistant


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_per_minute_limit_is_per_client_and_slides():
    clock = Clock()
    limiter = RateLimiter(per_minute=2, per_day=0, clock=clock)

    assert limiter.check("a") is None
    assert limiter.check("a") is None
    assert "at most 2 per minute" in limiter.check("a")
    assert limiter.check("b") is None  # another client has its own window

    clock.now += 60.01  # the oldest requests leave the window
    assert limiter.check("a") is None


def test_refused_requests_do_not_extend_the_window():
    clock = Clock()
    limiter = RateLimiter(per_minute=1, per_day=0, clock=clock)
    assert limiter.check("a") is None
    clock.now += 30
    assert limiter.check("a") is not None  # refused, and not recorded
    clock.now += 30.01
    assert limiter.check("a") is None


def test_daily_cap_is_global_and_resets_next_day():
    clock = Clock(now=SECONDS_PER_DAY * 100.0)
    limiter = RateLimiter(per_minute=0, per_day=3, clock=clock)

    for client in ("a", "b", "c"):
        assert limiter.check(client) is None
    assert "limit for today" in limiter.check("d")

    clock.now += SECONDS_PER_DAY
    assert limiter.check("d") is None


def test_zero_disables_both_limits():
    limiter = RateLimiter(per_minute=0, per_day=0, clock=Clock())
    assert all(limiter.check("a") is None for _ in range(1000))


@pytest.fixture
def client():
    return TestClient(main.app)


def test_query_endpoint_refuses_with_429_before_calling_the_llm(client, monkeypatch, fake_llm):
    llm = fake_llm({"sql": "SELECT COUNT(*) AS n FROM departments", "explanation": "Counts departments."})
    monkeypatch.setattr(main, "get_assistant", lambda: QueryAssistant(llm))
    monkeypatch.setattr(main, "query_limiter", RateLimiter(per_minute=1, per_day=0))

    first = client.post("/api/query", json={"question": "How many departments?"})
    second = client.post("/api/query", json={"question": "How many departments?"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert "per minute" in second.json()["detail"]
    assert len(llm.calls) == 1  # the refused request never reached the model


def test_client_address_uses_the_proxy_header_only_when_trusted(client, monkeypatch):
    seen: list[str] = []

    class Recorder(RateLimiter):
        def check(self, address: str) -> str | None:
            seen.append(address)
            return "stop"  # refuse, so no LLM is needed

    monkeypatch.setattr(main, "query_limiter", Recorder(0, 0))
    headers = {"X-Forwarded-For": "198.51.100.7, 203.0.113.9"}

    client.post("/api/query", json={"question": "q"}, headers=headers)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, trust_proxy=True))
    client.post("/api/query", json={"question": "q"}, headers=headers)

    # Untrusted: the socket address. Trusted: the entry the proxy appended (last), not the
    # spoofable first one.
    assert seen == ["testclient", "203.0.113.9"]
