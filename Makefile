.PHONY: test lint typecheck schemas

test:
	python -m pytest -q

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

# Regenerates JSON-schema exports into schemas/ from the Pydantic models.
schemas:
	python -m deeper.schemas
