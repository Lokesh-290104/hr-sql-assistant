"""Headline HR metrics for the dashboard strip. Plain SQL over the models, no LLM involved."""

import datetime as dt

from sqlalchemy import func, select

from app.db import get_engine
from app.models import employees, job_openings, performance_reviews


def compute_kpis(today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    year_ago = today - dt.timedelta(days=365)
    current = employees.c.status != "Terminated"

    with get_engine().connect() as conn:
        headcount = conn.scalar(select(func.count()).select_from(employees).where(current)) or 0
        on_leave = conn.scalar(select(func.count()).select_from(employees).where(employees.c.status == "On Leave")) or 0
        left_12m = conn.scalar(
            select(func.count()).select_from(employees).where(employees.c.termination_date >= year_ago)
        ) or 0
        open_roles = conn.scalar(
            select(func.count()).select_from(job_openings).where(job_openings.c.status == "Open")
        ) or 0
        latest_period = conn.scalar(select(func.max(performance_reviews.c.review_period)))
        avg_rating = None
        if latest_period:
            avg_rating = conn.scalar(
                select(func.avg(performance_reviews.c.rating)).where(
                    performance_reviews.c.review_period == latest_period
                )
            )

    # Attrition over the last 12 months relative to the people who were here during that time.
    attrition = 100 * left_12m / (headcount + left_12m) if (headcount + left_12m) else 0.0
    return [
        {"key": "headcount", "label": "Headcount", "value": headcount, "hint": f"{on_leave} on leave"},
        {"key": "attrition", "label": "Attrition, 12 mo", "value": round(attrition, 1), "unit": "%",
         "hint": f"{left_12m} people left"},
        {"key": "open_roles", "label": "Open roles", "value": open_roles, "hint": "hiring now"},
        {"key": "avg_rating", "label": "Avg rating", "value": round(float(avg_rating), 2) if avg_rating else None,
         "unit": "/5", "hint": latest_period or "no reviews yet"},
    ]
