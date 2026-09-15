#!/usr/bin/env python3
"""Derive blocks.csv: the block definitions the TSAR notes spell out.

UP (and a few BNSF) roster notes carry tables like

    Blocks leaving Des Moines:
    1. North Little Rock (LA, AR, Poplar Bluff, UP Memphis, BNSF TN/MS/AL,
       NS Region B2, CN TN/MS/AL, Tenaha UP, Waskom TX, DQE) - No CSX
    ...
    Blocks created at Mason City:
    - Parsons (MDMNL Parsons)
    - ICTF IM (IM for Dolores ICTF) ->MDMHN

These are the authoritative statement of what each block carries — the
TSARs are the truth; a player's sort files are their own shorthand and are
never read here. Each member phrase is resolved into the vocabulary a sort
can express:

    id:<map id>          a named location (locations.csv, case-insensitive,
                         state suffix ignored; a bare city may match several)
    st:<XX>              a state or province
    rr:<MARK>[:XX/..]    a railroad, optionally limited to states
    region:<RR>:<code>   a partner region (regions.csv → its states)
    ref:<TRAIN>:<block>  another train's block, by reference ("MDMNL Parsons")
    all:<MARK>           "All CSX" — everything for that road
    catchall             "All other ... traffic"
    not:<MARK>           an exclusion ("- No CSX")

Phrases nothing matches are kept verbatim in the `unresolved` column and
counted, so the residue can be worked down with hand rows.

Usage:  python3 blocks_import.py [--folder game_data/TSARs] [-o blocks.csv]
"""
import argparse
import csv
import glob
import os
import re
import sys

STATES = set('''AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
AB BC MB NB NL NS NT NU ON PE QC SK YT'''.split())
CLASS1 = {'UP', 'BNSF', 'CSX', 'NS', 'CN', 'CPKC', 'CP', 'KCS', 'CSXT', 'BN'}

LEAD = re.compile(r'Blocks\s+(leaving|created at|built at|made at|added at|out of)\s+([^:~]+?)\s*:', re.I)
LEAD_BARE = re.compile(r'^\s*Blocks\s*:\s*$', re.I)                    # BNSF: "Blocks:" under an @@ yard
ITEM = re.compile(r'^\s*(?:\d+[.)]|-)\s*(.+?)\s*(?:\((.*)\))?\s*(?:-+>\s*([A-Za-z0-9 /,&-]+?))?\s*(?:-\s*No\s+([A-Z/ ]+))?\s*$')
QUAL = re.compile(r'\s+(setout|set out|pickup|pick up)?\s*blocks?$', re.I)
CLASS_HINT = [(re.compile(r'\b(autoracks?|autos?|automotive|multilevels?)\b', re.I), 'class:auto'),
              (re.compile(r'\b(IM|intermodal|containers?|stacks?)\b'), 'class:im')]
REF = re.compile(r'^(M[A-Z]{3,5}|[A-Z]-[A-Z]{6}\d?)\s+(.+)$')       # UP manifest symbol, or BNSF X-ABCDEF1
RR_STATES = re.compile(r'^([A-Z]{2,5})\s+((?:[A-Z]{2}/)*[A-Z]{2})$')   # "BNSF TN/MS/AL", "UP LA"
NOTE = re.compile(r"^(grouped by|terminating only|leftover|all traffic|traffic (for|to|from)|no connecting|as needed|see |per |if |int'l|53'|\d+ units|all outbound|loaded$|local$|(csx|ns|up|bnsf|cn|cpkc)( and (csx|ns|up|bnsf))? connections|same traffic)", re.I)
REGION = re.compile(r'^(CPKC|CP|CSX|NS|BNSF|UP|CN)\s+Regions?\s+([A-Z0-9]+(?:/[A-Z0-9]+)*)$')


def norm(text):
    """lower-case, unify Ft./Fort/St./Mt., drop brackets and punctuation"""
    t = text.lower()
    t = re.sub(r'\[[^\]]*\]', ' ', t)
    t = re.sub(r'\b(ft\.?|fort)\b', 'ft', t)
    t = re.sub(r'\b(st\.|saint)\b', 'st', t)
    t = re.sub(r'\bmt\.', 'mt', t)
    t = re.sub(r'\bstreet\b', 'st', t)
    t = re.sub(r'\bpt\.', 'pt', t)
    t = re.sub(r'^kc\b', 'kansas city', t)
    t = re.sub(r'^(s|so)\b', 'south', t)
    t = re.sub(r'^(n|no)\b', 'north', t)
    t = re.sub(r'^e\b', 'east', t)
    t = re.sub(r'^w\b', 'west', t)
    t = re.sub(r"[.'\u2019]", '', t)
    return re.sub(r'\s+', ' ', t).strip()


