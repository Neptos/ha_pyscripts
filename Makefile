# Test + coverage helpers for the HA pyscript suite.
# Usage: `make <target>` (run `make` alone for the list). Requires `make install` first.

PYTHON ?= python3

.PHONY: help install test coverage coverage-html lint clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-15s %s\n", $$1, $$2}'

install:  ## Install dev dependencies (pytest, pytest-cov)
	$(PYTHON) -m pip install -r requirements-dev.txt

test:  ## Run the test suite (fast, no coverage)
	$(PYTHON) -m pytest

coverage:  ## Run tests with a coverage report (fails if below the fail_under floor)
	$(PYTHON) -m pytest --cov --cov-report=term-missing

coverage-html:  ## Run tests and write a browsable HTML coverage report
	$(PYTHON) -m pytest --cov --cov-report=term-missing --cov-report=html
	@echo "HTML report written to htmlcov/index.html"

lint:  ## Check the pyscript files for pyscript-forbidden generator expressions
	$(PYTHON) -m pytest tests/test_no_genexpr.py

clean:  ## Remove test/coverage artifacts
	rm -rf .coverage htmlcov .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
