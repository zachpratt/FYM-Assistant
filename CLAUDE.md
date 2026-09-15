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
make tables    # mims.csv/geo.csv if maps changed; name new ids from MapRNotes.rtf; blocks.csv
make update    # sync, tables, then check
make site      # rebuild docs/index.html from game_data/TSARs (else TSARs/)  (~0.2s)
make check     # same build, exits non-zero on unrecognised input (--strict)
make serve     # build + serve at localhost:8000 (Chrome blocks file://)
```

`python3 tsar_service.py --help` for the full CLI (`--only UP,BNSF` builds a
subset; positional file args work too).

The update loop: `make update` (game_sync.py rsyncs the game folder into
`game_data/` and commits a snapshot in game_data's own private git repo;
map_tables.py reruns mim_import.py + his_import.py only if a `.yrd`/`.his`
changed since the mirror commit stamped in `game_data/.git/map_tables_head`,
then names any id in FYMMyMaps.ini or mims.csv that locations.csv lacks from
the game's own revision notes `MapRNotes.rtf` (source=map; an id the notes
do not name is printed and left for a hand row, so strict still stops on
it); then a strict build) → read the printed blocks (what the sync changed,
map tables, new names, roster diff vs previous build, format-check
anomalies) → `git add -A` → commit → push. `git -C game_data
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
browsers get a `webkitdirectory` input. The page only ever reads the
following, all parsed in JS inside `HTML_TEMPLATE` (section "player folder"):

- `FYMMyMaps.ini` — `<id>:<0|1>` per map; flag 1 = assigned to this
  player = **"my yards"** (Zach's ruling; a `.wag` also exists for yards he
  merely ran trains through, so never key "mine" on `.wag` presence).
  Assigned yards are auto-added to favorites. The file enumerates every
  map id the game knows, so diffing it against `locations.csv` is the
  new-id alarm.
- `FYMLocoCars6.ini` — `TypeID=` / `Name=` blocks, the car type names, and
  `[Engine Models]` rows `EM<n>,<model>,…` for locomotives, and
  `[Railroads]` (`RailroadID=n` / `Mark=`, 319 rows): the sort files'
  railroad token is `n + 499`, so an open folder overrides the baked-in
  `railroad_ids.csv` table (`RR_IDS`; reset on disconnect).
- `yards/<id>.wag` — the yard's inventory: `[TrainNumber=n]` cut blocks
  (`TrainName=`, `TrainCreator=`) each followed by `[CarID=n]` car blocks
  (`CarName=`, `TypeID=` — first colon field only; cabooses (`TypeGroup=C`)
  and engines append three `&H` paint colours — `TypeGroup=` F car / C
  caboose / `E:0:0:0:<engine model>:<n>` locomotive, `DestinationID=` where field
  1 is the next yard (id 1000 = "unassigned", a word, never a link),
  `IsLoaded=` 7 loaded / 6 empty, then `StartHistory`
  rows `yard#code#mm/dd/yyyy#train#player#n`; codes 10 arrived, 20
  departed, 40 loaded, 41 unloaded, 30/50/60 service and shop, 00 created).
  `parseWag` must reproduce the Python-derived figures for Fostoria 1091
  (714 cars, 46 cuts, 303 loaded, 385 empty, 194 staying, 107 idle > 1 y).

Locomotives are their own area at the top of the yard view (model from
`[Engine Models]`), never sorted, joined to a train, or grouped by cut or
destination; the header's car count excludes them.

**Car-to-train join** (`yardJoin` in the same section) — **built but hidden**
since the `car-trains` merge on 2026-09-13 (`YARD_TRAINS=false`): Zach wanted
the sorts live but judged the train suggestions not trustworthy yet, so group
headings show no train chips, no "find a route" link and no direct/connection
counts until the flag is flipped. The description below is what the flag
turns on: for each car's
destination, the active trains that board at this yard (originate or
explicit pickup) and reach the destination's map (terminate, explicit
setout, or pass through), gated by the car's class via `carries()` — the
same function the route finder uses, so the two never disagree. Car class
comes from `TypeID`/`ParentTypeID` (5, 6, 19 intermodal; 7 autorack; else
carload). A ★ marks a train whose instructions at this yard name the
destination's city. "Find a route ▸" hands a destination with no direct
train to the route finder. Trains are suggested only in group headings
(sort, destination); the per-car train column was dropped 2026-09-11 at
Zach's request because the per-car inference was not trustworthy enough.

