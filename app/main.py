"""FastAPI app: REST API plus the chat UI. Run with `uvicorn app.main:app --reload`."""

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.config import settings
from app.llm import GeminiClient, LLMError
from app.models import metadata
from app.service import ROLE_EXCLUSIONS, QueryAssistant

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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

app = FastAPI(
    title="HR NL-to-SQL Assistant",
    description="Ask HR questions in plain English; get safe, read-only SQL and results.",
    version="1.0.0",
)


class HistoryTurn(BaseModel):
    question: str
    sql: str | None = None


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, examples=["How many employees are in Engineering?"])
    role: str = Field(default="hr_admin", examples=["hr_admin", "manager"])
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)


@lru_cache
def get_assistant() -> QueryAssistant:
    return QueryAssistant(GeminiClient())


@app.get("/api/health")
def health():
    return {"status": "ok", "database": db.dialect_name(), "model": settings.gemini_model}


@app.get("/api/roles")
def roles():
    return {role: {"restricted_tables": excluded} for role, excluded in ROLE_EXCLUSIONS.items()}


@app.get("/api/examples")
def examples():
    return EXAMPLE_QUESTIONS


@app.get("/api/schema")
def schema(role: str = "hr_admin"):
    if role not in ROLE_EXCLUSIONS:
        raise HTTPException(400, f"Unknown role '{role}'")
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
