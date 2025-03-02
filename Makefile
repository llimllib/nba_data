.PHONY: update
update:
	# have been getting errors like:
	# error: RPC failed; curl 92 HTTP/2 stream 7 was not closed cleanly: CANCEL (err 8)
	# and this seems to be the recommended solution
	git config --global http.postBuffer 157286400 && \
		git pull && \
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