def load_locations(path):
    """norm(name without state) -> [(id, state)]"""
    by = {}
    with open(path, newline='') as fh:
        for row in csv.reader(fh):
            if not row or not row[0].isdigit():
                continue
            name = row[1]
            m = re.search(r',\s*([A-Z]{2})$', name)
            st = m.group(1) if m else ''
            key = norm(re.sub(r',\s*[A-Z]{2}$', '', name))
            by.setdefault(key, []).append((row[0], st))
    return by


def load_marks(path, game_ini=os.path.join('game_data', 'FYMLocoCars6.ini')):
    """reporting marks: railroad_ids.csv plus the game's own [Railroads] table"""
    marks = set()
    if os.path.isfile(path):
        with open(path, newline='') as fh:
            for row in csv.DictReader(fh):
                marks.add(row['mark'].strip().upper())
    if os.path.isfile(game_ini):
        for line in open(game_ini, encoding='utf-8', errors='replace'):
            if line.startswith('Mark='):
                marks.add(line[5:].strip().upper())
    return marks


def load_aliases(path):
    """norm(phrase) -> [tokens]; an empty token list means 'known, nothing a sort can say'"""
    out = {}
    if os.path.isfile(path):
        with open(path, newline='') as fh:
            for row in csv.DictReader(fh):
                out[norm(row['phrase'])] = row['tokens'].split()
    return out


def load_regions(path):
    """(railroad, code) -> states"""
    out = {}
    if os.path.isfile(path):
        with open(path, newline='') as fh:
            for row in csv.DictReader(fh):
                if row['side'] == 'UP':
                    continue
                rr = {'CP': 'CPKC'}.get(row['side'], row['side'])
                out[(rr, row['region'])] = row['states'].split(';')
    return out


def split_members(text):
    """Comma-separated, but 'Coffeyville UP/SKOL' and 'BNSF MN/ND' stay whole."""
    return [m.strip() for m in text.split(',') if m.strip()]


