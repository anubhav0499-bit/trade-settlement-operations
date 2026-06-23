.PHONY: help run dashboard test lint clean docker-build docker-up docker-down docker-test

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Local development ────────────────────────────────────────────────────

run:  ## Run the full settlement pipeline
	SETTLE_LOG_FORMAT=console python -m main

dashboard:  ## Launch the Streamlit dashboard
	streamlit run dashboard/app.py --server.port 8501

test:  ## Run the test suite
	python -m pytest tests/ -v --tb=short

lint:  ## Run ruff linter
	python -m ruff check src/ tests/ main.py dashboard/ backtest/

generate-data:  ## Generate synthetic reference & trade data
	python -m generators.synthetic_data

clean:  ## Remove generated artifacts
	rm -rf data/generated/settlement.db data/generated/*.pkl data/generated/*.hmac
	rm -rf data/knowledge_base/faiss_index/
	rm -rf __pycache__ .pytest_cache

# ── Docker ───────────────────────────────────────────────────────────────

docker-build:  ## Build all Docker images
	docker compose build

docker-up:  ## Run pipeline then dashboard
	docker compose up --build

docker-down:  ## Stop and remove containers
	docker compose down -v

docker-test:  ## Run tests in a container
	docker compose run --rm test

docker-pipeline:  ## Run only the pipeline container
	docker compose run --rm pipeline

docker-dashboard:  ## Start only the dashboard container
	docker compose up --build dashboard
