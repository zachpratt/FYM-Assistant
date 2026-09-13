# TSAR Train Finder

A static, searchable view of the Fill Your Manifest TSAR rosters, plus an
optional read-only view of your own yards straight from your game folder.
Filter ~15,900 trains by location, railroad, type, symbol and status, read each
train's operating instructions, and see what is sitting in your yards.

**Live site:** https://zachpratt.github.io/FYM-Assistant/

The page is a single self-contained HTML file: no server, no build step at view
time, no dependencies. You can also open `docs/index.html` locally over HTTP
(`make serve`; browsers block the folder features on `file://`).

## What the site does

- **Find trains.** Pick a location to see the trains that originate, terminate
  or work there, then narrow by railroad, type (operator on the Shortline and
  Passenger rosters), symbol or status. A card expands to the route and the
  yardmaster's instructions; an `*IC*` interchange link jumps to the partner
  train.
- **Favorites.** The ☆ next to a yard's name saves it. Saved yards live in the
  Favorites dropdown and float to the top of the location list. Stored in the
  browser only.
- **Back button and shareable links.** Every location, view and filter change
  is a browser history entry, and the address bar always describes the page
  (`#loc=…&v=…&sym=…`), so a link to a train or a yard can be sent to someone.
- **Route a car (beta).** Collapsed behind a toggle under the update line: a
  from/to search that proposes train chains built only from what the rosters
  say, ranked with the shortline agreement table, MIM families and geography.
- **Your own yards (optional).** *Open my FYM folder* points the page at your
  Freight Yard Manager folder. It reads a handful of the game's files in the
  browser, writes nothing, and uploads nothing:
  - `FYMMyMaps.ini` for the yards assigned to you (starred automatically),
  - `yards/<id>.wag` for a yard's cars, cuts, destinations, load state and
    how long each car has waited,
  - the yard's `.nam`, `.set` and `.hcf` files for your sorts: group the
    yard by your own sort table, with each sort's definition and colour.
  Chrome and Edge remember the folder; Firefox and Safari ask each visit. On
  Windows, Chrome and Edge cannot read `.ini` files through a folder handle,
  so the setup asks once for `FYMMyMaps.ini` (or skip it and star your yards
  yourself). The `?` button reopens the walkthrough.

## Updating after the game publishes new TSARs or maps

```
1. make update          # mirror the Dropbox game folder, refresh the map tables, strict rebuild
2. git add -A && git commit -m "TSAR update" && git push
```

GitHub Pages republishes from `docs/` on `main` within about a minute.

`make update` is three steps:

1. `make sync` (`game_sync.py`): rsync the useful, non-image part of the game
   folder into `game_data/` and commit a snapshot into a private git repo
   inside it, so `git -C game_data log --stat` shows what the game changed.
   The game folder defaults to `~/Dropbox/Freight Yard Manager`; set
   `FYM_GAME_DIR` on a machine where it lives elsewhere.
2. `make tables` (`map_tables.py`): if any map file (`yards/*.yrd`, `*.his`)
   changed since the last import, regenerate `mims.csv` (`mim_import.py`) and
   the derived rows of `geo.csv` (`his_import.py`). Then name any location id
   that `FYMMyMaps.ini` or `mims.csv` knows but `locations.csv` does not, from
   the game's own revision notes (`MapRNotes.rtf`). An id the notes do not name
   is printed and left for a hand row, so the strict build stops on it.
3. `make check`: the build with `--strict`, which exits non-zero if anything
   about the input was unrecognised.

The build takes well under a second and prints what to read before you commit:
what the sync changed, the map-table and naming lines, the roster diff against
the previous build, and anything about the file format it did not recognise:

```
  changes since the last build (2026-07-28T15:24:46):
    CSX         3,060 ->    879  (-2,181)   <-- check the source file
    UP          2,224 ->  2,260  (+36)

  format check — unrecognised input:
         1x  note marker other than *IC* (new construct)
              e.g. *DPU*2x0 mid-train* in TSAR_UP.ini
```

