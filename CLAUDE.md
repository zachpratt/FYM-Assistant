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
make site      # rebuild docs/index.html from TSARs/  (~0.2s)
make check     # same build, exits non-zero on unrecognised input (--strict)
make serve     # build + serve at localhost:8000 (Chrome blocks file://)
```

`python3 tsar_service.py --help` for the full CLI (`--only UP,BNSF` builds a
subset; positional file args work too).

The TSAR update loop: replace files in `TSARs/` → `make site` → read the two
printed blocks (roster diff vs previous build, format-check anomalies) →
commit → push. The diff is what catches a truncated download; the anomaly pass
is what catches the game changing its format.

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

- `TSARs/` and `sample_yard_data/` are **gitignored deliberately** — the
  game's data, not ours to redistribute. TSARs/ must exist locally to build.
  Never commit them or work around the ignore.
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
