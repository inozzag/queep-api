.PHONY: setup dev emulators test lint
setup:
	uv sync --locked
dev:
	uv run --env-file .env.example uvicorn queep.main:app --host 127.0.0.1 --port 8000
emulators:
	firebase emulators:start --project demo-queep
test:
	firebase emulators:exec --only auth,firestore,storage --project demo-queep 'uv run --env-file .env.example pytest -q'
lint:
	uv run ruff check .
