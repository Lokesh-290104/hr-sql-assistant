"""Create the HR schema and fill it with realistic, reproducible demo data.

Usage:
    python -m scripts.seed                 # uses ADMIN_DATABASE_URL, else DATABASE_URL
    python -m scripts.seed --employees 500

Seeding needs write access, so it uses ADMIN_DATABASE_URL (a user that can
create tables). The app itself should run with the read-only DATABASE_URL.
"""

import argparse
import datetime as dt
import os
import random

from faker import Faker
from sqlalchemy import create_engine, insert

from app.config import settings
from app.models import (
    attendance,
    candidates,
    departments,
    employees,
    job_openings,
    leave_requests,
    metadata,
    performance_reviews,
    salaries,
)

DEPARTMENTS = [
    # name, location, budget (INR), share of headcount, job titles, CTC band (INR lakh)
    ("Engineering", "Bengaluru", 180_000_000, 0.32,
     ["Software Engineer", "Senior Software Engineer", "Tech Lead", "QA Engineer", "DevOps Engineer"], (6, 45)),
    ("Sales", "Mumbai", 90_000_000, 0.18, ["Sales Executive", "Account Manager", "Regional Sales Manager"], (4, 30)),
    ("Customer Success", "Pune", 40_000_000, 0.12, ["Support Associate", "Customer Success Manager"], (3.5, 18)),
    ("Marketing", "Mumbai", 50_000_000, 0.08, ["Marketing Associate", "Content Strategist", "Growth Manager"], (4.5, 28)),
    ("Human Resources", "Gurugram", 25_000_000, 0.07, ["HR Executive", "HR Business Partner", "Talent Acquisition Specialist"], (4, 25)),
    ("Finance", "Gurugram", 30_000_000, 0.07, ["Accountant", "Financial Analyst", "Finance Manager"], (5, 32)),
    ("Product", "Bengaluru", 60_000_000, 0.10, ["Product Manager", "Product Designer", "Business Analyst"], (8, 50)),
    ("Operations", "Hyderabad", 35_000_000, 0.06, ["Operations Executive", "Operations Manager"], (3.5, 22)),
]
CITIES = ["Bengaluru", "Mumbai", "Pune", "Gurugram", "Hyderabad", "Chennai", "Kolkata", "Jamshedpur"]
LEAVE_TYPES = ["Sick", "Casual", "Earned"]
SOURCES = ["LinkedIn", "Referral", "Naukri", "Campus", "Careers Page"]
REVIEW_COMMENTS = {
    1: "Did not meet expectations; needs a performance improvement plan.",
    2: "Partially met expectations; improvement needed in delivery.",
    3: "Met expectations consistently.",
    4: "Exceeded expectations on key goals.",
    5: "Outstanding impact; strong candidate for promotion.",
}


