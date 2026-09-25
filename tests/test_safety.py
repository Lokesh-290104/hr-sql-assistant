import pytest

from app.safety import UnsafeQueryError, validate_sql

TABLES = {"employees", "departments", "salaries"}


def check(sql, dialect="mysql", tables=TABLES, max_rows=100):
    return validate_sql(sql, dialect=dialect, allowed_tables=tables, max_rows=max_rows)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM employees",
        "SELECT d.name, COUNT(*) FROM employees e JOIN departments d ON d.id = e.department_id GROUP BY d.name",
        "WITH current AS (SELECT * FROM employees WHERE status <> 'Terminated') SELECT COUNT(*) FROM current",
        "SELECT first_name FROM employees UNION SELECT name FROM departments",
        "SELECT * FROM employees WHERE id IN (SELECT employee_id FROM salaries WHERE annual_ctc > 100000);",
    ],
)
def test_allows_read_only_queries(sql):
    assert check(sql).sql


@pytest.mark.parametrize(
    "sql, message",
    [
        ("DELETE FROM employees", "read-only"),
        ("UPDATE salaries SET annual_ctc = 0", "read-only"),
        ("INSERT INTO employees (id) VALUES (1)", "read-only"),
        ("DROP TABLE employees", "read-only"),
        ("TRUNCATE TABLE salaries", "read-only"),
        ("GRANT ALL ON *.* TO 'x'@'%'", None),
        ("SELECT 1; DROP TABLE employees", "single SQL statement"),
        ("SELECT * FROM employees; SELECT * FROM salaries", "single SQL statement"),
        ("SELECT SLEEP(10)", "SLEEP"),
        ("SELECT BENCHMARK(1000000, MD5('a'))", "BENCHMARK"),
        ("SELECT LOAD_FILE('/etc/passwd')", "LOAD_FILE"),
        ("SELECT * FROM mysql.user", "not allowed"),
        ("SELECT * FROM information_schema.tables", "not allowed"),
        ("SELECT * FROM users", "does not exist"),
        ("SELECT * FROM employees FOR UPDATE", "LOCK"),
        ("SELECT * FROM employees INTO OUTFILE '/tmp/x'", None),
        ("WITH x AS (SELECT 1) DELETE FROM employees", None),
        ("", "No SQL"),
    ],
)
def test_rejects_unsafe_queries(sql, message):
    with pytest.raises(UnsafeQueryError, match=message):
        check(sql)


def test_role_restricted_table_is_rejected():
    with pytest.raises(UnsafeQueryError, match="salaries"):
        check("SELECT * FROM salaries", tables={"employees", "departments"})


def test_restricted_table_hidden_inside_subquery_is_rejected():
    with pytest.raises(UnsafeQueryError, match="salaries"):
        check(
            "SELECT * FROM employees WHERE id IN (SELECT employee_id FROM salaries)",
            tables={"employees", "departments"},
        )


def test_adds_limit_when_missing():
    assert check("SELECT * FROM employees").sql.endswith("LIMIT 100")


def test_caps_large_limit():
    assert check("SELECT * FROM employees LIMIT 100000").sql.endswith("LIMIT 100")


def test_keeps_small_limit():
    assert check("SELECT * FROM employees LIMIT 5").sql.endswith("LIMIT 5")


def test_returns_referenced_tables_without_ctes():
    result = check("WITH x AS (SELECT * FROM employees) SELECT * FROM x JOIN departments d ON 1 = 1")
    assert result.tables == ["departments", "employees"]
