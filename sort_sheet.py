#!/usr/bin/env python3
"""Print a yard's sort sheet: what the rosters say the yard should be sorting.

For a yard, every train that lifts, adds or creates a block there (from the
@@ verbs in block_moves.csv) and every block table written at the yard
(blocks.csv), each block flattened into the vocabulary a sort can express:
locations, states, railroads (optionally per state), partner regions
expanded to their states, class hints, exclusions and the catch-all.
References to other trains' blocks ("MDMNL Parsons") are followed and the
chain is shown. A block a train names at the yard but never defines
anywhere in its notes is listed as "named only".

The TSARs are the only input; the player's own sort files are never read.

Usage:  python3 sort_sheet.py 2124            # by map id
        python3 sort_sheet.py "Mason City"    # by name (first match)
"""
import csv
import re
import sys
from collections import OrderedDict, defaultdict

STATE_NAMES = {}


def load_csv(path):
    with open(path, newline='') as fh:
        return list(csv.DictReader(fh))


def core(sym):
    """MSSDM-## / SSDM%-## / MSSDM -> SSDM (UP prefixes M on its manifests)"""
    s = sym.split('-')[0].rstrip('%')
    return s


def block_key(name):
    n = re.sub(r'\s+(setout|set out|pickup|pick up)?\s*blocks?$', '', name.strip(), flags=re.I)
    return re.sub(r'\s+', '_', n.strip().lower())


