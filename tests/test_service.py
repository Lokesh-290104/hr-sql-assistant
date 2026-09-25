from app.service import QueryAssistant

HEADCOUNT_SQL = (
    "SELECT d.name AS department, COUNT(*) AS headcount FROM employees e "
    "JOIN departments d ON d.id = e.department_id WHERE e.status <> 'Terminated' GROUP BY d.name"
)


def test_answers_question(fake_llm):
    llm = fake_llm({"sql": HEADCOUNT_SQL, "explanation": "Headcount per department.", "clarification": None})
    result = QueryAssistant(llm).ask("How many people are in each department?")

    assert result.status == "ok"
    assert result.columns == ["department", "headcount"]
    assert 1 <= result.row_count <= 8
    assert result.attempts == 1
    assert "LIMIT" in result.sql


def test_schema_context_and_rules_are_in_prompt(fake_llm):
    llm = fake_llm({"sql": "SELECT 1", "explanation": "", "clarification": None})
    QueryAssistant(llm).ask("anything")
    system, _ = llm.calls[0]

    assert "TABLE employees" in system
    assert "'On Leave'" in system  # real category values
    assert "'Human Resources'" in system  # department names sampled from the data
    assert "effective_to IS NULL" in system


def test_self_repairs_after_database_error(fake_llm):
    llm = fake_llm(
        {"sql": "SELECT full_name FROM employees", "explanation": "", "clarification": None},
        {"sql": "SELECT first_name, last_name FROM employees", "explanation": "Fixed.", "clarification": None},
    )
    result = QueryAssistant(llm).ask("List employees")

    assert result.status == "ok"
    assert result.attempts == 2
    _, repair_prompt = llm.calls[1]
    assert "SELECT full_name FROM employees" in repair_prompt
    assert "full_name" in repair_prompt.split("failed with this error:")[1]


def test_unsafe_sql_is_never_executed(fake_llm):
    bad = {"sql": "DELETE FROM employees", "explanation": "", "clarification": None}
    result = QueryAssistant(llm := fake_llm(bad, bad, bad)).ask("delete everyone")

    assert result.status == "error"
    assert result.rows == []
    assert len(llm.calls) == 3
    # The table still has its rows.
    count = QueryAssistant(fake_llm({"sql": "SELECT COUNT(*) AS n FROM employees", "explanation": ""})).ask("count")
    assert count.rows[0][0] > 0


def test_clarification_is_returned(fake_llm):
    llm = fake_llm({"sql": None, "explanation": "Ambiguous.", "clarification": "Which review period?"})
    result = QueryAssistant(llm).ask("Show good performers")

    assert result.status == "clarification"
    assert result.clarification == "Which review period?"


def test_manager_cannot_see_salaries(fake_llm):
    salary = {"sql": "SELECT AVG(annual_ctc) FROM salaries", "explanation": "", "clarification": None}
    llm = fake_llm(salary, salary, salary)
    result = QueryAssistant(llm).ask("Average salary?", role="manager")

    assert result.status == "error"
    assert "salaries" not in result.error  # users get a friendly message, not internals
    _, repair_prompt = llm.calls[1]
    assert "salaries" in repair_prompt.split("failed with this error:")[1]  # the LLM gets the details
    system, _ = llm.calls[0]
    assert "TABLE salaries" not in system  # restricted tables are hidden from the prompt too


def test_follow_up_includes_history(fake_llm):
    llm = fake_llm({"sql": "SELECT 1", "explanation": "", "clarification": None})
    history = [{"question": "Headcount by department?", "sql": HEADCOUNT_SQL}]
    QueryAssistant(llm).ask("Only women", history=history)
    _, user = llm.calls[0]

    assert "Headcount by department?" in user
    assert HEADCOUNT_SQL in user
    assert user.strip().endswith("Question: Only women")


def test_invalid_llm_output_is_an_error(fake_llm):
    result = QueryAssistant(fake_llm("I think you should SELECT stuff")).ask("hi")
    assert result.status == "error"
    assert "JSON" in result.error


def test_accepts_fenced_json(fake_llm):
    reply = '```json\n{"sql": "SELECT COUNT(*) AS n FROM departments", "explanation": "x"}\n```'
    result = QueryAssistant(fake_llm(reply)).ask("How many departments?")
    assert result.status == "ok"
    assert result.rows == [[8]]
