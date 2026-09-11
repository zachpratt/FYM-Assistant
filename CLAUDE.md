# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An interpreter for the Fill Your Manifest game's TSAR roster files
(`TSAR_<RAILROAD>.ini`) that generates a static train-finder site. One Python
script, no dependencies, no tests. The generated page is served by GitHub Pages
from `docs/` on `main` (https://zachpratt.github.io/FYM-Assistant/), so
**committing a rebuilt `docs/index.html` and pushing IS the deployment**.

## Commands

```
make sync      # mirror ~/Dropbox/Freight Yard Manager into game_data/ + snapshot commit
make update    # sync, then check
make site      # rebuild docs/index.html from game_data/TSARs (else TSARs/)  (~0.2s)
make check     # same build, exits non-zero on unrecognised input (--strict)
make serve     # build + serve at localhost:8000 (Chrome blocks file://)
```

`python3 tsar_service.py --help` for the full CLI (`--only UP,BNSF` builds a
subset; positional file args work too).

The TSAR update loop: `make update` (game_sync.py rsyncs the game folder into
`game_data/`, commits a snapshot in game_data's own private git repo, then
builds strict) → read the printed blocks (what the sync changed, roster diff vs
previous build, format-check anomalies) → commit → push. `git -C game_data
log --stat` / `diff` is the history of the game folder itself. The roster diff
is what catches a truncated download; the anomaly pass is what catches the
game changing its format.

## Architecture

Everything is in `tsar_service.py` (~1,100 lines, roughly half of which is
`HTML_TEMPLATE`, an inline raw string containing all page CSS/JS):

1. **Parse** (`parse_tsar`): every roster shares one INI-ish format —
   `[TypeInfo]` declaring types/prefixes, then `[<Type>_<typeid>_<n>]` sections
   with keys `S N O D R T F X`. Split key=value on the FIRST `=` only (keys
   contain spaces/punctuation, values contain `=`). Train type is resolved via
   the `_id` map in `[TypeInfo]`, never by regex on the section name.
2. **Anomalies**: anything unrecognised (novel note markers, missing/unknown
   keys, undeclared type ids, unregistered railroads, TotalCount mismatch, low
   IC link rate) is collected and reported, and fails `--strict`. Currently
   zero across all nine rosters — keep it that way: extend the parser AND the
   checks together.
3. **Locations** (`load/merge/save_location_store`): names exist only
   incidentally inside `@@<id> <name> - <instruction>` note markers. They
   accumulate in `locations.csv` (`id,name,source`) and are never dropped;
   `source=manual` rows always beat scraped ones. **Names are provisional** —
   the `@@` ids are map identifications and some scraped pairings are known to
   be wrong; ids are the ground truth, names are labels.
4. **Payload** (`build_payload`): resolves `*IC*<mark> <sym>*<rrIdx-typeId-trainIdx>*`
   interchange markers to train uids. The pointer indexes the game's own roster
   ordering (`RR_REGISTRY[..]['idx']`); ~⅓ of pointers are stale, so resolution
   falls back to matching the symbol the marker spells out.
5. **Emit** (`dump_payload`): JSON is written one train per line and `</` is
   escaped to `<\/`. Both are load-bearing: line-per-train keeps git diffs of
   `docs/index.html` proportional to the data change (a 3-train update = ~4
   lines), and the escape stops instruction text containing `</script>` from
   ending the inline script block.

`docs/build-report.json` is machine-read state, not just logging — the next
build diffs against it.

## Car router (beta, collapsed by default)

Merged from the former `routing-tools` branch on 2026-09-11. The "Route a
car (beta)" bar sits behind a `▸` toggle under the update banner, remembered
per browser (`localStorage` key `fym.router`), so the public page reads as the
plain train finder until a visitor opens it. Its data layers are built into
the payload regardless and are worth keeping current:

- `geo.csv` (lat/lon per identity; `his_import.py` derives rows from the
  map bundles' `.his` footers, `geo_import.py` adds city-level rows from an
  offline GeoNames download; derived rows win) → `DATA.geo`, the Details
  view, distance ranking and the terminal-road test.
- `interchange_data/` (shortline partner table + derived exchange points) →
  `DATA.ixp`; bad yard ids there are anomalies.
- `FREIGHT_CLASS` in `tsar_service.py` maps every declared train type to a
  freight capability; a new or stale type is an anomaly in `--strict`.
- `audit_directions` mirrors the JS pickup/setout regexes (keep both copies
  in sync); unclassified work-looking notes are stored in the build report
  and only NEW ones are printed on the next build.

## Player folder layer (client-side, read-only)

"Open my FYM folder" in the header lets a visitor point the page at their
own Freight Yard Manager folder. Chrome/Edge use `showDirectoryPicker`
with the handle kept in IndexedDB (`fym` / `handles` / `game`); other
browsers get a `webkitdirectory` input. The page only ever reads three
things, all parsed in JS inside `HTML_TEMPLATE` (section "player folder"):

- `FYMMyMaps.ini` — `<id>:<0|1>` per map; flag 1 = assigned to this
  player = **"my yards"** (Zach's ruling; a `.wag` also exists for yards he
  merely ran trains through, so never key "mine" on `.wag` presence).
  Assigned yards are auto-added to favorites. The file enumerates every
  map id the game knows, so diffing it against `locations.csv` is the
  new-id alarm.
- `FYMLocoCars6.ini` — `TypeID=` / `Name=` blocks, the car type names.
- `yards/<id>.wag` — the yard's inventory: `[TrainNumber=n]` cut blocks
  (`TrainName=`, `TrainCreator=`) each followed by `[CarID=n]` car blocks
  (`CarName=`, `TypeID=`, `TypeGroup=` F/E, `DestinationID=` where field
  1 is the next yard (id 1000 = "unassigned", a word, never a link),
  `IsLoaded=` 7 loaded / 6 empty, then `StartHistory`
  rows `yard#code#mm/dd/yyyy#train#player#n`; codes 10 arrived, 20
  departed, 40 loaded, 41 unloaded, 30/50/60 service and shop, 00 created).
  `parseWag` must reproduce the Python-derived figures for Fostoria 1091
  (714 cars, 46 cuts, 303 loaded, 385 empty, 194 staying, 107 idle > 1 y).

**Car-to-train join** (`yardJoin` in the same section): for each car's
destination, the active trains that board at this yard (originate or
explicit pickup) and reach the destination's map (terminate, explicit
setout, or pass through), gated by the car's class via `carries()` — the
same function the route finder uses, so the two never disagree. Car class
comes from `TypeID`/`ParentTypeID` (5, 6, 19 intermodal; 7 autorack; else
carload). A ★ marks a train whose instructions at this yard name the
destination's city. "Find a route ▸" hands a destination with no direct
train to the route finder.

