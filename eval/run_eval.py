"""Measure execution accuracy of the assistant against hand-written gold SQL.

A prediction counts as correct when its result matches the gold query's result:
same number of rows, and every gold column appears (as a multiset of values) in
some predicted column. So extra columns, column order and row order don't matter,
but the actual numbers and names do.

    python -m eval.run_eval                     # full schema context
    python -m eval.run_eval --compare           # full vs. minimal context (ablation)
    python -m eval.run_eval --limit 10 --delay 0
"""

import argparse
import datetime as dt
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import sqlglot

from app import db
from app.llm import GeminiClient
from app.service import QueryAssistant

HERE = Path(__file__).resolve().parent
GOLD_DIALECT = "mysql"


def _normalize(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return str(value).strip().lower()


def results_match(gold_rows: list[list], pred_rows: list[list]) -> bool:
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    gold_cols = [Counter(_normalize(r[i]) for r in gold_rows) for i in range(len(gold_rows[0]))]
    pred_cols = [Counter(_normalize(r[i]) for r in pred_rows) for i in range(len(pred_rows[0]))]
    unused = list(range(len(pred_cols)))
    for gold in gold_cols:
        match = next((i for i in unused if pred_cols[i] == gold), None)
        if match is None:
            return False
        unused.remove(match)
    return True


def gold_rows(sql: str) -> list[list]:
    dialect = db.dialect_name()
    if dialect != GOLD_DIALECT:
        sql = sqlglot.transpile(sql, read=GOLD_DIALECT, write=dialect)[0]
    _, rows, _ = db.run_query(sql, max_rows=100_000)
    return rows


def evaluate(questions: list[dict], detail: str, delay: float) -> dict:
    assistant = QueryAssistant(GeminiClient(), schema_detail=detail)
    records = []
    for i, q in enumerate(questions, start=1):
        expected = gold_rows(q["sql"])
        result = assistant.ask(q["question"])
        correct = result.status == "ok" and results_match(expected, result.rows)
        records.append({
            "id": q["id"],
            "category": q["category"],
            "question": q["question"],
            "status": result.status,
            "correct": correct,
            "attempts": result.attempts,
            "latency_ms": result.latency_ms,
            "sql": result.sql,
            "error": result.error or result.clarification,
        })
        mark = "PASS" if correct else "FAIL"
        print(f"  [{detail}] {i:>3}/{len(questions)} {mark}  {q['question']}", flush=True)
        if delay and i < len(questions):
            time.sleep(delay)  # stay under free-tier rate limits

    n = len(records)
    by_category = defaultdict(lambda: [0, 0])
    for r in records:
        by_category[r["category"]][0] += r["correct"]
        by_category[r["category"]][1] += 1
    return {
        "detail": detail,
        "questions": n,
        "execution_accuracy": sum(r["correct"] for r in records) / n,
        "valid_sql_rate": sum(r["status"] == "ok" for r in records) / n,
        "first_try_valid_rate": sum(r["status"] == "ok" and r["attempts"] == 1 for r in records) / n,
        "self_repaired": sum(r["status"] == "ok" and r["attempts"] > 1 for r in records),
        "avg_latency_ms": int(sum(r["latency_ms"] for r in records) / n),
        "by_category": {k: f"{c}/{t}" for k, (c, t) in sorted(by_category.items())},
        "records": records,
    }


def print_summary(report: dict) -> None:
    print(f"\n=== Schema context: {report['detail']} ({report['questions']} questions) ===")
    print(f"Execution accuracy : {report['execution_accuracy']:.1%}")
    print(f"Valid SQL          : {report['valid_sql_rate']:.1%} (first try {report['first_try_valid_rate']:.1%})")
    print(f"Fixed by self-repair: {report['self_repaired']}")
    print(f"Avg latency        : {report['avg_latency_ms']} ms")
    for category, score in report["by_category"].items():
        print(f"  {category:14} {score}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", action="store_true", help="also run with minimal schema context")
    parser.add_argument("--limit", type=int, help="only the first N questions")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds between LLM calls (free-tier rate limit)")
    args = parser.parse_args()

    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))[: args.limit]
    reports = [evaluate(questions, "full", args.delay)]
    if args.compare:
        reports.append(evaluate(questions, "minimal", args.delay))

    for report in reports:
        print_summary(report)
    if args.compare:
        full, minimal = reports[0]["execution_accuracy"], reports[1]["execution_accuracy"]
        print(f"\nSchema context improves execution accuracy by {100 * (full - minimal):.1f} percentage points.")

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"eval_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    print(f"\nDetailed results: {out}")


if __name__ == "__main__":
    main()
