"""Prompt construction for NL-to-SQL."""

import datetime as dt
import json

DIALECT_LABELS = {"mysql": "MySQL 8", "sqlite": "SQLite", "postgres": "PostgreSQL"}

# A few worked examples teach the output format and the business rules
# (current employees, current salary) better than instructions alone.
FEW_SHOT = [
    {
        "question": "How many people currently work in each department?",
        "sql": "SELECT d.name AS department, COUNT(*) AS headcount\n"
        "FROM employees e JOIN departments d ON d.id = e.department_id\n"
        "WHERE e.status <> 'Terminated'\nGROUP BY d.name\nORDER BY headcount DESC",
        "explanation": "Counts current (non-terminated) employees per department.",
    },
    {
        "question": "Who are the 5 highest paid engineers?",
        "sql": "SELECT e.first_name, e.last_name, e.job_title, s.annual_ctc\n"
        "FROM employees e\nJOIN departments d ON d.id = e.department_id\n"
        "JOIN salaries s ON s.employee_id = e.id AND s.effective_to IS NULL\n"
        "WHERE d.name = 'Engineering' AND e.status <> 'Terminated'\n"
        "ORDER BY s.annual_ctc DESC\nLIMIT 5",
        "explanation": "Uses each current Engineering employee's current salary row and returns the top 5.",
    },
    {
        "question": "Show me the good performers",
        "sql": None,
        "clarification": "Which rating counts as a good performer (for example 4 and above), and for which review period?",
        "explanation": "The question is ambiguous.",
    },
]

SYSTEM_TEMPLATE = """You are an HR analytics assistant. You translate HR questions into a single read-only {dialect} SQL query over the database described below.

Respond with JSON only, exactly in this shape:
{{"sql": "<query or null>", "explanation": "<one or two plain-English sentences>", "clarification": "<question or null>"}}

Rules:
- Use only tables and columns that appear in the schema. Never invent columns.
- Only SELECT (or WITH ... SELECT) queries. Never modify data.
- For text categories, use the exact values listed in the schema.
- "Employees", "staff" or "headcount" means current employees (status <> 'Terminated') unless the question asks about former employees.
- A person's current salary is the salaries row with effective_to IS NULL. Money is in INR.
- Show people by first_name and last_name, not only by id. Use readable column aliases.
- Today's date is {today}. Use {dialect} date functions for relative dates such as "this year" or "last month".
- Return at most {max_rows} rows. Aggregate when the question asks for totals, averages or counts.
- Use the conversation history to resolve follow-up questions ("what about Sales?", "only the women").
- If the question is ambiguous in a way that would change the answer, set sql to null and ask one short clarification question.
- If the question is unrelated to this HR data, or asks to change data, set sql to null and explain why in explanation.

Database schema:
{schema}

Examples:
{examples}"""


def build_system_prompt(schema: str, dialect: str, max_rows: int, today: dt.date | None = None) -> str:
    examples = "\n".join(
        f"Q: {ex['question']}\nA: "
        + json.dumps({"sql": ex["sql"], "explanation": ex["explanation"], "clarification": ex.get("clarification")})
        for ex in FEW_SHOT
    )
    return SYSTEM_TEMPLATE.format(
        dialect=DIALECT_LABELS.get(dialect, dialect),
        today=(today or dt.date.today()).isoformat(),
        max_rows=max_rows,
        schema=schema,
        examples=examples,
    )


def build_user_prompt(
    question: str,
    history: list[dict] | None = None,
    failed_sql: str | None = None,
    error: str | None = None,
) -> str:
    parts = []
    if history:
        parts.append("Conversation so far:")
        for turn in history:
            parts.append(f"Q: {turn.get('question', '')}")
            if turn.get("sql"):
                parts.append(f"SQL: {turn['sql']}")
        parts.append("")
    parts.append(f"Question: {question}")
    if failed_sql is not None:
        parts.append(
            f"\nYour previous SQL was:\n{failed_sql}\n"
            f"It failed with this error:\n{error}\n"
            "Return corrected JSON."
        )
    return "\n".join(parts)