**Sorts** (`parseSorts`, `sortFor`, `sortTrains`): `yards/<id>.nam` names
250 slots, `yards/<id>.set` holds `DisplaySetups` (per-operator views) and
`SortData`, one `<count>:<tokens…>:` line per slot. Tokens: id ≥ 1000 =
destination map; 1..58 = state per `FYMStates.ini`; `-1:60:<map>:<n>` =
industry n on that map (the car's waybill field 3); 500..999 = railroad id
followed by a state or 0 (`RR_IDS` holds the few proven ids; the table is
not in any readable file); 60 = bad orders; 62 = catch-all. Sort NAMES are
personal shorthand — never match on them (Mason City has "???????" and
"okokok UP PARSONS"). Zach's rulings (2026-09-11): the game is a cascade —
a car takes the first sort it matches — but an exact yard (or industry)
match beats a state match wherever it sits, so the implementation is
precedence industry > id > state > railroad > catch-all with the display
order breaking ties; the 62 catch-all is evaluated last even when it is
shown at the top (Fostoria's NS Bellevue), and Bad Orders (60) is pinned
above everything on screen. The `DisplaySetups` list is the on-screen
order top to bottom. The view is the named display carrying the yard's
dominant operator, else "all sorts" (slot order), overridable per yard.
Railroad ids are the game's own table (500..~818, not the Shortline
roster's `_id`, not alphabetical); `.set` stores railroads in the order
they were ticked, so a scratch sort ticked in picker order yields the
id→mark table (pending Zach). A sort's
trains are scored by token coverage over trains boarding here; same-map
sibling ids (Payne on the Fostoria map) match by exact id. Industry-only
sorts are local spots, not blocks.

Never write into the folder from the page. The native folder dialog cannot
be automated: verify the flow by fetching files from `game_data/` while
serving the repo root and feeding a `Map` of `File`s to `loadGame()`, then
have Zach click the real button once.

## Domain rules that look like bugs but aren't

- **Symbols are verbatim by decision.** Displayed symbol = `[TypeInfo prefix] +
  S=` value untouched. The digit in BNSF `Q-ALTLAC1-##` and the `%` in UP
  `MLEP%-##` are in the game's data (confirmed via the `*IC*` cross-references,
  which spell out full symbols). Do not strip or normalise them.
- **Prefix joining differs by roster kind.** Single-railroad files concatenate
  (`M` + `YROG-##` → `MYROG-##`); the two multi-operator files
  (`TSAR_Shortline.ini`, `TSAR_Passenger.ini`, flagged `multi` in
  `RR_REGISTRY`) use a space because the prefix is a reporting mark
  (`AMTK 1-##`). Those two also list one *operator* per "type", which is why
  the UI relabels the Type filter to "Operator" for them.
- **Sentinel values**: `F=2000-01-01` means "always effective",
  `X=2099-12-31` means "never expires", `##` in symbols is a day-of-month
  placeholder. `TSAR_KCS.ini` is a placeholder roster with one dummy train.
- **Note text whitespace matters**: leading indentation carries the blocking
  lists; blank `~~` segments are paragraph breaks. Preserve both.

## Repo boundaries

- `game_data/` (the Dropbox mirror; TSARs/, yards/, trains/, …) plus the
  legacy `TSARs/` and `sample_yard_data/` drop-in folders are **gitignored
  deliberately** — the game's data, not ours to redistribute. Never commit
  them or work around the ignore. `game_data/` has its own nested git repo;
  never push it anywhere. Folder discovery skips `TSAR_Tutorial.ini` and
  `TSAR_KCS.ini` (placeholder roster), and game_sync.py excludes both.
- `docs/`, `locations.csv` and `mims.csv` are **committed deliberately**
  (Pages serves `docs/`; the name store and the MIM-family table must persist
  across updates). Don't gitignore them. `mims.csv` is derived from the
  game's `.yrd` map files by `mim_import.py` — rerun only when maps change
  (rare); the build merely reads it.
- Game screenshots for the future map-ID extraction effort should also stay
  uncommitted.

## Verifying UI changes

No test suite. After touching `HTML_TEMPLATE`, rebuild, serve over HTTP, and
check in a browser: page renders with no console errors, a card expands
(route nodes, indented instructions), an interchange link jumps to its partner
train, and the Shortline/Passenger pills relabel the type filter to Operator.