**Sorts** (`parseSorts`, `sortFor`, `sortTrains`): `yards/<id>.nam` names
250 slots, `yards/<id>.set` holds `DisplaySetups` (per-operator views,
`<name>:<bool>:<bool>:<bound map ids>:<slots>`; a view bound to the yard's
own id is the default view) and `SortData`, one `<count>:<tokens…>:` line per slot, and `yards/<id>.hcf`
("V1.0", "<HumpColours>", then one `r:g:b` line per slot) gives each sort
its colour, shown as a swatch on every car row and optional as the row
order within cut/destination groups ("order cars by"). Tokens: id ≥ 1000 =
destination map; 1..58 = state per `FYMStates.ini`; `-1:60:<map>:<n>` =
industry n on that map (the car's waybill field 3); 500..999 = railroad id
followed by a state or 0 (`RR_IDS`: the folder's `[Railroads]` table when
open, else `railroad_ids.csv`); 60 = bad orders; 62 = catch-all. Sort NAMES are
personal shorthand — never match on them (Mason City has "???????" and
"okokok UP PARSONS"). Zach's rulings (2026-09-11): the game is a cascade —
a car takes the first sort it matches — but an exact yard (or industry)
match beats a state match wherever it sits, so the implementation is
precedence industry > id > state > railroad > catch-all with the display
order breaking ties; the 62 catch-all is evaluated last even when it is
shown at the top (Fostoria's NS Bellevue), and Bad Orders (60) is pinned
above everything on screen. A sort's TRAIN (`sortTrains`) is chosen in
trust order, every path gated by `carries()` on the block's car classes so
a unit/engine/non-revenue train is never suggested: "symbol" — the sort
name carries a boarding train's symbol core or its unique 4+-char leading
token (Pekin: MBNAS, LPD01, "1900 PEOR" = IMRR 1900 Powerton); "vote" —
the block's cars elect the train most of them have direct, needing ≥2
voters and ≥⅕ of the outbound cars; "tokens" — the definition scored
against stops; "name" — a place word, last resort. The heading labels
vote/tokens/name so the reader knows how much to trust it (Zach, 2026-09-11:
railroad-list sorts at Pekin mean "hand these to the home road's
manifest", not "that road's trains" — which is why token scoring is below
the vote). The `DisplaySetups` list is the on-screen
order top to bottom. The view is the named display carrying the yard's
dominant operator, else "all sorts" (slot order), overridable per yard.
Railroad ids are the game's own table (500..~818, not the Shortline
roster's `_id`, not alphabetical). RECOVERED 2026-09-11 by transcribing
the picker → `railroad_ids.csv` (247 rows, committed; the build bakes it
in as `DATA.rrids`, CSXT→CSX), then FOUND the same day in
`FYMLocoCars6.ini` `[Railroads]` as `RailroadID + 499` (all 247 rows
agree; the folder has 319). The CSV is the no-folder fallback; refresh it
from the folder table if the game adds railroads. A sort's
trains are scored by token coverage over trains boarding here; same-map
sibling ids (Payne on the Fostoria map) match by exact id. Industry-only
sorts are local spots, not blocks.

**Windows Chrome/Edge cannot read `.ini` files through a directory handle**
(found 2026-09-13). Chromium's File System Access API rejects any name
whose extension Safe Browsing rates DANGEROUS on the current platform
(`FileSystemAccessManagerImpl::IsSafePathComponent`), and `.ini` is
DANGEROUS on Windows only: `getFileHandle("FYMMyMaps.ini")` throws
TypeError "Name is not allowed" and `entries()` silently drops the file.
`.wag/.nam/.set/.hcf/.json/.zip` are unaffected and macOS never hits it.
Two consequences in the design:

- The game-wide tables are **baked into the page** (`load_game_tables`
  reads `FYMLocoCars6.ini` and `FYMStates.ini` from the folder above the
  TSARs, i.e. the mirror root → `DATA.game` = car types, parents, engine
  models, railroad ids (`RailroadID + 499`) and states). A readable folder
  copy still overrides them so a player whose game is newer than the build
  keeps the right names. Only `FYMMyMaps.ini` is personal.
- On that TypeError the page keeps the folder handle and asks for the one
  file through `showOpenFilePicker` (`pickIni`; the open picker has no such
  filter, only saves prompt). The intro modal doubles as the setup flow:
  step 1 "Open my FYM folder", step 2 "Choose FYMMyMaps.ini" appears only
  when Windows blocks it, and the header button reads "Finish setup" until
  it is done. Step 2 is skippable (Zach, 2026-09-13): the skip is stored
  with the folder (IndexedDB `skipini`), `GAME.mySource` becomes `"favs"`
  and "my yards" = the starred yards that have a `.wag` here (`myFromFavs`,
  kept current by `toggleFav`); nothing is auto-starred in that mode, the
  landing heading says "starred with an inventory file", and the modal's
  step 2 stays visible in a "skipped" state so the file can be chosen
  later, which clears the skip and restores the automatic list. The file handle goes to IndexedDB `ini` and its text to
  `initext`, so a reconnect reads it fresh while permission is granted and
  otherwise uses the cached copy (the landing panel says which; "choose
  them again" re-picks). Simulate on a Mac with a fake directory handle
  that throws that TypeError for `.ini` names and a stubbed
  `showOpenFilePicker`.

Never write into the folder from the page. The native folder dialog cannot
be automated: verify the flow by fetching files from `game_data/` while
serving the repo root and feeding a `Map` of `File`s to `loadGame()`, then
have Zach click the real button once.

## Block definitions and sort sheets

The UP and BNSF rosters write block tables in their notes ("Blocks leaving
Des Moines: 1. North Little Rock (LA, AR, ..., NS Region B2) - No CSX";
"Blocks created at Mason City: - Parsons (MDMNL Parsons)"); NS, CSX, CN and
CPKC only name blocks in @@ verbs. **The TSARs are the truth; a player's
sort files are their own shorthand and are never read or compared** (Zach,
2026-09-15). The goal is to help players build accurate sorts.

- `blocks_import.py` → `blocks.csv` (one row per table entry, members
  resolved into sort tokens `id: st: rr:MARK[:ST/..] rr:MARK@id
  region:RR:code ref:TRAIN:block all: not: class: catchall`, unresolved
  phrases kept verbatim) and `block_moves.csv` (every "@@yard - pick up /
  set out / create X block" verb, all six roads). Rerun after a TSAR update.
- `block_aliases.csv`: phrases a sort can't see through (terminals inside a
  map, nicknames, typos, regional shorthand), with a note per row. Grow it
  from the "most frequent unresolved phrases" the importer prints.
- `regions.csv` / `junctions.csv`: UP's published interline routing guides
  (UP-CP 2004, UP-CSX 2009, NS-UP 2019; PDFs gitignored in
  `routing_agreements/`) — the partner region codes the rosters cite, at
  state level with a confidence column (Zach's ruling: state level is all a
  sort can say), plus CN "Thunder Bay" shorthand rows. Junction tables are
  exact.
- `sort_sheet.py <id|name>`: a yard's sheet — every train that lifts,
  adds or creates a block there, each block flattened (references followed
  and shown as "via", regions expanded) and tagged specific / by state /
  by railroad / catch-all. Mason City 2124 is the reference case (Zach
  approved it 2026-09-15). `Sheet.yard_sheet(id)` is the data API.
- Baked into the page by `load_sheets` (`make tables` runs
  `blocks_import.py` so the tables follow every TSAR update): payload
  `sheets` (yard id → trains with `uid` and `[block index, how]` refs),
  `blocks` (5,994 distinct definitions, members as `[token, via]`, one per
  line) and `regions` ("RR:code" → states). +1.5 MB on the page (7.5 →
  9.0 MB). A bare city in a block is narrowed to the states the block's
  other members name (Arlington → TX in the Ft Worth block).
- Page (branch `sort-sheets`, 2026-09-15): a **Sorts** view on any location
  with a sheet (`sortsPanel`; hash `v=sorts`): each train as a jump link
  with its O→D, then its blocks — name, specificity tag (specific / by
  state-region / by railroad / catch-all, with the cascade advice in the
  intro line), how it got there ("created at here", "pick up here; defined
  at Butler", "named only"), the next train as a jump link, members as chips
  grouped under the reference they came through ("= MDMNL Parsons").
  Every card also gets a collapsed **Blocks defined in these notes**
  section (`trainBlocksHtml`) grouped by yard. Members that name a map no
  train stops at get their names carried into `locs` by `load_sheets`.

Known soft spots: hand-off references ("Overflow MITPS Parsons") resolve
to the block another train hands to that symbol; 27% of entries still
carry an unresolved phrase; "All Des Moines yards" expands to every Des
Moines map (six ids).

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
  game's `.yrd` map files by `mim_import.py` and `geo.csv`'s derived rows
  from the `.his` files by `his_import.py` (which keeps every non-derived
  row: geo_import.py's city rows and hand rows such as South St. Paul UP
  1658, whose map lost its coordinates in 2026-09); `make update` reruns
  both only when maps changed, and the build merely reads them.
- Game screenshots for the future map-ID extraction effort should also stay
  uncommitted.

## Verifying UI changes

No test suite. After touching `HTML_TEMPLATE`, rebuild, serve over HTTP, and
check in a browser: page renders with no console errors, a card expands
(route nodes, indented instructions), an interchange link jumps to its partner
train, and the Shortline/Passenger pills relabel the type filter to Operator.
