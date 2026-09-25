# HR NL-to-SQL Assistant

FastAPI + MySQL app that turns HR questions into guarded, read-only SQL via an LLM
(Gemini or OpenRouter, chosen by `LLM_PROVIDER` in `.env`). See README.md.

## Testing

- Run: `.venv\Scripts\python -m pytest` (tests live in `tests/`, config in `pytest.ini`)
- Tests are offline: a fake LLM plus a throwaway SQLite database; no API keys or MySQL needed.
- New guardrail or pipeline behavior gets a test; bug fixes get a regression test.
- Never commit code that makes existing tests fail.

## Running

- App: `.venv\Scripts\python -m uvicorn app.main:app --port 8000` then open http://localhost:8000
- Accuracy eval (spends LLM quota): `.venv\Scripts\python -m eval.run_eval --limit 15`

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
