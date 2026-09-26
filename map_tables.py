#!/usr/bin/env python3
"""Keep the map-derived tables current after a sync (part of `make update`).

Two jobs, both cheap and idempotent:

  1. If any map file (yards/*.yrd or yards/*.his) changed in the mirror since
     the last time this ran, regenerate mims.csv (mim_import.py) and geo.csv
     (his_import.py). "Since last time" is the mirror commit recorded in
     game_data/.git/map_tables_head — inside the nested repo's .git so neither
     rsync nor either git repo sees it; when it is missing the imports simply
     run (a third of a second over all 924 maps).
  2. Name new location ids. Any id that FYMMyMaps.ini or mims.csv knows but
     locations.csv does not is looked up in the game's own revision notes
     (MapRNotes.rtf in the folder root, lines like "4036 Laurens, SC (vID)")
     and appended with source=map. Ids the notes do not name are printed and
     left alone, so the strict build still stops on them — a guessed name is
     worse than a stopped build.

Usage:  python3 map_tables.py [--force] [--dest game_data]
"""
import argparse
import csv
import os
import re
import subprocess
import sys

MIMS = 'mims.csv'
LOCS = 'locations.csv'
NOTES = 'MapRNotes.rtf'
HEADER = 'FYMHeader.ini'      # the game's Message= line carries the newest revision notes
STAMP = os.path.join('.git', 'map_tables_head')


