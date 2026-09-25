"""HR database schema, defined once with SQLAlchemy so it works on MySQL and SQLite.

Column comments double as documentation for the LLM: they are included in the
schema context sent with every question.
"""

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

metadata = MetaData()

departments = Table(
    "departments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(60), nullable=False, unique=True),
    Column("location", String(60), nullable=False, comment="Office city"),
    Column("annual_budget", Numeric(14, 2), comment="Yearly budget in INR"),
    comment="Company departments",
)

employees = Table(
    "employees",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("first_name", String(50), nullable=False),
    Column("last_name", String(50), nullable=False),
    Column("email", String(120), nullable=False, unique=True),
    Column("gender", String(10), comment="'Male', 'Female' or 'Other'"),
    Column("date_of_birth", Date),
    Column("hire_date", Date, nullable=False),
    Column("termination_date", Date, comment="NULL while the person still works here"),
    Column("job_title", String(80), nullable=False),
    Column("department_id", Integer, ForeignKey("departments.id"), nullable=False),
    Column("manager_id", Integer, ForeignKey("employees.id"), comment="Direct manager; NULL for the CEO"),
    Column("employment_type", String(20), nullable=False, comment="'Full-time', 'Part-time', 'Contract' or 'Intern'"),
    Column("work_city", String(60), nullable=False),
    Column("status", String(20), nullable=False, comment="'Active', 'On Leave' or 'Terminated'"),
    comment="One row per person ever employed. Current headcount = status <> 'Terminated'",
)

salaries = Table(
    "salaries",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("annual_ctc", Numeric(12, 2), nullable=False, comment="Annual cost-to-company in INR"),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, comment="NULL for the current salary; former employees have none"),
    comment="Salary history. Current salary = row with effective_to IS NULL",
)

leave_requests = Table(
    "leave_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("leave_type", String(20), nullable=False, comment="'Sick', 'Casual', 'Earned', 'Maternity' or 'Paternity'"),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("days", Integer, nullable=False, comment="Working days taken"),
    Column("status", String(20), nullable=False, comment="'Approved', 'Pending' or 'Rejected'"),
    Column("applied_on", Date, nullable=False),
    comment="Leave applications",
)

attendance = Table(
    "attendance",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("work_date", Date, nullable=False),
    Column("check_in", DateTime),
    Column("check_out", DateTime),
    Column("status", String(20), nullable=False, comment="'Present', 'WFH', 'Half-day' or 'Absent'"),
    comment="Daily attendance for working days in the last 60 days",
)

performance_reviews = Table(
    "performance_reviews",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("employee_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("reviewer_id", Integer, ForeignKey("employees.id"), nullable=False),
    Column("review_period", String(10), nullable=False, comment="Half-year, e.g. '2025-H2'"),
    Column("rating", Integer, nullable=False, comment="1 (poor) to 5 (outstanding)"),
    Column("comments", Text),
    comment="Half-yearly performance reviews",
)

job_openings = Table(
    "job_openings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(80), nullable=False),
    Column("department_id", Integer, ForeignKey("departments.id"), nullable=False),
    Column("opened_on", Date, nullable=False),
    Column("closed_on", Date, comment="NULL while still open"),
    Column("status", String(20), nullable=False, comment="'Open', 'Filled' or 'Cancelled'"),
    comment="Recruitment requisitions",
)

candidates = Table(
    "candidates",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job_opening_id", Integer, ForeignKey("job_openings.id"), nullable=False),
    Column("full_name", String(100), nullable=False),
    Column("source", String(20), nullable=False, comment="'LinkedIn', 'Referral', 'Naukri', 'Campus' or 'Careers Page'"),
    Column("stage", String(20), nullable=False, comment="'Applied', 'Screening', 'Interview', 'Offer', 'Hired' or 'Rejected'"),
    Column("applied_on", Date, nullable=False),
    comment="Job applicants",
)
