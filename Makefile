.PHONY: update
update:
	git pull && \
		uv run python -m src.dl && \
		git add data && \
		git commit -m "Latest data: $$(date -u)" && \
		git push

# CI already has a checkout, so we shouldn't need to do a `git pull`
.PHONY: update-ci
update:
	uv run python -m src.dl && \
		git add data && \
		git commit -m "Latest data: $$(date -u)" && \
		git push

.PHONY: update-fixtures
update-fixtures:
	uv run tests/capture_responses.py

.PHONY: test
test:
	uv run pytest -v --no-header tests
