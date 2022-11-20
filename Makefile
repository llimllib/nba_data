.PHONY: update
update:
	# installing pipenv is finicky - if you do a regular pip install pipenv, it
	# will remove pipenv from your user installation? I'm not sure what the
	# best way to get it by default for both local and automated processes is.
	#
	# Here I'm just going to assume it exists
	pipenv install && \
		pipenv run python dl.py && \
		git add data && \
		git commit -m "Latest data: $$(date -u)" || exit 0 && \
		git push
