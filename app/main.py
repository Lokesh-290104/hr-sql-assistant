"""FastAPI app: REST API plus the chat UI. Run with `uvicorn app.main:app --reload`."""

import logging
from typing import Literal
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.config import settings
from app.llm import LLMError, create_llm_client
from app.models import metadata
from app.service import ROLE_EXCLUSIONS, QueryAssistant

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("hr_assistant")

EXAMPLE_QUESTIONS = [
    "How many active employees are in each department?",
    "What is the average salary by department?",
    "Who joined in the last 6 months?",
    "Which employees are on leave right now?",
    "Top 10 people with the most approved sick days this year",
    "Attrition by department: how many people left each department?",
    "Which job openings have been open the longest, and how many candidates do they have?",
    "Average performance rating by department for 2025-H2",
]

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Read category values for the schema context once at startup, not on the first user's request.
    try:
        await run_in_threadpool(db.warm_schema_cache)
    except SQLAlchemyError as e:
        logger.warning("Schema context not warmed (database unavailable): %s", e)
    yield


app = FastAPI(
    title="HR NL-to-SQL Assistant",
    description="Ask HR questions in plain English; get safe, read-only SQL and results.",
    version="1.0.0",
    lifespan=lifespan,
)


class HistoryTurn(BaseModel):
    question: str = Field(max_length=500)
    sql: str | None = Field(default=None, max_length=4000)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, examples=["How many employees are in Engineering?"])
    # Least privilege: a request that doesn't say which role it is gets the restricted one.
    role: Literal["hr_admin", "manager"] = "manager"
    # Only the last HISTORY_TURNS turns reach the LLM; the cap here just bounds request size.
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)


@lru_cache
def get_assistant() -> QueryAssistant:
    return QueryAssistant(create_llm_client())


@app.get("/api/health")
def health(response: Response):
    try:
        with db.get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        database = "ok"
    except SQLAlchemyError:
        database = "unreachable"
        response.status_code = 503
    model = settings.gemini_model if settings.llm_provider == "gemini" else settings.openrouter_models[0]
    if get_assistant.cache_info().currsize:
        model = getattr(get_assistant().llm, "active_model", model)
    return {"status": "ok" if database == "ok" else "degraded", "database": database,
            "dialect": db.dialect_name(), "provider": settings.llm_provider, "model": model}


@app.get("/api/roles")
def roles():
    return {role: {"restricted_tables": excluded} for role, excluded in ROLE_EXCLUSIONS.items()}


@app.get("/api/examples")
def examples():
    return EXAMPLE_QUESTIONS


@app.get("/api/schema")
def schema(role: Literal["hr_admin", "manager"] = "manager"):
    excluded = set(ROLE_EXCLUSIONS[role])
    return [
        {
            "name": t.name,
            "description": t.comment,
            "columns": [{"name": c.name, "type": str(c.type.compile())} for c in t.columns],
        }
        for t in metadata.sorted_tables
        if t.name not in excluded
    ]


@app.post("/api/query")
def query(req: QueryRequest):
    try:
        assistant = get_assistant()
    except LLMError as e:
        raise HTTPException(503, str(e))
    result = assistant.ask(req.question, [t.model_dump() for t in req.history], req.role)
    return result.to_dict()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
