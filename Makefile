.PHONY: update
update:
	pipenv install && \
		pipenv run python dl.py && \
		git add data && \
		git commit -m "Latest data: $(date -u)" || exit 0 && \
		git push
