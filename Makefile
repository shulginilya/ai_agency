.PHONY: install run repl test lint fmt typecheck eval docker-build docker-run check

install:
	uv sync --extra dev

run:
	uv run agency run "$(P)"

repl:
	uv run agency repl

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

typecheck:
	uv run mypy src evals

eval:
	uv run python -m evals.run_eval

check: lint typecheck test

docker-build:
	docker build -t agency:latest .

docker-run:
	docker compose run --rm agent run "$(P)"
