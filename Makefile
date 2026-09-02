.PHONY: test coverage

test:
	python -m pytest

coverage:
	python -m pytest --cov=src/niles --cov-branch --cov-report=term-missing
