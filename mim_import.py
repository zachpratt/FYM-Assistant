#!/usr/bin/env python3
"""Derive mims.csv from a folder of FYM .yrd map files.

The game's maps are Multiple Identity Maps (MiMs): one 2D interface map
carries several controllable yard identities plus virtual off-map
destinations (vIDs). Each map's .yrd file declares them as `MIM:` blocks
(id = the LAST ':'-field; older editors write `MIM:<id>`, newer
`MIM:<n>:<id>`) followed by a pixel polygon. A file with no MIM block is a
single-identity map. Coordinates are per-map pixels, not geography.

Rules, validated against the complete 920-map corpus (2026-07-30):
  - the mother map is the FILENAME id, never the first-listed MIM
  - an id that has its own .yrd elsewhere is a real map; any other map's
    claim on it is a cross-reference ('ref'), not ownership
  - otherwise the id belongs to the claiming map where its zone is largest;
    a substantial zone (>= 50,000 px^2) is a co-located yard ('yard'),
    a small box is a virtual destination ('vid')
  - known map-author mislabelings are excluded outright (EXCLUDED below)

Usage:  python3 mim_import.py <folder-of-yrd-files>  [-o mims.csv]

The .yrd inputs are the game's data and are never committed; this script's
output, mims.csv, is (same split as TSARs/ vs locations.csv). Map updates
are rare, so refreshes are occasional and manual.
"""
import argparse
import csv
import glob
import os
import re

# Author errors ruled on by Zach (2026-07-30): the zone is mislabeled with an
# id whose real home is elsewhere; the true local id is unknown.
EXCLUDED = {
    ('1423', '2070'),   # New Orleans Avondale zone labeled Tumbler Ridge, BC
    ('2385', '2386'),   # Winchester, OR primary zone labeled Parkesburg, PA
}

YARD_MIN_PX2 = 50000


def polygon_area(pts):
    if len(pts) < 3:
        return 0
    s = 0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) // 2


def parse_yrd(path):
    """(map name, [(member id, zone px^2), ...]) — mother excluded."""
    base = os.path.basename(path).split('.')[0]
    mapname, mims, polys, cur = '?', [], {}, None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\r\n')
            if line.startswith('[Lines]'):
                break
            if line.startswith('Map='):
                mapname = line[4:]
            elif line.startswith('MIM:'):
                mid = line.split(':')[-1].strip()
                if not mid.isdigit():
                    raise ValueError(f'unparseable MIM line in {path}: {line!r}')
                cur = mid
                mims.append(mid)
                polys[mid] = []
            elif cur and re.match(r'^\d+:\d+$', line):
                x, y = line.split(':')
                polys[cur].append((int(x), int(y)))
            elif not line.strip():
                cur = None
    return mapname, [(m, polygon_area(polys.get(m, []))) for m in mims if m != base]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('folder', help='folder containing <id>.yrd files')
    ap.add_argument('-o', '--out', default='mims.csv')
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.folder, '*.yrd')),
                   key=lambda p: int(os.path.basename(p).split('.')[0]))
    if not files:
        raise SystemExit(f'no .yrd files in {a.folder}')

    mothers = {os.path.basename(p).split('.')[0] for p in files}
    claims = {}
    for p in files:
        base = os.path.basename(p).split('.')[0]
        _, members = parse_yrd(p)
        for m, area in members:
            if (base, m) not in EXCLUDED:
                claims.setdefault(m, []).append((base, area))

    rows = []
    for m, cl in sorted(claims.items(), key=lambda kv: int(kv[0])):
        if m in mothers:
            rows.extend((mo, m, 'ref', area) for mo, area in cl)
            continue
        owner = max(cl, key=lambda x: x[1])[0]
        for mo, area in cl:
            kind = ('yard' if area >= YARD_MIN_PX2 else 'vid') if mo == owner else 'ref'
            rows.append((mo, m, kind, area))

    with open(a.out, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['mother_id', 'member_id', 'kind', 'zone_px2', 'source'])
        for mo, m, k, area in rows:
            w.writerow([mo, m, k, area, 'derived'])

    kinds = {}
    for _, _, k, _ in rows:
        kinds[k] = kinds.get(k, 0) + 1
    print(f'{len(files)} maps -> {len(rows)} member rows in {a.out}  {kinds}')


if __name__ == '__main__':
    main()