The roster diff is how a truncated or half-downloaded `.ini` gets caught. The
format check is how a change to the game's format gets caught: the interpreter
knows `@@<location>` and `*IC*<symbol>` note markers, every declared train type,
every location id the tables refer to, and reports anything else rather than
silently rendering it.

Without a game folder you can still drop `TSAR_*.ini` files into `TSARs/` and
run `make site`.

## Naming locations

The rosters never list location names directly; they only appear incidentally
inside instruction text (`@@1450 Memphis Tennessee Yard - ...`). Names live in
`locations.csv` and are merged forward on every build, so a name learned once is
never lost when a later roster stops mentioning that stop. Today every id the
train data references is named.

Each row carries a `source`, and higher sources win:

- `manual`: hand-entered, never overwritten by anything.
- `map`: the game's own names, from the in-game map-ID screen (ids 1001–4009)
  and from `MapRNotes.rtf` for everything newer, added by `make update`.
- `scraped`: taken from instruction text; replaced by either of the above.

To rename a stop yourself, edit its row and set the source to `manual`:

```csv
id,name,source
1450,"Memphis Tennessee Yard, TN",map
3461,Barstow West Yard,manual        <- yours, never overwritten
```

## Layout

```
tsar_service.py     the interpreter and site generator (page CSS/JS inline)
game_sync.py        rsync + snapshot of the Dropbox game folder into game_data/
map_tables.py       map-table refresh + new-id naming, run by make update
mim_import.py       mims.csv from the game's .yrd map files (co-located yards, vids)
his_import.py       geo.csv derived rows from the .his map files (coordinates, railroads)
geo_import.py       geo.csv city rows from an offline GeoNames download (rare, manual)
locations.csv       location names, hand-editable
mims.csv            MIM families: which ids share a map
geo.csv             coordinates per location (derived from maps, city-level, hand rows)
railroad_ids.csv    the game's railroad-id table used by sort files (fallback; the
                    player's own FYMLocoCars6.ini overrides it in the browser)
interchange_data/   shortline partner table and derived exchange points
docs/index.html     the generated site (what GitHub Pages serves)
docs/build-report.json   per-build stats, diffed by the next build
game_data/          mirror of the game folder (input, not committed, own git history)
TSARs/              legacy drop-in folder for TSAR_<RAILROAD>.ini files (not committed)
```

`game_data/` and `TSARs/` are gitignored: the game's files are not ours to
redistribute, so this repo holds the interpreter, the tables and the generated
site but not the source data. The game-wide tables the page needs at view time
(car types, engine models, railroads, states) are baked into `docs/index.html`
at build time from the mirror.

## Notes on the file format

All rosters share one format. `tsar_service.py`'s docstring documents it in
full; the parts that differ between railroads are:

- **Train symbols.** The full symbol is `prefix + S` (UP `M` + `YROG-##` →
  `MYROG-##`), except on `TSAR_Shortline.ini` and `TSAR_Passenger.ini`, where the
  prefix is an operator's reporting mark and takes a space (`AMTK 1-##`).
  Symbols are shown exactly as the game has them, section digits and all.
- **Multi-railroad rosters.** Shortline and Passenger list one *operator* per
  type rather than one train type, so the type filter relabels itself
  "Operator" for those and is scoped to the selected railroads.
- **Interchange markers.** `*IC*` markers carry a
  `railroadIndex-typeId-trainIndex` pointer into the game's roster ordering, so
  they render as links to the partner train. Roughly a third of the pointers are
  stale, so resolution falls back to matching the symbol the marker itself names.

Eight rosters are built. `TSAR_KCS.ini` (a placeholder with one dummy train)
and `TSAR_Tutorial.ini` are skipped by folder discovery and excluded from the
mirror.