def git(dest, *args):
    r = subprocess.run(['git', '-C', dest, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def maps_changed(dest, force):
    """-> (changed: bool, reason: str)."""
    head = git(dest, 'rev-parse', 'HEAD')
    if head is None:
        return True, 'mirror has no snapshot yet'
    if force:
        return True, '--force'
    stamp_path = os.path.join(dest, STAMP)
    if not os.path.isfile(stamp_path):
        return True, 'no record of a previous import'
    last = open(stamp_path).read().strip()
    if last == head:
        return False, 'no sync since the last import'
    if git(dest, 'cat-file', '-e', last) is None:
        return True, 'previous import predates the mirror history'
    out = git(dest, 'diff', '--name-only', last, head, '--', 'yards/*.yrd', 'yards/*.his')
    files = [f for f in (out or '').splitlines() if f]
    if files:
        return True, f'{len(files)} map file(s) changed since the last import'
    return False, 'no map files changed since the last import'


def stamp(dest):
    head = git(dest, 'rev-parse', 'HEAD')
    if head:
        with open(os.path.join(dest, STAMP), 'w') as fh:
            fh.write(head + '\n')


def run_import(script):
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    first = (r.stdout.strip().splitlines() or [''])[0]
    if r.returncode:
        sys.exit(f'  {script} failed:\n{r.stdout}{r.stderr}')
    print(f'    {script}: {first}')


def known_locations():
    ids = set()
    with open(LOCS, newline='') as fh:
        for row in csv.reader(fh):
            if row and row[0].strip().isdigit():
                ids.add(row[0].strip())
    return ids


def wanted_ids(dest):
    """Ids the game or the MIM table refer to: the new-id alarm's input."""
    ids = set()
    p = os.path.join(dest, 'FYMMyMaps.ini')
    if os.path.isfile(p):
        for line in open(p, encoding='utf-8', errors='replace'):
            i = line.split(':')[0].strip()
            if i.isdigit():
                ids.add(i)
    if os.path.isfile(MIMS):
        with open(MIMS, newline='') as fh:
            for row in csv.reader(fh):
                for i in row[:2]:
                    if i.strip().isdigit():
                        ids.add(i.strip())
    return ids


NOTE_LINE = re.compile(r'\b(\d{4}) ([^\\{}\r\n]+?) \((?:child|v|yard|map|parent|Map|Child|V)?ID\)')


def names_from_notes(dest):
    """{id: name} from the revision notes; RTF control words stripped first."""
    p = os.path.join(dest, NOTES)
    if not os.path.isfile(p):
        return {}
    raw = open(p, encoding='latin-1').read()
    txt = re.sub(r"\\'[0-9a-f]{2}", '?', raw)            # non-ASCII escapes: flag, don't guess
    txt = re.sub(r'\\[a-zA-Z]+-?\d* ?', '', txt)         # control words
    names = {}
    for m in NOTE_LINE.finditer(txt):
        i, name = m.group(1), m.group(2).strip()
        if '?' in name:
            continue
        names.setdefault(i, name)                        # first mention wins
    return names


HEAD_ITEM = re.compile(r'(\d{4}) ([A-Za-z][A-Za-z0-9 .&\'/-]*?) ([A-Z]{2})(?= \(|,|\.|$)')
HEAD_GROUP = re.compile(r'(New vIDs|New childIDs|New yards|New maps|New|Remade|Replacement|Renamed|Closed)\s*:\s*([^.]*)\.', re.I)


def names_from_header(dest):
    """{id: (name, closed)} from FYMHeader.ini's Message line, e.g.
    "18 September, New vIDs: 4038 Commerce City UP CO, ... Closed: 1172 Memphis
    Leewood Yard UP TN." Later mentions win (an id closed one week may be
    reused the next); "(was Newark Airport NJ)" is skipped."""
    p = os.path.join(dest, HEADER)
    if not os.path.isfile(p):
        return {}
    out = {}
    for line in open(p, encoding='utf-8', errors='replace'):
        # revision lines are dated: "23 September, Remade: 1235 Rosser GA, ..."
        if not re.match(r'^\d{1,2} [A-Z][a-z]+,', line):
            continue
        text = re.sub(r'\([^)]*\)', '', line)
        for group, body in HEAD_GROUP.findall(text):
            closed = group.lower() == 'closed'
            for i, name, st in HEAD_ITEM.findall(body.strip()):
                out[i] = (f'{name.strip()}, {st}', closed)
    return out


def apply_header(dest):
    """New / remade / replacement ids from the header become source=map rows,
    replacing any non-manual row (ids get reused: 1172 was Memphis Leewood
    one week and Waldron AR the next). Closed ids keep their row."""
    hdr = names_from_header(dest)
    if not hdr:
        return
    rows = list(csv.reader(open(LOCS, newline='')))
    by = {r[0]: n for n, r in enumerate(rows) if r and r[0].isdigit()}
    changed = []
    for i, (name, closed) in hdr.items():
        if closed:
            continue
        if i in by:
            r = rows[by[i]]
            src = r[2].strip().lower() if len(r) > 2 else 'manual'
            if src == 'manual' or r[1] == name:
                continue
            changed.append(f'{i} {r[1]} -> {name}')
            rows[by[i]] = [i, name, 'map']
        else:
            changed.append(f'{i} {name}')
            rows.append([i, name, 'map'])
    if changed:
        with open(LOCS, 'w', newline='') as fh:
            csv.writer(fh).writerows(rows)
        print(f'  locations: {len(changed)} id(s) named from {HEADER}: ' + '; '.join(changed))


def name_new(dest):
    apply_header(dest)
    missing = sorted(wanted_ids(dest) - known_locations(), key=int)
    if not missing:
        print('  locations: every id in FYMMyMaps.ini and mims.csv is named')
        return
    notes = names_from_notes(dest)
    named = [(i, notes[i]) for i in missing if i in notes]
    unnamed = [i for i in missing if i not in notes]
    if named:
        with open(LOCS, 'a', newline='') as fh:
            w = csv.writer(fh)
            for i, name in named:
                w.writerow([i, name, 'map'])
        print(f'  locations: named {len(named)} new id(s) from {NOTES}: '
              + ', '.join(f'{i} {n}' for i, n in named))
    if unnamed:
        print(f'  locations: {len(unnamed)} id(s) with no name in {HEADER} or {NOTES} — '
              f'add them to {LOCS} by hand (strict build will fail): ' + ', '.join(unnamed))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dest', default='game_data', help='the mirror (default: game_data)')
    ap.add_argument('--force', action='store_true', help='regenerate the tables regardless')
    a = ap.parse_args()
    if not os.path.isdir(os.path.join(a.dest, 'yards')):
        sys.exit(f'{a.dest}/yards not found — run make sync first')
    changed, why = maps_changed(a.dest, a.force)
    if changed:
        print(f'  map tables: regenerating ({why})')
        run_import('mim_import.py')
        run_import('his_import.py')
        stamp(a.dest)
    else:
        print(f'  map tables: up to date ({why})')
    name_new(a.dest)


if __name__ == '__main__':
    main()
