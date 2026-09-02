.DEFAULT_GOAL := help
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help venv install test lint fix check run build clean release-check

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtualenv
	@test -x $(BIN)/python || $(PY) -m venv $(VENV)

install: venv  ## Install the development dependencies
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements-dev.txt

test: install  ## Run the test suite
	$(BIN)/pytest -q

lint: install  ## Check formatting and lint rules
	$(BIN)/ruff check .

fix: install  ## Apply what ruff can fix on its own
	$(BIN)/ruff check --fix .

check: lint test  ## What CI runs

run: install  ## Run the bot from this checkout
	$(BIN)/python main.py

build: install  ## Build the sdist and the wheel
	$(BIN)/pip install -q build twine
	rm -rf dist build *.egg-info
	$(BIN)/python -m build
	$(BIN)/twine check dist/*
	$(BIN)/python .github/scripts/check_wheel.py

release-check: check build  ## Everything the release workflow will check
	@$(BIN)/python .github/scripts/release_notes.py \
	  $$($(BIN)/python -c 'import astolfo; print(astolfo.__version__)') >/dev/null
	@echo "ready to tag v$$($(BIN)/python -c 'import astolfo; print(astolfo.__version__)')"

clean:  ## Remove build and cache directories
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
