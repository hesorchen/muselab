.PHONY: test lint fmt run

test:
	uv run pytest -v

test-fast:
	uv run pytest -x --tb=short

lint:
	uv run ruff check backend tests
	bash scripts/lint.sh
	node --check frontend/app.js
	node --check frontend/i18n/index.js
	node --check frontend/data/constants.js
	node --check frontend/modules/file-capabilities.mjs
	node --check frontend/modules/persistent-cache.mjs

fmt:
	@echo "no formatter configured yet; consider adding ruff later"

run:
	uv run uvicorn backend.main:app --host 0.0.0.0 --port 8765 --reload
