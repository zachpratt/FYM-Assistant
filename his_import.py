#!/usr/bin/env python3
"""Derive geo.csv (per-identity coordinates + railroads) from FYM .his files.

Each map's .his file ends with a footer holding the map's Long/Lat and an
RR: list; newer files also carry a "MIM Data" section with Parent/Child
blocks per identity: "<Name>, <ST> (<id>)" followed by its own RR: and
Long/Lat. Child ids sometimes carry suffixes ("1014X", "2517#") that do not
match the real location ids, so identities are resolved by NAME against
locations.csv first and by the parenthesised id second.

Only structural facts are extracted. Descriptions, author names, version
history and the YM (yardmaster) field are deliberately left behind: Zach is
holding off on republishing the map authors' prose and personal fields.

Usage:  python3 his_import.py <folder-of-his-files>  [-o geo.csv]
"""
import argparse
import csv
import glob
import os
import re

LATLON = re.compile(r'^(Lat|Long):\s*(-?\d+(?:\.\d+)?)\s*$', re.M)
IDENT = re.compile(r'^(?:Parent|Child)\s*$', re.M)
NAME_ID = re.compile(r'^(.*\S)\s*\((\d+)[A-Z#]*\)\s*$')


def parse_his(path):
    """Yield (name_or_None, id_or_None, lat, lon, rr) per identity block.
    The footer (map-level) block yields (None, None, lat, lon, rr)."""
    txt = open(path, encoding='utf-8', errors='replace').read()
    blocks = []
    mim = txt.split('MIM Data')
    if len(mim) > 1:
        # Parent/Child blocks: name line follows the Parent/Child marker
        for chunk in re.split(r'^(?=Parent|Child)', mim[1], flags=re.M):
            lines = [l.strip() for l in chunk.splitlines() if l.strip()]
            if not lines or lines[0].rstrip() not in ('Parent', 'Child'):
                continue
            name, lid = None, None
            for l in lines[1:4]:
                m = NAME_ID.match(l)
                if m:
                    name, lid = m.group(1).strip(), m.group(2)
                    break
            lat = lon = None
            rr = ''
            mlat = re.search(r'^Lat:\s*(-?\d+\.?\d*)', chunk, re.M)
            mlon = re.search(r'^Long:\s*(-?\d+\.?\d*)', chunk, re.M)
            mrr = re.search(r'^RR:\s*(.+)$', chunk, re.M)
            if mlat and mlon:
                lat, lon = float(mlat.group(1)), float(mlon.group(1))
            if mrr:
                rr = mrr.group(1).strip()
            if lat is not None:
                blocks.append((name, lid, lat, lon, rr))
        txt = mim[0]                      # footer fields live before MIM Data
    mlat = re.search(r'^Lat:\s*(-?\d+\.?\d*)', txt, re.M)
    mlon = re.search(r'^Long:\s*(-?\d+\.?\d*)', txt, re.M)
    mrr = re.search(r'^RR:\s*(.+)$', txt, re.M)
    if mlat and mlon:
        blocks.append((None, None, float(mlat.group(1)), float(mlon.group(1)),
                       mrr.group(1).strip() if mrr else ''))
    return blocks


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('folder', help='folder containing <id>.his files')
    ap.add_argument('-l', '--locations', default='locations.csv')
    ap.add_argument('-o', '--out', default='geo.csv')
    a = ap.parse_args()

    name_to_id, known = {}, set()
    with open(a.locations, newline='') as fh:
        for row in csv.reader(fh):
            if row and row[0].isdigit():
                known.add(row[0])
                name_to_id.setdefault(row[1].strip(), row[0])

    files = sorted(glob.glob(os.path.join(a.folder, '*.his')),
                   key=lambda p: int(os.path.basename(p).split('.')[0]))
    if not files:
        raise SystemExit(f'no .his files in {a.folder}')

    def sane(lat, lon):
        return 14 < lat < 72 and -170 < lon < -50    # North America incl. AK/MX

    def repair(lat, lon):
        """Authors sometimes swap the fields or drop the minus on Long."""
        for la, lo in ((lat, lon), (lon, lat), (lat, -abs(lon)), (lon, -abs(lat))):
            if sane(la, lo):
                return la, lo
        return None

    geo = {}          # id -> (lat, lon, rr)
    unresolved, rejected = [], []
    for p in files:
        base = os.path.basename(p).split('.')[0]
        for name, lid, lat, lon, rr in parse_his(p):
            if name is None:
                tid = base                       # footer = the mother map
            else:
                tid = name_to_id.get(name) or (lid if lid in known else None)
                if tid is None:
                    unresolved.append((base, name, lid))
                    continue
            fixed = repair(lat, lon)
            if fixed is None:
                rejected.append((base, tid, lat, lon))
                continue
            rr = re.sub(r'<[^>]*>', '', rr).strip(' ,')
            # prefer identity-block coords over a footer duplicate
            if tid not in geo or name is not None:
                geo[tid] = (fixed[0], fixed[1], rr)

    with open(a.out, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['id', 'lat', 'lon', 'rr', 'source'])
        for tid in sorted(geo, key=int):
            lat, lon, rr = geo[tid]
            w.writerow([tid, lat, lon, rr, 'derived'])

    print(f'{len(files)} .his files -> {len(geo)} located identities in {a.out}')
    if unresolved:
        print(f'{len(unresolved)} identity block(s) did not resolve to a known location:')
        for u in unresolved[:10]:
            print('   ', u)
    if rejected:
        print(f'{len(rejected)} coordinate(s) unrepairable (not North America either way):')
        for r in rejected[:10]:
            print('   ', r)


if __name__ == '__main__':
    main()