def _weekdays(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += dt.timedelta(days=1)


def _half_years(start: dt.date, end: dt.date) -> list[tuple[str, dt.date]]:
    periods = []
    for year in range(start.year, end.year + 1):
        for half, period_end in (("H1", dt.date(year, 6, 30)), ("H2", dt.date(year, 12, 31))):
            if start <= period_end <= end:
                periods.append((f"{year}-{half}", period_end))
    return periods


def generate(n_employees: int, today: dt.date, seed: int = 42) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)
    data: dict[str, list[dict]] = {t.name: [] for t in metadata.sorted_tables}

    for i, (name, location, budget, *_rest) in enumerate(DEPARTMENTS, start=1):
        data["departments"].append({"id": i, "name": name, "location": location, "annual_budget": budget})

    # Employees: CEO first, then department heads, then everyone else.
    emails: set[str] = set()

    def person(emp_id: int, dept_id: int, title: str, manager_id: int | None, hire_date: dt.date) -> dict:
        gender = rng.choices(["Male", "Female", "Other"], weights=[55, 43, 2])[0]
        first = fake.first_name_male() if gender == "Male" else fake.first_name_female()
        last = fake.last_name()
        email = f"{first}.{last}".lower().replace(" ", "")
        while email in emails:
            email += str(rng.randint(0, 9))
        emails.add(email)
        emp_type = "Full-time" if title.startswith(("Chief", "Head")) else rng.choices(
            ["Full-time", "Contract", "Part-time", "Intern"], weights=[82, 9, 3, 6]
        )[0]
        return {
            "id": emp_id,
            "first_name": first,
            "last_name": last,
            "email": f"{email}@acmehr.example",
            "gender": gender,
            "date_of_birth": fake.date_of_birth(minimum_age=21 if emp_type != "Intern" else 20, maximum_age=58),
            "hire_date": hire_date,
            "termination_date": None,
            "job_title": title,
            "department_id": dept_id,
            "manager_id": manager_id,
            "employment_type": emp_type,
            "work_city": data["departments"][dept_id - 1]["location"] if rng.random() < 0.75 else rng.choice(CITIES),
            "status": "Active",
        }

    company_start = today.replace(year=today.year - 8)
    emps = data["employees"]
    emps.append(person(1, 1, "Chief Executive Officer", None, company_start))
    heads = {}
    for dept_id, (name, *_rest) in enumerate(DEPARTMENTS, start=1):
        emp_id = len(emps) + 1
        emps.append(person(emp_id, dept_id, f"Head of {name}", 1,
                           fake.date_between(company_start, today.replace(year=today.year - 3))))
        heads[dept_id] = emp_id

    weights = [d[3] for d in DEPARTMENTS]
    while len(emps) < n_employees:
        dept_id = rng.choices(range(1, len(DEPARTMENTS) + 1), weights=weights)[0]
        titles = DEPARTMENTS[dept_id - 1][4]
        emp_id = len(emps) + 1
        hire = fake.date_between(company_start, today - dt.timedelta(days=7))
        emp = person(emp_id, dept_id, rng.choice(titles), heads[dept_id], hire)
        # ~14% attrition, ~4% currently on long leave
        roll = rng.random()
        if roll < 0.14 and (today - hire).days > 120:
            emp["termination_date"] = fake.date_between(hire + dt.timedelta(days=90), today - dt.timedelta(days=1))
            emp["status"] = "Terminated"
        elif roll < 0.18:
            emp["status"] = "On Leave"
        emps.append(emp)

    # Salaries: a joining salary plus yearly hikes, the last one open-ended.
    for emp in emps:
        dept = DEPARTMENTS[emp["department_id"] - 1]
        low, high = dept[5]
        if emp["job_title"].startswith("Chief"):
            ctc = 12_000_000
        elif emp["job_title"].startswith("Head"):
            ctc = high * 100_000 * rng.uniform(1.1, 1.5)
        else:
            ctc = rng.uniform(low, high) * 100_000
            if emp["employment_type"] == "Intern":
                ctc = rng.uniform(2.4, 4.8) * 100_000
        start = emp["hire_date"]
        end_of_employment = emp["termination_date"] or today
        while True:
            next_hike = start.replace(year=start.year + 1) if not (start.month == 2 and start.day == 29) else start + dt.timedelta(days=365)
            is_last = next_hike > end_of_employment
            data["salaries"].append({
                "id": len(data["salaries"]) + 1,
                "employee_id": emp["id"],
                "annual_ctc": round(ctc, -3),
                "effective_from": start,
                "effective_to": (emp["termination_date"] if is_last else next_hike - dt.timedelta(days=1)),
            })
            if is_last:
                break
            ctc *= rng.uniform(1.04, 1.15)
            start = next_hike

    # Leave requests over the last ~18 months.
    leave_start = today - dt.timedelta(days=540)
    for emp in emps:
        window_start = max(emp["hire_date"], leave_start)
        window_end = emp["termination_date"] or today + dt.timedelta(days=30)
        if window_start >= window_end:
            continue
        for _ in range(rng.randint(1, 9)):
            leave_type = rng.choices(LEAVE_TYPES, weights=[35, 40, 25])[0]
            start = fake.date_between(window_start, window_end)
            days = rng.randint(1, 3) if leave_type != "Earned" else rng.randint(2, 10)
            data["leave_requests"].append({
                "id": len(data["leave_requests"]) + 1,
                "employee_id": emp["id"],
                "leave_type": leave_type,
                "start_date": start,
                "end_date": start + dt.timedelta(days=days - 1),
                "days": days,
                "status": "Pending" if start > today else rng.choices(["Approved", "Rejected"], weights=[90, 10])[0],
                "applied_on": start - dt.timedelta(days=rng.randint(0, 20)),
            })
        if emp["status"] == "On Leave":
            leave_type = "Maternity" if emp["gender"] == "Female" and rng.random() < 0.6 else rng.choice(["Earned", "Sick", "Paternity"])
            start = today - dt.timedelta(days=rng.randint(3, 40))
            days = {"Maternity": 130, "Paternity": 10, "Earned": 20, "Sick": 15}[leave_type]
            data["leave_requests"].append({
                "id": len(data["leave_requests"]) + 1,
                "employee_id": emp["id"],
                "leave_type": leave_type,
                "start_date": start,
                "end_date": start + dt.timedelta(days=days + 45),
                "days": days,
                "status": "Approved",
                "applied_on": start - dt.timedelta(days=14),
            })

    # Attendance for working days in the last 60 days (active employees only).
    for emp in emps:
        if emp["status"] != "Active":
            continue
        remote_bias = rng.uniform(0.05, 0.5)
        for day in _weekdays(max(emp["hire_date"], today - dt.timedelta(days=60)), today):
            status = rng.choices(["Present", "WFH", "Half-day", "Absent"],
                                 weights=[(1 - remote_bias) * 90, remote_bias * 90, 5, 5])[0]
            check_in = check_out = None
            if status != "Absent":
                check_in = dt.datetime.combine(day, dt.time(8, 30)) + dt.timedelta(minutes=rng.randint(0, 110))
                hours = rng.uniform(3.5, 4.5) if status == "Half-day" else rng.uniform(7.5, 10)
                check_out = check_in + dt.timedelta(hours=hours)
            data["attendance"].append({
                "id": len(data["attendance"]) + 1,
                "employee_id": emp["id"],
                "work_date": day,
                "check_in": check_in,
                "check_out": check_out,
                "status": status,
            })

    # Performance reviews for each completed half-year of employment (after 6 months tenure).
    for emp in emps:
        if emp["id"] == 1:
            continue
        end = emp["termination_date"] or today
        for period, period_end in _half_years(emp["hire_date"] + dt.timedelta(days=180), end):
            if period_end < today.replace(year=today.year - 3):
                continue
            rating = rng.choices([1, 2, 3, 4, 5], weights=[4, 12, 45, 29, 10])[0]
            data["performance_reviews"].append({
                "id": len(data["performance_reviews"]) + 1,
                "employee_id": emp["id"],
                "reviewer_id": emp["manager_id"] or 1,
                "review_period": period,
                "rating": rating,
                "comments": REVIEW_COMMENTS[rating],
            })

    # Recruitment: openings over the last year and their candidates.
    for _ in range(40):
        dept_id = rng.choices(range(1, len(DEPARTMENTS) + 1), weights=weights)[0]
        opened = fake.date_between(today - dt.timedelta(days=365), today - dt.timedelta(days=5))
        status = rng.choices(["Open", "Filled", "Cancelled"], weights=[35, 55, 10])[0]
        closed = None if status == "Open" else min(opened + dt.timedelta(days=rng.randint(20, 90)), today)
        opening_id = len(data["job_openings"]) + 1
        data["job_openings"].append({
            "id": opening_id,
            "title": rng.choice(DEPARTMENTS[dept_id - 1][4]),
            "department_id": dept_id,
            "opened_on": opened,
            "closed_on": closed,
            "status": status,
        })
        hired_one = False
        for _ in range(rng.randint(3, 25)):
            if status == "Filled" and not hired_one:
                stage, hired_one = "Hired", True
            elif status == "Open":
                stage = rng.choices(["Applied", "Screening", "Interview", "Offer", "Rejected"], weights=[35, 20, 15, 5, 25])[0]
            else:
                stage = "Rejected"
            data["candidates"].append({
                "id": len(data["candidates"]) + 1,
                "job_opening_id": opening_id,
                "full_name": fake.name(),
                "source": rng.choices(SOURCES, weights=[35, 20, 25, 10, 10])[0],
                "stage": stage,
                "applied_on": fake.date_between(opened, closed or today),
            })

    return data


def seed(url: str, n_employees: int = 300, today: dt.date | None = None) -> dict[str, int]:
    engine = create_engine(url)
    data = generate(n_employees, today or dt.date.today())
    metadata.drop_all(engine)
    metadata.create_all(engine)
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            rows = data[table.name]
            for i in range(0, len(rows), 2000):
                conn.execute(insert(table), rows[i:i + 2000])
    engine.dispose()
    return {name: len(rows) for name, rows in data.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--employees", type=int, default=300)
    parser.add_argument("--url", default=os.getenv("ADMIN_DATABASE_URL") or settings.database_url)
    args = parser.parse_args()
    counts = seed(args.url, args.employees)
    for name, count in counts.items():
        print(f"{name:22} {count:>7,} rows")
