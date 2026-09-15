.PHONY: site check serve clean sync tables update

# Mirror the Dropbox game folder into game_data/ and snapshot it (see game_sync.py).
sync:
	python3 game_sync.py

# Map-derived tables (mims.csv, geo.csv) if the sync changed any map file,
# then names for new location ids from the game's revision notes.
tables:
	python3 map_tables.py
	python3 blocks_import.py

# The update loop in one go: sync, tables, then a strict rebuild.
update: sync tables check

# Rebuild docs/index.html from game_data/TSARs (or the legacy TSARs/ folder).
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
