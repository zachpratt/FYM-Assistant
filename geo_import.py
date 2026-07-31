#!/usr/bin/env python3
"""Fill geo.csv with city-level coordinates matched from a GeoNames gazetteer.

One-off import tool in the mim_import.py mold: run it when new locations need
coordinates, not as part of the build (the build merely reads geo.csv).

    python3 geo_import.py path/to/cities1000.txt

Every location name in locations.csv carries its city and a two-letter
state/province suffix ("Birmingham 37th St. Yard, AL"). The longest leading
word-run of the name that names a gazetteer city in that state wins; if no
prefix matches, the first token is dropped once and the scan retried
("Mexico Eagle Pass Interchange UP, TX" -> Eagle Pass, TX). Matches append
`id,lat,lon,,city` rows to geo.csv. Rows already present — including the
precise source=derived ones transcribed from map authors — are never touched:
a later precise fix simply replaces its city-level placeholder by hand.

City-centroid accuracy (a few miles) is sufficient for every consumer in the
site: the route ranking's wandering penalty quantises at 25 mi, the corridor
tests use 1.2-1.35x ratios, and terminal-road detection uses a 35 mi diameter.
Unmatched names are printed for hand review.
"""

import csv
import sys

LOCATIONS = 'locations.csv'
GEO = 'geo.csv'

# GeoNames gives Canadian admin1 as two-digit codes; the game's names use
# postal abbreviations.
CA_ADMIN1 = {'01': 'AB', '02': 'BC', '03': 'MB', '04': 'NB', '05': 'NL',
             '07': 'NS', '08': 'ON', '09': 'PE', '10': 'QC', '11': 'SK',
             '12': 'YT', '13': 'NT', '14': 'NU'}

ABBREV = {'e': 'east', 'w': 'west', 'n': 'north', 's': 'south',
          'ft': 'fort', 'mt': 'mount', 'st': 'saint', 'ste': 'sainte'}


def norm(text):
    """lowercase, drop periods, expand direction/St./Ft. abbreviations."""
    out = []
    for tok in text.replace('.', ' ').split():
        tok = tok.lower().strip("'")
        out.append(ABBREV.get(tok, tok))
    return ' '.join(out)


def load_gazetteer(path):
    """(state, normalised name) -> (lat, lon); primary names beat alternates,
    bigger population beats smaller within each tier."""
    primary, alt = {}, {}
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            f = line.rstrip('\n').split('\t')
            if len(f) < 15:
                continue
            country, admin1 = f[8], f[10]
            if country == 'US':
                state = admin1
            elif country == 'CA':
                state = CA_ADMIN1.get(admin1)
            else:
                continue
            if not state:
                continue
            try:
                lat, lon, pop = float(f[4]), float(f[5]), int(f[14] or 0)
            except ValueError:
                continue
            for tier, names in ((primary, [f[1], f[2]]),
                                (alt, f[3].split(',') if f[3] else [])):
                for nm in names:
                    nm = norm(nm)
                    if not nm:
                        continue
                    key = (state, nm)
                    if key not in tier or pop > tier[key][2]:
                        tier[key] = (lat, lon, pop)
    return primary, alt


def match(name, primary, alt):
    """-> (lat, lon) for the longest city-name prefix, or None."""
    if ',' not in name:
        return None
    head, state = name.rsplit(',', 1)
    state = state.strip().upper()
    tokens = norm(head).split()
    for start in (0, 1):                       # retry once without token 0
        for end in range(min(len(tokens), start + 5), start, -1):
            key = (state, ' '.join(tokens[start:end]))
            hit = primary.get(key) or alt.get(key)
            if hit:
                return hit[0], hit[1]
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    primary, alt = load_gazetteer(sys.argv[1])
    print(f"gazetteer: {len(primary):,} primary + {len(alt):,} alternate names (US/CA)")

    have = {r[0] for r in csv.reader(open(GEO, encoding='utf-8'))
            if r and r[0] != 'id'}
    names = {r[0]: r[1] for r in csv.reader(open(LOCATIONS, encoding='utf-8'))
             if r and r[0] != 'id'}

    added, unmatched = [], []
    for lid in sorted((i for i in names if i not in have), key=int):
        hit = match(names[lid], primary, alt)
        if hit:
            added.append((lid, round(hit[0], 4), round(hit[1], 4)))
        else:
            unmatched.append(f"  #{lid}  {names[lid]}")

    with open(GEO, 'a', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        for lid, lat, lon in added:
            w.writerow([lid, lat, lon, '', 'city'])

    print(f"located {len(added):,} of {len(added) + len(unmatched):,} "
          f"unlocated ids at city level -> {GEO}")
    if unmatched:
        print(f"{len(unmatched)} name(s) need hand review:")
        print('\n'.join(unmatched))


if __name__ == '__main__':
    main()
