.PHONY: site check clean

# Rebuild docs/index.html from whatever is currently in TSARs/.
site:
	python3 tsar_service.py

# Same build, but fail if anything about the input format is unrecognised.
check:
	python3 tsar_service.py --strict

# Serve the built site locally at http://localhost:8000 (Chrome blocks file://).
serve: site
	cd docs && python3 -m http.server 8000

clean:
	rm -f docs/index.html docs/build-report.json
