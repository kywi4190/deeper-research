.PHONY: test lint typecheck schemas

test:
	python -m pytest -q

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

# Regenerates JSON-schema exports into schemas/ (no-op until the schema layer exists).
schemas:
	@echo "No schema exporter yet (added in Prompt 2)."
