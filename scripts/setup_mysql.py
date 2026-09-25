"""One-time MySQL setup: create the database, a SELECT-only app user, and demo data.

Reads credentials from .env (see .env.example):
    MYSQL_ROOT_PASSWORD   admin password you chose in MySQL Configurator
    MYSQL_APP_PASSWORD    new password for the read-only app user

Usage:
    python -m scripts.setup_mysql
"""

import os
import sys

from sqlalchemy import create_engine, text

from app.config import mysql_url
from scripts.seed import seed


def main() -> None:
    root_password = os.getenv("MYSQL_ROOT_PASSWORD")
    app_password = os.getenv("MYSQL_APP_PASSWORD")
    if not root_password or not app_password:
        sys.exit("Set MYSQL_ROOT_PASSWORD and MYSQL_APP_PASSWORD in .env first.")

    database = os.getenv("MYSQL_DATABASE", "hr_assistant")
    app_user = os.getenv("MYSQL_APP_USER", "hr_readonly")
    if not database.replace("_", "").isalnum() or not app_user.replace("_", "").isalnum():
        sys.exit("MYSQL_DATABASE and MYSQL_APP_USER may only contain letters, digits and underscores.")

    server = create_engine(mysql_url("root", root_password, None))
    with server.begin() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4"))
        conn.execute(text(f"CREATE USER IF NOT EXISTS '{app_user}'@'localhost' IDENTIFIED BY :pw"), {"pw": app_password})
        conn.execute(text(f"ALTER USER '{app_user}'@'localhost' IDENTIFIED BY :pw"), {"pw": app_password})
        # The app can only read. Even if every other guardrail failed, it cannot change data.
        conn.execute(text(f"GRANT SELECT ON `{database}`.* TO '{app_user}'@'localhost'"))
    server.dispose()
    print(f"Database '{database}' and read-only user '{app_user}' are ready.")

    counts = seed(mysql_url("root", root_password, database))
    for name, count in counts.items():
        print(f"  {name:22} {count:>7,} rows")

    app = create_engine(mysql_url(app_user, app_password, database))
    with app.connect() as conn:
        employees = conn.execute(text("SELECT COUNT(*) FROM employees")).scalar()
        try:
            conn.execute(text("DELETE FROM employees"))
            write_blocked = False
        except Exception:
            write_blocked = True
        conn.rollback()
    app.dispose()
    print(f"Read-only user sees {employees} employees; writes blocked: {write_blocked}")


if __name__ == "__main__":
    main()
