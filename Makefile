.PHONY: test lint fmt run

test:
	uv run pytest -v

test-fast:
	uv run pytest -x --tb=short

lint:
	uv run python -m compileall -q backend tests

fmt:
	@echo "no formatter configured yet; consider adding ruff later"

run:
	uv run uvicorn backend.main:app --host 0.0.0.0 --port 8765 --reload