class Resolver:
    def __init__(self, locs, marks, regions, aliases):
        self.locs, self.marks, self.regions, self.aliases = locs, marks, regions, aliases
        self.unresolved = {}
        self.words = [(k, set(k.split()), v) for k, v in locs.items()]

    def place(self, phrase, state=''):
        """ids for a place phrase: exact, then 'All X yards', then a railroad
        qualifier stripped, then a city prefix, then every word present."""
        key = norm(phrase)
        m = re.match(r'^(.*\S)\s+([A-Z]{2})$', phrase.strip())
        if m and m.group(2) in CLASS1:
            m = None
        if not state and m and m.group(2) in STATES and norm(m.group(1)) in self.locs:
            key, state = norm(m.group(1)), m.group(2)
        pick = lambda hits: [i for i, st in hits if not state or st == state] or ([i for i, st in hits] if not state else [])
        m2 = re.match(r'^(.*\S)\s+([A-Z]{2,5}(?:/[A-Z]{2,5})+)$', phrase.strip())
        if m2 and all(x in self.marks or x in CLASS1 for x in m2.group(2).split('/')):
            ids = []
            for road in m2.group(2).split('/'):
                ids += self.place(f'{m2.group(1)} {road}', state)
            if ids:
                return ids
        cands = [key, re.sub(r'^all (.+?) yards?$', r'\1', key), re.sub(r' (up|bnsf|csx|ns|cn|cpkc|kcs|ptra)$', '', key)]
        for c in cands:
            if c in self.locs and pick(self.locs[c]):
                return pick(self.locs[c])
        if not state and m and m.group(2) in STATES:            # "Mansfield LA": prefix match within the state
            k2, st2 = norm(m.group(1)), m.group(2)
            hits = [x for k, hh in self.locs.items() if (k == k2 or k.startswith(k2 + ' ')) for x in hh if x[1] == st2]
            if hits:
                return [i for i, _ in hits][:12]
        pref = [x for k, hits in self.locs.items() if k.startswith(key + ' ') for x in hits]
        if pick(pref):
            return pick(pref)[:12]
        words = set(key.split()) - {'yard', 'yards', 'the'}
        if len(words) >= 2 or (len(words) == 1 and len(next(iter(words))) >= 5):
            sub = [x for k, ws, hits in self.words if words <= ws for x in hits]
            if pick(sub) and (len(words) >= 2 or len(pick(sub)) <= 4):
                return pick(sub)[:6]
        return []

    def resolve(self, phrase):
        p = phrase.strip().rstrip('.')
        up = p.upper()
        hints = [tok for rx, tok in CLASS_HINT if rx.search(p)]
        if NOTE.match(p):                       # prose about the block, not a member
            return hints
        p = re.sub(r'\s*-+>\s*[A-Za-z0-9-]+$', '', p)             # "Westfield ->ILBHO"
        p = re.sub(r'^(IM|intermodal|autos?|autoracks?|loaded autoracks?|loads?|empties)\s+for\s+', '', p, flags=re.I)
        p = re.sub(r'\s+(IM|intermodal|autoracks?|autos?)$', '', p, flags=re.I)
        if not p:
            return hints
        if norm(p) in self.aliases:
            return self.aliases[norm(p)] + hints
        if re.fullmatch(r'\d{4}', p):
            return [f'id:{p}'] + hints
        m = re.match(r'^(.*?)\s+(loaded|loads|empty|empties|manifest|manifest only|im only|only)$', p, re.I)
        if m and m.group(1):
            q = m.group(2).lower()
            hints += ['class:loaded'] if q in ('loaded', 'loads') else ['class:empty'] if q in ('empty', 'empties') else []
            p = m.group(1)
            if norm(p) in self.aliases:
                return self.aliases[norm(p)] + hints
        m = re.match(r'^(.+?)\s+(service area|area)$', p, re.I)
        if m:
            p = m.group(1)
        p = re.sub(r'\s+(UP|BNSF|CSX|NS|CN|CPKC|KCS|PTRA)(/(UP|BNSF|CSX|NS|CN|CPKC|KCS|PTRA))+$', '', p)
        out = self._resolve(p, p.upper())
        return out + hints

    def _resolve(self, p, up):
        m = REF.match(p)
        if m and m.group(2)[0].isupper():
            return [f"ref:{m.group(1)}:{QUAL.sub('', m.group(2)).strip().replace(' ', '_')}"]
        if re.match(r'^all other\b', p, re.I) or re.match(r'^(overflow|everything else)\b', p, re.I):
            return ['catchall']
        m = re.match(r'^All\s+([A-Z]{2,5})$', p)
        if m:
            return [f'all:{m.group(1)}']
        m = REGION.match(p)
        if m:
            rr = 'CPKC' if m.group(1) == 'CP' else m.group(1)
            toks = []
            for code in m.group(2).split('/'):
                if (rr, code) in self.regions:
                    toks.append(f'region:{rr}:{code}')
                elif code in self.marks:                # "CPKC Regions 2/3/DMVW" tucks a mark in
                    toks.append(f'rr:{code}')
                else:
                    toks.append(f'?region:{rr}:{code}')
            return toks
        if up in STATES:
            return [f'st:{up}']
        if re.fullmatch(r'[A-Z]{2}(/[A-Z]{2})+', up) and all(s in STATES for s in up.split('/')):
            return [f'st:{s}' for s in up.split('/')]
        m = RR_STATES.match(p)
        if m and (m.group(1) in CLASS1 or m.group(1) in self.marks) and all(s in STATES for s in m.group(2).split('/')):
            return [f'rr:{m.group(1)}:{m.group(2)}']
        if up in self.marks or up in CLASS1:
            return [f'rr:{up}']
        if re.fullmatch(r'[A-Z]{2,5}(/[A-Z]{2,5})+', up) and all(x in self.marks or x in CLASS1 for x in up.split('/')):
            return [f'rr:{x}' for x in up.split('/')]
        m = re.match(r'^([A-Z]{2,5})\s+((?:[A-Z]{2}/)*[A-Z]{2})\s+(west|east|north|south) of\b', p, re.I)
        if m and (m.group(1) in CLASS1 or m.group(1) in self.marks):
            return [f'rr:{m.group(1)}:{m.group(2)}']
        # "BNSF Houston", "UP Memphis": a road's traffic at a place
        m = re.match(r'^([A-Z]{2,5})\s+(.+)$', p)
        if m and (m.group(1) in CLASS1 or m.group(1) in self.marks) and m.group(2)[0].isupper():
            ids = self.place(m.group(2))
            if ids:
                return [f'rr:{m.group(1)}@{i}' for i in ids[:6]]
        # places, possibly "Dallas/Ft Worth"
        ids = self.place(p)
        if ids:
            return [f'id:{i}' for i in ids]
        if '/' in p:
            parts = [self.place(x) for x in p.split('/')]
            if all(parts):
                return [f'id:{i}' for ids in parts for i in ids]
        self.unresolved[p] = self.unresolved.get(p, 0) + 1
        return [f'?{p}']