class Sheet:
    def __init__(self):
        self.blocks = load_csv('blocks.csv')
        self.moves = load_csv('block_moves.csv')
        self.locs = {r[0]: r[1] for r in csv.reader(open('locations.csv')) if r and r[0].isdigit()}
        self.regions = {}
        for r in load_csv('regions.csv'):
            rr = {'CP': 'CPKC'}.get(r['side'], r['side'])
            self.regions[(rr, r['region'])] = r
        # (railroad, symbol core, block key) -> [entries]
        self.defs = defaultdict(list)
        for b in self.blocks:
            self.defs[(b['railroad'], core(b['symbol']), block_key(b['block']))].append(b)

    def find_def(self, rr, sym, name):
        c = core(sym)
        for k in (c, c[1:] if rr == 'UP' and c.startswith('M') else None, 'M' + c if rr == 'UP' else None):
            if k and (rr, k, block_key(name)) in self.defs:
                return self.defs[(rr, k, block_key(name))]
        # "MITPS Parsons" may mean the Parsons block another train hands TO MITPS
        want = block_key(name)
        hits = [b for b in self.blocks if b['railroad'] == rr and block_key(b['block']) == want
                and b['next_train'] and core(b['next_train']).lstrip('M') == c.lstrip('M')]
        return hits

    def flatten(self, entry, seen=None, depth=0):
        """-> list of (token, via) with references followed"""
        seen = seen or set()
        out = []
        key = (entry['railroad'], core(entry['symbol']), block_key(entry['block']))
        if key in seen:
            return [('?loop', '')]
        seen = seen | {key}
        for tok in entry['resolved'].split():
            if tok.startswith('ref:'):
                _, train, name = tok.split(':', 2)
                targets = self.find_def(entry['railroad'], train, name.replace('_', ' '))
                if not targets:
                    out.append((f'?ref {train} {name.replace("_", " ")}', ''))
                    continue
                t = targets[0]
                out.append((f'= {train} "{t["block"]}" at {t["yard"]}', 'ref'))
                for tk, via in self.flatten(t, seen, depth + 1):
                    out.append((tk, via or f'{train} {t["block"]}'))
            else:
                out.append((tok, ''))
        return out

    def render_token(self, tok):
        kind, _, rest = tok.partition(':')
        if kind == 'id':
            return self.locs.get(rest, '#' + rest)
        if kind == 'st':
            return rest
        if kind == 'rr':
            mark, _, tail = rest.partition(':')
            if '@' in mark:
                mark, at = mark.split('@')
                return f'{mark} at {self.locs.get(at, "#" + at)}'
            return f'{mark} in {tail.replace("/", ", ")}' if tail else f'{mark} (all)'
        if kind == 'region':
            rr, _, code = rest.partition(':')
            r = self.regions.get((rr, code))
            if r:
                return f'{rr} region {code.replace("_", " ")} = {", ".join(r["states"].split(";"))}'
            return f'{rr} region {code}'
        if kind == 'all':
            return f'all {rest} traffic'
        if kind == 'not':
            return f'except {rest}'
        if kind == 'class':
            return {'im': 'intermodal', 'auto': 'autoracks', 'loaded': 'loads only', 'empty': 'empties only'}.get(rest, rest)
        if kind == 'catchall':
            return 'everything else (catch-all)'
        return tok

    def specificity(self, tokens):
        kinds = {t.split(':')[0] for t, _ in tokens}
        if 'catchall' in kinds:
            return 3
        if kinds & {'id'}:
            return 0
        if kinds & {'st', 'region'}:
            return 1
        return 2

    def yards(self):
        """every yard id with a sheet: a train lifts/adds/creates a block there, or a table is written there"""
        ids = {m['yard_id'] for m in self.moves if m['verb'] in ('pick up', 'add', 'create')}
        for b in self.blocks:
            ids.update(i for i in b['yard_ids'].split('|') if i)
        return sorted(ids, key=int)

    def block_data(self, b, how):
        toks = self.flatten(b)
        return {'name': b['block'], 'how': how, 'kind': b['kind'], 'next': b['next_train'],
                'spec': self.specificity(toks), 'members': [[t, via] for t, via in toks],
                'unresolved': b['unresolved'], 'yard': b['yard']}

    def yard_sheet(self, yid):
        """-> [{rr, sym, blocks: [block_data...]}] in roster order; [] when nothing lifts here"""
        trains = OrderedDict()
        for m in self.moves:
            if m['yard_id'] == yid and m['verb'] in ('pick up', 'add', 'create'):
                trains.setdefault((m['railroad'], m['symbol']), []).append((m['verb'], re.sub(r'^both\s+', '', m['block'])))
        tables = defaultdict(list)
        for b in self.blocks:
            if yid in b['yard_ids'].split('|'):
                tables[(b['railroad'], b['symbol'])].append(b)
        for key in tables:
            trains.setdefault(key, [])
        out = []
        for (rr, sym), verbs in trains.items():
            shown, blocks = set(), []
            for b in tables.get((rr, sym), []):
                shown.add(block_key(b['block']))
                blocks.append(self.block_data(b, b['kind'] + ' here'))
            for verb, bname in verbs:
                if block_key(bname) in shown:
                    continue
                shown.add(block_key(bname))
                defs = self.find_def(rr, sym, bname)
                if defs:
                    blocks.append(self.block_data(defs[0], f'{verb} here; defined at {defs[0]["yard"]}'))
                else:
                    blocks.append({'name': bname, 'how': f'{verb} here', 'kind': 'named', 'next': '', 'spec': None,
                                   'members': [], 'unresolved': '', 'yard': ''})
            out.append({'rr': rr, 'sym': sym, 'blocks': blocks})
        return out

    def sheet(self, yid):
        name = self.locs.get(yid, '#' + yid)
        print(f'{name} ({yid}) — sort sheet from the rosters\n')
        # trains that lift/add/create a block here, in roster order of appearance
        trains = OrderedDict()
        for m in self.moves:
            if m['yard_id'] == yid and m['verb'] in ('pick up', 'add', 'create'):
                key = (m['railroad'], m['symbol'])
                trains.setdefault(key, []).append((m['verb'], re.sub(r'^both\s+', '', m['block'])))
        # tables written at this yard
        tables = defaultdict(list)
        for b in self.blocks:
            if yid in b['yard_ids'].split('|'):
                tables[(b['railroad'], b['symbol'])].append(b)
        for key in tables:
            trains.setdefault(key, [])
        if not trains:
            print('  no roster train lifts a named block here')
            return
        for (rr, sym), verbs in trains.items():
            label = f'{rr} {sym}'
            print(f'{label}')
            shown = set()
            # blocks from the yard's own table first, in the author's order
            for b in tables.get((rr, sym), []):
                shown.add(block_key(b['block']))
                self.print_block(b, b['kind'] + ' here')
            for verb, bname in verbs:
                if block_key(bname) in shown:
                    continue
                shown.add(block_key(bname))
                defs = self.find_def(rr, sym, bname)
                if defs:
                    d = defs[0]
                    self.print_block(d, f'{verb} here; defined at {d["yard"]}')
                else:
                    print(f'  • {bname:<22} {verb} here — named only, no definition in this train\'s notes')
            print()

    def print_block(self, b, how):
        toks = self.flatten(b)
        spec = ['specific', 'by state / region', 'by railroad', 'catch-all'][self.specificity(toks)]
        nxt = f' → {b["next_train"]}' if b['next_train'] else ''
        print(f'  • {b["block"]:<22} {how}{nxt}   [{spec}]')
        for tok, via in toks:
            if tok.startswith('= '):
                print(f'      {tok}')
            elif tok.startswith('?'):
                print(f'      ?? {tok[1:]}')
            else:
                print(f'      {self.render_token(tok)}' + (f'   (via {via})' if via else ''))
        if b['unresolved']:
            print(f'      ?? not resolved: {b["unresolved"]}')


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    s = Sheet()
    arg = sys.argv[1]
    if not arg.isdigit():
        hits = [i for i, n in s.locs.items() if arg.lower() in n.lower()]
        if not hits:
            sys.exit(f'no location matches {arg!r}')
        arg = hits[0]
    s.sheet(arg)


if __name__ == '__main__':
    main()
