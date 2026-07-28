# TSAR Train Finder

A static, searchable view of the Fill Your Manifest TSAR rosters. Filter ~15,900
trains by location, railroad, type and symbol, and read each train's operating
instructions.

**Live site:** https://zachpratt.github.io/FYM-Assistant/

The page is a single self-contained HTML file — no server, no build step at view
time, no dependencies. You can also just open `docs/index.html` locally.

## Updating after the game publishes new TSARs

```
1. drop the new TSAR_*.ini files into TSARs/   (replacing the old ones)
2. make site
3. git commit -am "TSAR update" && git push
```

GitHub Pages republishes from `docs/` on `main` within about a minute.

Step 2 takes well under a second and prints two things worth reading before you
commit — what changed since the last build, and anything about the file format it
did not recognise:

```
  changes since the last build (2026-07-28T15:24:46):
    CSX         3,060 ->    879  (-2,181)   <-- check the source file
    UP          2,224 ->  2,260  (+36)

  format check — unrecognised input:
         1x  note marker other than *IC* (new construct)
              e.g. *DPU*2x0 mid-train* in TSAR_UP.ini
```

The first block is how a truncated or half-downloaded `.ini` gets caught. The
second is how a change to the game's format gets caught: the interpreter knows
`@@<location>` and `*IC*<symbol>` note markers, and anything else it sees is
reported rather than silently rendered as body text.

`make check` is the same build with `--strict`, which exits non-zero if anything
was unrecognised. Use it if you ever want to gate the commit.

## Naming locations

The game's files never list location names directly — they only appear
incidentally inside instruction text (`@@1450 Memphis Tennessee Yard - ...`).
About 1,000 of ~2,800 stops get named that way; the rest show as `#1450`.

Names live in `locations.csv` and are merged forward on every build, so a name
discovered once is never lost when a later roster stops mentioning that stop.

To name a stop yourself, edit the row and set its `source` to `manual`:

```csv
id,name,source
1450,Memphis Tennessee Yard,scraped
3461,Barstow West Yard,manual        <- yours, never overwritten
```

`manual` rows always win over anything scraped from the notes and survive every
rebuild. Adding a row for an id that the notes never mention works too.

## Layout

```
TSARs/            the game's TSAR_<RAILROAD>.ini files (input, not committed)
tsar_service.py   the interpreter and site generator
locations.csv     accumulated location names, hand-editable
docs/index.html   the generated site (what GitHub Pages serves)
docs/build-report.json   per-build stats, used to diff the next build
```

`TSARs/` is gitignored — the game's roster files aren't ours to redistribute, so
this repo holds the interpreter and the generated site but not the source data.
Put your own copies of the `.ini` files in `TSARs/` and `make site` will build
from them.

## Notes on the file format

All nine rosters share one format. `tsar_service.py`'s docstring documents it in
full; the parts that differ between railroads are:

- **Train symbols.** The full symbol is `prefix + S` (UP `M` + `YROG-##` →
  `MYROG-##`), except on `TSAR_Shortline.ini` and `TSAR_Passenger.ini`, where the
  prefix is an operator's reporting mark and takes a space (`AMTK 1-##`).
- **Multi-railroad rosters.** Shortline and Passenger list one *operator* per
  type rather than one train type, so the type filter relabels itself
  "Operator" for those and is scoped to the selected railroads.
- **Interchange markers.** `*IC*` markers carry a
  `railroadIndex-typeId-trainIndex` pointer into the game's roster ordering, so
  they render as links to the partner train. Roughly a third of the pointers are
  stale, so resolution falls back to matching the symbol the marker itself names.

`TSAR_KCS.ini` is a placeholder containing one dummy `N/A` train; it parses
fine, there is just nothing in it.