def sections(path):
    t = open(path, encoding='latin-1').read()
    for m in re.finditer(r'^\[([^\]]+)\]\s*\n((?:(?!^\[).*\n?)*)', t, re.M):
        yield m.group(1), m.group(2)


def train_fields(body):
    f = {}
    for line in body.splitlines():
        k, _, v = line.partition('=')
        if k in ('S', 'O', 'D', 'T'):
            f[k] = v
    return f


def parse_roster(path, rr, res, locs_by_name, rows):
    prefix = {'UP': 'M', 'BNSF': ''}.get(rr, '')
    for sec, body in sections(path):
        f = train_fields(body)
        if 'T' not in f or 'Blocks' not in f['T']:
            continue
        sym = f.get('S', '')
        segs = [s.strip() for s in f['T'].split('~')]
        yard = kind = None
        at = None                                   # the last @@ yard the notes were talking about
        for seg in segs:
            mm = re.match(r'^@@(\d+)\s+([^-]+?)\s*-', seg)
            if mm:
                at = (mm.group(1), mm.group(2).strip())
            m = LEAD.search(seg)
            if m:
                kind, yard = m.group(1).lower(), m.group(2).strip().lstrip('>').strip()
                continue
            if LEAD_BARE.match(seg):
                kind, yard = 'at', (at[1] if at else f.get('O', ''))
                continue
            if yard is None:
                continue
            m = ITEM.match(seg.lstrip('>'))
            if not m:
                if re.match(r'^\s*(\d+\.|-)', seg):        # an item we could not parse
                    rows.append([rr, sym, yard, kind, seg.strip()[:80], '', '', '', '', '?unparsed'])
                elif seg and not seg.startswith(('@@', '<<', '>>', '--', 'HP/T')):
                    yard = None                            # table ended
                continue
            name, members, nxt, excl = m.group(1).strip(), m.group(2) or '', (m.group(3) or '').strip(), (m.group(4) or '').strip()
            name = QUAL.sub('', name).strip()
            toks = []
            if members.strip():
                for ph in split_members(members):
                    toks += res.resolve(ph)
            else:                                    # "1. Rolla Autos ->MNPDV": the name is the definition
                toks += res.resolve(name)
            if excl:
                toks += [f'not:{x.strip()}' for x in excl.split('/') if x.strip()]
            yid = '|'.join(res.place(yard)[:6])
            unres = [t[1:] for t in toks if t.startswith('?')]
            rows.append([rr, sym, yard, kind, name, nxt, members, ' '.join(t for t in toks if not t.startswith('?')), yid, '; '.join(unres)])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--folder', default=os.path.join('game_data', 'TSARs'))
    ap.add_argument('-o', '--out', default='blocks.csv')
    a = ap.parse_args()
    locs = load_locations('locations.csv')
    res = Resolver(locs, load_marks('railroad_ids.csv'), load_regions('regions.csv'), load_aliases('block_aliases.csv'))
    rows = []
    for p in sorted(glob.glob(os.path.join(a.folder, 'TSAR_*.ini'))):
        rr = os.path.basename(p)[5:-4].upper()
        if rr in ('KCS', 'TUTORIAL'):
            continue
        parse_roster(p, rr, res, locs, rows)
    with open(a.out, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['railroad', 'symbol', 'yard', 'kind', 'block', 'next_train', 'members', 'resolved', 'yard_ids', 'unresolved'])
        w.writerows(rows)
    entries = [r for r in rows if r[9] != '?unparsed']
    clean = sum(1 for r in entries if not r[9])
    ntok = sum(len(r[7].split()) for r in entries)
    nun = sum(len(r[9].split('; ')) for r in entries if r[9])
    by_rr = {}
    for r in entries:
        by_rr[r[0]] = by_rr.get(r[0], 0) + 1
    print(f'{len(entries):,} block entries ({", ".join(f"{k} {v:,}" for k, v in by_rr.items())}) from '
          f'{len({(r[0], r[1]) for r in entries}):,} trains at {len({r[2] for r in entries}):,} yards -> {a.out}')
    print(f'  {ntok:,} member tokens resolved, {nun:,} phrases unresolved; '
          f'{clean:,} entries ({clean * 100 // max(1, len(entries))}%) fully resolved; '
          f'{sum(1 for r in rows if r[9] == "?unparsed")} items unparsed')
    top = sorted(res.unresolved.items(), key=lambda x: -x[1])[:25]
    if top:
        print('  most frequent unresolved phrases:')
        for ph, n in top:
            print(f'    {n:4d}  {ph}')


if __name__ == '__main__':
    main()
