.PHONY: update
update:
	git pull && \
		uv run python dl.py && \
		git add data && \
		git commit -m "Latest data: $$(date -u)" && \
		git push
