.PHONY: update
update:
	git pull && \
		uv run python src/dl.py && \
		git add data && \
		git commit -m "Latest data: $$(date -u)" && \
		git push

.PHONY: update-fixtures
update-fixtures:
	uv run tests/capture_responses.py

.PHONY: test
test:
	uv run pytest -v --no-header tests
