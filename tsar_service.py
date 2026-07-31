#!/usr/bin/env python3
"""
tsar_service.py  --  TSAR .ini  ->  static train-finder website
===============================================================

Reads every TSAR_<RAILROAD>.ini file in a folder (default: ./TSARs) and writes a
single self-contained HTML file. Players can filter trains by location, railroad,
type/operator and symbol, and read the operating instructions for each train.
No web server is required: open the file directly, or serve it from GitHub Pages.

UPDATING
--------
The game's .ini files change regularly. To refresh the site:

    1. drop the new TSAR_*.ini files into TSARs/   (replacing the old ones)
    2. make site                                   (or: python tsar_service.py)
    3. git commit -am "TSAR update" && git push    (GitHub Pages republishes)

Step 2 prints what changed against the previous build and flags anything about
the file format it did not recognise, which is how a truncated download or a
change to the game's format gets noticed rather than silently shipped.

USAGE
-----
    python tsar_service.py                       # reads ./TSARs, writes docs/index.html
    python tsar_service.py -f TSARs -o docs/index.html
    python tsar_service.py TSARs/TSAR_UP.ini TSARs/TSAR_BNSF.ini

    -f, --folder DIR      Folder of TSAR_*.ini files   (default: ./TSARs)
    -o, --out FILE        Output HTML file             (default: docs/index.html)
    -l, --locations FILE  Location name store, CSV id,name,source
                                                       (default: locations.csv)
    -t, --title TEXT      Page title                   (default: "Train Finder")
        --only CODE[,..]  Build only these railroads (e.g. --only UP,BNSF)
        --strict          Exit non-zero if anything about the format is unrecognised
        --no-location-cache  Do not read or update the location name store

FILE FORMAT (identical across all nine railroads, verified)
-----------------------------------------------------------
    [TypeInfo]
    TotalCount=<n>
    T=<Type A>;<Type B>;...            ; list of train types
    <Type>_id=<n>                      ; the type id used in section names
    <Type>_p=<prefix>                  ; symbol prefix (may be empty)
    <Type>_c=<n>  <Type>_m=<n>         ; counts, unused here

    [<Type>_<type id>_<n>]
    S=<symbol>        ; "##" = day-of-month placeholder, "%" = section letter
    N=<train name>    ; human name, often blank
    O=<origin loc id>
    D=<destination loc id>
    R=<comma separated route location ids>
    T=<notes, "~" separated>
    F=<effective date>   ; 2000-01-01 means "always"
    X=<expiry date>      ; 2099-12-31 means "never expires"

Note segments inside T:
    @@<loc id> <Location Name> - <instruction at this location>
    *IC*<MARK> <symbol>*<railroad index>-<type id>-<train index>*
    <anything else>      ; free text, indentation is significant

The full train symbol is  prefix + S  for single-railroad files (UP "M" + "YROG-##"
-> "MYROG-##"), and  prefix + " " + S  for the two multi-railroad files, where the
prefix is the operator's reporting mark ("AMTK" + " " + "1-##" -> "AMTK 1-##").
Both rules were confirmed against the *IC* cross-references, which spell out the
target's full symbol.
"""

import argparse, csv, glob, json, os, re, sys
from collections import Counter, defaultdict
from datetime import date, datetime

REPORT_NAME = 'build-report.json'

# Below this share of *IC* markers resolving to a real train, assume the game has
# renumbered its rosters rather than that the data merely drifted.
IC_LINK_FLOOR = 0.70

SECTION_RE = re.compile(r'^\[(.+)\]$')
SECNAME_RE = re.compile(r'^(.*)_(\d+)_(\d+)$')
RRNAME_RE  = re.compile(r'TSAR[_-]?(.+?)\.ini$', re.I)
AT_RE      = re.compile(r'^\s*@@(\d+)\s*(?:-\s*)?(.*)$', re.S)
IC_RE      = re.compile(r'^\s*\*IC\*(.*?)\*(\d+)-(\d+)-(\d+)\*\s*$', re.S)

# Segment kinds in the JSON payload (kept numeric to hold the file size down).
SEG_YARD, SEG_IC, SEG_TEXT, SEG_BREAK = 0, 1, 2, 3

# The game's own railroad ordering, which is what the first number of an *IC*
# pointer refers to. Verified by cross-checking every *IC* marker in every file
# against the reporting mark it names. Unlisted railroads still parse fine; their
# interchange markers simply render as plain text instead of clickable links.
RR_REGISTRY = {
    'PASSENGER': {'name': 'Passenger',  'color': '#7c93a8', 'idx': 0, 'multi': True, 'pax': True},
    'BNSF':      {'name': 'BNSF',       'color': '#f26a21', 'idx': 1},
    'CN':        {'name': 'CN',         'color': '#e23b3b', 'idx': 2},
    'CPKC':      {'name': 'CPKC',       'color': '#e6484f', 'idx': 3},
    'CSX':       {'name': 'CSX',        'color': '#3d7edb', 'idx': 4},
    'KCS':       {'name': 'KCS',        'color': '#b4954a', 'idx': 5},
    'NS':        {'name': 'NS',         'color': '#9aa4b1', 'idx': 6},
    'UP':        {'name': 'UP',         'color': '#ffd200', 'idx': 7},
    'SHORTLINE': {'name': 'Shortlines', 'color': '#2dd4a7', 'idx': 8, 'multi': True},
}
DEFAULT_COLOR = '#2dd4a7'

# ---------------------------------------------------------------------------
# Freight classification of every declared train type on the single-railroad
# rosters. The route finder needs to know what a train can carry, and the
# type NAMES are each road's own vocabulary ("US Central Division" is CPKC's
# carload local network), so a keyword match cannot be trusted. Every type a
# roster declares must appear here; a new type in a TSAR update is an anomaly
# until it is classified deliberately. Multi-operator rosters are not listed:
# their "types" are operators (shortlines = carload, passenger = passenger).
#
#   carload    - can carry general carload freight (manifests, locals, yard
#                jobs, transfers)
#   intermodal - containers/trailers
#   auto       - autoracks
#   unit       - single-commodity unit service, no general freight
#   engines    - light power / helpers, no cars
#   nonrev     - specials, company moves, placeholders; never route a car
#
# A value is one class or a tuple of classes: mixed service like "Manifest/IM"
# carries both carload and intermodal, and the car-type selector needs the
# full capability set, not a primary class.
# ---------------------------------------------------------------------------

FREIGHT_CLASSES = ('carload', 'intermodal', 'auto', 'unit', 'engines', 'nonrev')

FREIGHT_CLASS = {
    'BNSF': {
        'Baretable Intermodal (B)':               'intermodal',
        'Dimensional Special (J)':                'nonrev',      # AMBIGUOUS: high/wide specials do carry revenue loads
        'Domestic Intermodal (Q)':                'intermodal',
        'Helpers (K)':                            'engines',
        'High Priority Domestic Intermodal (Z)':  'intermodal',
        'High Priority High HPT Manifest (HH)':   'carload',
        'High Priority Low HPT Manifest (HL)':    'carload',
        'International Intermodal (S)':           'intermodal',
        'Light Engines (D)':                      'engines',
        'Local (L)':                              'carload',
        'Low Priority Manifest (M)':              'carload',
        'Road Switcher (R)':                      'carload',
        'Transfer (T)':                           'carload',
        'Unit Ag. Empty (X)':                     'unit',
        'Unit Ag. Loaded (G)':                    'unit',
        'Unit Coal Empty (E)':                    'unit',
        'Unit Coal Loaded (C)':                   'unit',
        'Unit Train (U)':                         'unit',
        'Vehicle (V)':                            'auto',
        'Yard Job (Y)':                           'carload',
    },
    'CN': {
        'Actual Blocked Manifest':                'carload',
        'CPR Origin/Shared Running':              'carload',     # AMBIGUOUS: run-through with CPKC; assumed to carry CN carload
        'Express Automotive':                     'auto',
        'Extra Trains':                           'nonrev',      # AMBIGUOUS: extras could be anything
        'Work Trains':                            'nonrev',
        'High Priority IM':                       'intermodal',
        'Locals':                                 'carload',
        'Manifest':                               'carload',
        'Quality Intermodal':                     'intermodal',
        'Roadswitchers':                          'carload',
        'Transfer Moves':                         'carload',
        'Uniform Bulk':                           'unit',
        'Unit Bulk':                              'unit',
        'Unit Coal':                              'unit',
        'Unit Grain':                             'unit',
        'Unit Sand/Sulphur':                      'unit',
        'Yard Jobs':                              'carload',
    },
    'CPKC': {
        'Canada Central Division':                'carload',
        'Canada East Division':                   'carload',
        'Canada Pacific Division':                'carload',
        'Canada Prairies Division':               'carload',
        'Expedited Merchandise':                  'carload',
        'Foreign Haulage/Non-Revenue':            'nonrev',      # AMBIGUOUS: name says non-revenue, but "foreign haulage" may carry freight
        'Mexico North Division':                  'carload',
        'Overflow and Detours':                   'nonrev',      # AMBIGUOUS: overflow sections may carry revenue freight; zero trains today
        'Priority IM/Autos/Manifest':             ('carload', 'intermodal', 'auto'),
        'Regional Freight':                       'carload',
        'US Central Division':                    'carload',
        'US East Division':                       'carload',
        'US Northeastern Division':               'carload',
        'US South Division':                      'carload',
        'US West Division':                       'carload',
        'Unit Autorack':                          'auto',
        'Unit Bulk':                              'unit',
        'Unit Coal/Petcoke':                      'unit',
        'Unit Crude Oil':                         'unit',
        'Unit Ethanol':                           'unit',
        'Unit Frac Sand':                         'unit',
        'Unit Grain':                             'unit',
        'Unit Molten Sulfur':                     'unit',
        'Unit Phosphate':                         'unit',
        'Unit Potash':                            'unit',
        'Unit Sulphur':                           'unit',
    },
    'CSX': {
        'Auto/IM':                                ('auto', 'intermodal'),
        'Automotive':                             'auto',
        'Coal':                                   'unit',
        'Coal DPU':                               'unit',
        'DPU Autorack':                           'auto',
        'DPU IM Priority':                        'intermodal',
        'DPU Intermodal':                         'intermodal',
        'DPU Manifest':                           'carload',
        'E. Coal DPU':                            'unit',
        'Empty Coal':                             'unit',
        'Ethanol':                                'unit',
        'Foreign Movements':                      'nonrev',      # AMBIGUOUS: foreign-road run-throughs; unclear if they take CSX cars
        'Grain':                                  'unit',
        'Intermodal':                             'intermodal',
        'Local':                                  'carload',
        'Manifest':                               'carload',
        'Manifest/IM':                            ('carload', 'intermodal'),
        'Misc. Priority':                         'carload',     # AMBIGUOUS: unclear what it carries; zero trains today
        'Misc. Unit':                             'unit',
        'Oil':                                    'unit',
        'Priority Intermodal':                    'intermodal',
        'Priorty IM Peak':                        'intermodal',
        'Yard Job/Local':                         'carload',
    },
    'KCS': {
        'N/A':                                    'nonrev',      # placeholder roster, one dummy train
    },
    'NS': {
        'Aggregates/Sand':                        'unit',
        'Auto / Manifest Mix':                    ('carload', 'auto'),
        'Autos':                                  'auto',
        'Baretable Intermodal':                   'intermodal',
        'DPU Manifest':                           'carload',
        'Ethanol/Oil ':                           'unit',        # trailing space is in the game's own TypeInfo
        'Intermodal':                             'intermodal',
        'Intermodal/Auto Mix':                    ('intermodal', 'auto'),
        'Light Engines':                          'engines',
        'Local':                                  'carload',
        'Manifest':                               'carload',
        'Manifest/IM Mix':                        ('carload', 'intermodal'),
        'Priority Intermodal':                    'intermodal',
        'Roadrailer':                             'intermodal',  # AMBIGUOUS: trailer trains; no conventional cars, closest fit
        'Specials/Company Trains':                'nonrev',
        'Suspended trains':                       'nonrev',
        'Unit':                                   'unit',
        'Unit Coal Empty':                        'unit',
        'Unit Coal Loaded':                       'unit',
        'Unit Grain Empty':                       'unit',
        'Unit Grain Loaded':                      'unit',
        'Yard Job':                               'carload',
    },
    'UP': {
        'Auto':                                   'auto',
        'Bulk Unit':                              'unit',
        'Coal':                                   'unit',
        'Expedited Z':                            'intermodal',
        'Grain Byproducts':                       'unit',
        'Grain Empties':                          'unit',
        'Grain Loads':                            'unit',
        'Grain Meal':                             'unit',
        'Grain Reposition':                       'unit',
        'Grain Shuttle':                          'unit',
        'Intermodal I':                           'intermodal',
        'Light Engines':                          'engines',
        'Local':                                  'carload',
        'Manifest':                               'carload',
        'Quality Intermodal':                     'intermodal',
        'Quality Manifest':                       'carload',
        'Rock':                                   'unit',
        'Special':                                'nonrev',
        'Unit':                                   'unit',
        'Unit Ethanol':                           'unit',
        'Work, MofW':                             'nonrev',
        'Yard':                                   'carload',
    },
}


class Anomalies:
    """Everything about the input that the interpreter did not expect.

    The point is that a format change should be loud. A new note marker or a
    renamed key would otherwise parse "successfully" into a page that is quietly
    missing information, and nobody would look at the .ini files again to find
    out why. Repeats are folded together so one new construct across 3,000
    trains reports as a single line with a count."""

    def __init__(self):
        self.items = Counter()
        self.examples = {}

    def add(self, kind, detail=''):
        self.items[kind] += 1
        if kind not in self.examples and detail:
            self.examples[kind] = detail

    def __len__(self):
        return len(self.items)

    def report(self):
        if not self.items:
            return
        print("\n  format check — unrecognised input:")
        for kind, n in self.items.most_common():
            eg = self.examples.get(kind, '')
            print(f"    {n:>6,}x  {kind}" + (f"\n              e.g. {eg}" if eg else ""))


def rr_meta(code):
    m = RR_REGISTRY.get(code, {})
    return {
        'name':  m.get('name', code),
        'color': m.get('color', DEFAULT_COLOR),
        'idx':   m.get('idx'),
        # A "multi" file lists one operator per type rather than one train type
        # per type: TSAR_Shortline.ini and TSAR_Passenger.ini.
        'multi': bool(m.get('multi')),
        # Passenger operators are excluded from freight-routing pools.
        'pax':   bool(m.get('pax')),
    }


def railroad_from_filename(path):
    base = os.path.basename(path)
    m = RRNAME_RE.search(base)
    return (m.group(1) if m else os.path.splitext(base)[0]).upper()


def norm_sym(s):
    """Fold a symbol to letters+digits so 'IHB GA6-##' and 'IHBGA6-##' compare equal."""
    return re.sub(r'[^A-Z0-9]', '', s.upper())


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def read_sections(path):
    """[Section] -> {key: value}. Splits on the FIRST '=' only: keys in [TypeInfo]
    contain spaces and punctuation ("Grain Shuttle_p", "Unit Sand/Sulphur_id") and
    note values contain '=', so nothing else is safe. Values are kept verbatim."""
    sections, cur = {}, None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.rstrip('\r\n')
            ms = SECTION_RE.match(line)
            if ms:
                cur = ms.group(1)
                sections.setdefault(cur, {})
            elif cur is not None and '=' in line:
                k, v = line.split('=', 1)
                sections[cur][k.strip()] = v
    return sections


TRAIN_KEYS = ('S', 'N', 'O', 'D', 'R', 'T', 'F', 'X')


def parse_tsar(path, anom):
    """Return a railroad dict: code, meta, types, prefixes and raw train records."""
    code = railroad_from_filename(path)
    meta = rr_meta(code)
    sections = read_sections(path)
    base = os.path.basename(path)

    if code not in RR_REGISTRY:
        anom.add("railroad not in RR_REGISTRY (no brand colour, cannot be an "
                 "interchange target)", f"{code} from {base}")
    if 'TypeInfo' not in sections:
        anom.add("file has no [TypeInfo] block", base)

    ti = sections.get('TypeInfo', {})
    types = [t for t in ti.get('T', '').split(';') if t]
    prefixes = {t: ti.get(t + '_p', '') for t in types}
    # type id -> type name, so a train's type never has to be guessed from a
    # section name that may itself contain underscores or trailing spaces.
    by_id = {ti.get(t + '_id', ''): t for t in types}

    trains = []
    for name, rec in sections.items():
        if name == 'TypeInfo':
            continue
        m = SECNAME_RE.match(name)
        if not m:
            anom.add("section name is not [<Type>_<type id>_<n>] (skipped)",
                     f"[{name}] in {base}")
            continue
        missing = [k for k in TRAIN_KEYS if k not in rec]
        if missing:
            anom.add(f"train section missing key(s): {','.join(missing)}",
                     f"[{name}] in {base}")
        extra = [k for k in rec if k not in TRAIN_KEYS]
        if extra:
            anom.add(f"train section has unknown key(s): {','.join(sorted(extra))}",
                     f"[{name}] in {base}")
        ttype = by_id.get(m.group(2))
        if ttype is None:
            anom.add("type id in section name is not declared in [TypeInfo]",
                     f"[{name}] in {base}")
            ttype = m.group(1)
        trains.append({
            'rr':  code,
            'ty':  ttype,
            'tid': m.group(2),
            'idx': m.group(3),
            's':   rec.get('S', ''),
            'nm':  rec.get('N', '').strip(),
            'o':   rec.get('O', ''),
            'd':   rec.get('D', ''),
            'r':   [n for n in rec.get('R', '').split(',') if n],
            'raw': rec.get('T', ''),
            'f':   rec.get('F', ''),
            'x':   rec.get('X', ''),
        })

    declared = ti.get('TotalCount', '')
    if declared.isdigit() and int(declared) != len(trains):
        anom.add("TotalCount disagrees with the number of train sections "
                 "(truncated download?)",
                 f"{base} declares {declared}, found {len(trains)}")

    used = {t['ty'] for t in trains}
    for t in used - set(types):
        anom.add("train type is not listed in [TypeInfo] T=", f"{t!r} in {base}")

    # Note markers. Anything delimited by '*' that is not *IC* is a construct
    # this interpreter has never seen; today there are none in any file.
    for t in trains:
        for seg in t['raw'].split('~'):
            st = seg.strip()
            if not st.startswith('*'):
                continue
            if st.startswith('*IC*') and not IC_RE.match(seg):
                anom.add("*IC* marker without a parseable "
                         "<railroad>-<type>-<train> pointer", f"{st[:70]} in {base}")
            elif not st.startswith('*IC*'):
                anom.add("note marker other than *IC* (new construct)",
                         f"{st[:70]} in {base}")

    return {'code': code, 'meta': meta, 'types': types, 'file': path,
            'prefixes': prefixes, 'trains': trains}


def full_symbol(rr, train):
    """prefix + S, or prefix + ' ' + S on the two multi-operator rosters."""
    p = rr['prefixes'].get(train['ty'], '')
    if not p:
        return train['s']
    return p + (' ' if rr['meta']['multi'] else '') + train['s']


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

def split_note(raw):
    """Yield (kind, payload) for each '~'-separated segment of a T= value."""
    if not raw:
        return
    for seg in raw.split('~'):
        if not seg.strip():
            yield 'break', None
            continue
        m = AT_RE.match(seg)
        if m:
            rest = m.group(2)
            dash = rest.find(' - ')
            if dash >= 0:
                yield 'yard', (m.group(1), rest[:dash].strip(), rest[dash + 3:].strip())
            else:
                yield 'yard', (m.group(1), '', rest.strip())
            continue
        m = IC_RE.match(seg)
        if m:
            yield 'ic', (m.group(1).strip(), m.group(2), m.group(3), m.group(4))
            continue
        # Free text. Trailing whitespace goes, leading whitespace stays: the
        # blocking lists ("         1: Kamloops") rely on their indentation.
        yield 'text', seg.rstrip()


def scrape_location_names(railroads):
    """Location id -> most frequently used name, pooled across every file.
    Pooling matters: Shortline alone names 154 stops, but inherits 398 from the
    shared map once the other rosters are read alongside it."""
    votes = defaultdict(Counter)
    for rr in railroads:
        for t in rr['trains']:
            for kind, p in split_note(t['raw']):
                if kind == 'yard' and p[1]:
                    votes[p[0]][p[1]] += 1
    return {lid: c.most_common(1)[0][0] for lid, c in votes.items()}


# --- location name store -----------------------------------------------------
# Names are only ever discovered incidentally, from the "@@1450 Memphis Tennessee
# Yard - ..." markers in whatever roster happens to mention a stop. That makes
# them fragile across updates: if next month's files stop mentioning a yard, its
# name would vanish and the page would show "#1450" again. So names are kept in
# locations.csv and merged forward, never dropped.
#
# Sources, in order of authority:
#   manual  - hand-entered, never overwritten by anything
#   map     - transcribed from the game's own map-ID screen (authoritative,
#             only replaced by a manual row or a fresh map import)
#   scraped - majority vote from the @@ note markers; weakest, refreshed
#             whenever the current rosters still name the stop

LOC_SOURCES = ('scraped', 'map', 'manual')
LOC_HEADER = ['id', 'name', 'source']


def load_location_store(path):
    """id -> (name, source). Tolerates a bare two-column id,name CSV."""
    store = {}
    if not path or not os.path.isfile(path):
        return store
    with open(path, encoding='utf-8', errors='replace', newline='') as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            lid, nm = row[0].strip(), row[1].strip()
            src = row[2].strip().lower() if len(row) > 2 else 'manual'
            if not lid.isdigit() or not nm or lid.lower() == 'id':
                continue
            store[lid] = (nm, src if src in LOC_SOURCES else 'manual')
    return store


def merge_location_store(store, scraped):
    """Fold this build's scrape into the store. Manual entries are untouchable;
    scraped entries refresh when the current files still name them, and survive
    when they don't. Returns (store, newly named, refreshed)."""
    added = changed = 0
    for lid, nm in scraped.items():
        cur = store.get(lid)
        if cur is None:
            store[lid] = (nm, 'scraped')
            added += 1
        elif cur[1] == 'scraped' and cur[0] != nm:
            store[lid] = (nm, 'scraped')
            changed += 1
    return store, added, changed


def save_location_store(path, store):
    rows = sorted(store.items(), key=lambda kv: int(kv[0]))
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(LOC_HEADER)
        for lid, (nm, src) in rows:
            w.writerow([lid, nm, src])


# ---------------------------------------------------------------------------
# interchange agreements (interchange_data/)
#
# Shortline_interchange.txt is a hand-maintained table of each shortline's
# reporting mark, connecting partners and preferred partner(s).
# derived_exchange_points.csv maps (shortline, partner) pairs to the map ids
# where both do work — machine-derived, then hand-curated, like locations.csv.
# The file legitimately lists railroads the game does not model, so rows
# naming operators absent from the rosters are skipped and counted, not
# flagged; a yard id that exists nowhere in the data IS an anomaly (ids are
# ground truth), as is a structurally unreadable CSV row.
# ---------------------------------------------------------------------------

IX_FOLDER = 'interchange_data'
IX_TABLE = 'Shortline_interchange.txt'
IX_POINTS = 'derived_exchange_points.csv'
IX_ALIASES = {'CP': 'CPKC'}          # file spellings -> roster spellings
IX_MARK_RE = re.compile(r'[A-Z0-9]{2,6}')


def load_interchange_data(folder, railroads, loc_ids, anom):
    """Returns (pairs, points) for the payload:
    pairs  {mark: {'p': [partner marks], 'f': [preferred marks]}}
    points [[shortline, partner, yard_id], ...]
    Both restricted to operators that exist in the parsed rosters."""
    ops = set()
    for rr in railroads:
        if rr['meta']['multi']:
            if not rr['meta']['pax']:
                ops.update(p for p in rr['prefixes'].values() if p)
        else:
            ops.add(rr['code'])

    pairs, unknown_marks = {}, set()
    table = os.path.join(folder, IX_TABLE)
    if os.path.isfile(table):
        with open(table, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                cells = [c.strip() for c in line.strip().strip('|').split('|')]
                if len(cells) < 3 or cells[0] in ('Shortline', '') \
                        or set(cells[0]) <= set('- '):
                    continue
                mark = cells[0].split()[0]        # "RJCK (TN MS)" -> RJCK
                if mark not in ops:
                    unknown_marks.add(mark)
                    continue
                conn = [IX_ALIASES.get(p, p) for p in IX_MARK_RE.findall(cells[2])]
                pref = [IX_ALIASES.get(p, p)
                        for p in (cells[3].split() if len(cells) > 3 else [])]
                cur = pairs.setdefault(mark, {'p': [], 'f': []})
                for p in conn:
                    if p != mark and p not in cur['p']:
                        cur['p'].append(p)
                for p in pref:
                    if p != mark and p not in cur['f']:
                        cur['f'].append(p)

    points, skipped_rows = [], 0
    csv_path = os.path.join(folder, IX_POINTS)
    if os.path.isfile(csv_path):
        with open(csv_path, encoding='utf-8', errors='replace', newline='') as fh:
            for row in csv.reader(fh):
                if not row or row[0].strip() in ('shortline', ''):
                    continue
                if len(row) < 3:
                    anom.add("interchange CSV row with fewer than 3 columns",
                             repr(row)[:70])
                    continue
                sl = IX_ALIASES.get(row[0].strip(), row[0].strip())
                pt = IX_ALIASES.get(row[1].strip(), row[1].strip())
                yid = row[2].strip()
                if sl not in ops or pt not in ops:
                    skipped_rows += 1
                    continue
                if yid not in loc_ids:
                    anom.add("interchange CSV yard id not present in any roster",
                             f"{yid} ({sl}-{pt})")
                    continue
                if [sl, pt, yid] not in points:
                    points.append([sl, pt, yid])

    if pairs or points:
        print(f"  interchange data: {len(pairs)} shortline(s) matched, "
              f"{len(points)} exchange point rows kept "
              f"({len(unknown_marks)} mark(s) and {skipped_rows} row(s) name "
              f"operators the game does not model)")
    return pairs, points


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build_payload(railroads, names):
    """Flatten every railroad into the JSON the page runs on, resolving the
    *IC* interchange pointers to real trains along the way."""
    trains, uid_of, sym_index = [], {}, defaultdict(dict)
    by_game_idx = {}

    for rr in railroads:
        if rr['meta']['idx'] is not None:
            by_game_idx[rr['meta']['idx']] = rr['code']
        for t in rr['trains']:
            uid = len(trains)
            uid_of[(rr['code'], t['tid'], t['idx'])] = uid
            sym = full_symbol(rr, t)
            sym_index[rr['code']].setdefault(norm_sym(sym), uid)
            trains.append({'rr': rr['code'], 'ty': t['ty'], 's': t['s'], 'sym': sym,
                           'nm': t['nm'], 'o': t['o'], 'd': t['d'], 'r': t['r'],
                           'f': t['f'], 'x': t['x'], '_raw': t['raw']})

    def resolve_ic(body, fi, tid, idx):
        """The pointer's railroad index is reliable; its type/train index has gone
        stale in about a third of the markers, so fall back to matching the symbol
        the marker itself spells out. Returns a train uid or None."""
        code = by_game_idx.get(int(fi))
        if code is None:
            return None
        want = norm_sym(body)
        uid = uid_of.get((code, tid, idx))
        if uid is not None and norm_sym(trains[uid]['sym']) == want:
            return uid
        table = sym_index[code]
        # The body reads "<MARK> <symbol>". On multi-operator rosters the mark is
        # part of the symbol; elsewhere it is the railroad's own name. Try both.
        hit = table.get(want)
        if hit is None and ' ' in body:
            hit = table.get(norm_sym(body.split(' ', 1)[1]))
        if hit is not None:
            return hit
        return uid  # last resort: trust the pointer even though the symbol drifted

    ic_total = ic_linked = 0
    for t in trains:
        segs = []
        for kind, p in split_note(t.pop('_raw')):
            if kind == 'yard':
                lid, nm, txt = p
                # Drop the name when it is the one already in the location table;
                # it is repeated on thousands of segments otherwise.
                segs.append([SEG_YARD, lid, txt] if nm == names.get(lid, '')
                            else [SEG_YARD, lid, txt, nm])
            elif kind == 'ic':
                body, fi, tid, idx = p
                ic_total += 1
                uid = resolve_ic(body, fi, tid, idx)
                if uid is None:
                    segs.append([SEG_IC, body])
                else:
                    ic_linked += 1
                    segs.append([SEG_IC, body, uid])
            elif kind == 'text':
                segs.append([SEG_TEXT, p])
            else:
                segs.append([SEG_BREAK])
        while segs and segs[-1][0] == SEG_BREAK:
            segs.pop()
        t['g'] = segs

    loc_ids = set()
    for t in trains:
        loc_ids.update(t['r'])
        loc_ids.add(t['o'])
        loc_ids.add(t['d'])
        # a train can be told to work a yard that is not one of its stops
        loc_ids.update(g[1] for g in t['g'] if g[0] == SEG_YARD)
    loc_ids.discard('')
    locations = [{'id': i, 'nm': names.get(i, '')}
                 for i in sorted(loc_ids, key=lambda x: int(x) if x.isdigit() else 0)]

    roads = [{
        'c':  rr['code'],
        'n':  rr['meta']['name'],
        'k':  rr['meta']['color'],
        'm':  1 if rr['meta']['multi'] else 0,
        'pax': 1 if rr['meta']['pax'] else 0,
        'ty': sorted(rr['types']),
        'px': {t: p for t, p in rr['prefixes'].items() if p},
        # capability set per type, '+'-joined ("carload+intermodal")
        'fc': {} if rr['meta']['multi'] else
              {ty: cls if isinstance(cls, str) else '+'.join(cls)
               for ty, cls in FREIGHT_CLASS.get(rr['code'], {}).items()},
    } for rr in railroads]

    payload = {'gen': date.today().isoformat(), 'rrs': roads,
               'locs': locations, 'trains': trains}
    return payload, ic_total, ic_linked


def dump_payload(payload):
    """Serialise with one train (and one location) per line.

    Semantically identical to a single-line dump, but it means a TSAR update
    shows up in git as the few hundred lines that actually changed rather than
    as one 6 MB line, so `git diff --stat` tells you what moved and successive
    builds delta-compress instead of storing a whole new copy each time."""
    j = lambda o: json.dumps(o, separators=(',', ':'), ensure_ascii=False)
    out = ['{"gen":' + j(payload['gen']) + ',',
           '"rrs":[', ',\n'.join(j(r) for r in payload['rrs']), '],',
           '"locs":[', ',\n'.join(j(l) for l in payload['locs']), '],',
           '"ixp":' + j(payload.get('ixp', {})) + ',',
           '"ix":[', ',\n'.join(j(r) for r in payload.get('ix', [])), '],',
           '"mims":[', ',\n'.join(j(f) for f in payload.get('mims', [])), '],',
           '"geo":[', ',\n'.join(j(g) for g in payload.get('geo', [])), '],',
           '"trains":[', ',\n'.join(j(t) for t in payload['trains']), ']}']
    # '/' only ever occurs inside a JSON string, so this cannot corrupt the
    # structure — it just stops a note containing "</script>" from ending the
    # inline script block early.
    return '\n'.join(out).replace('</', '<\\/')


def collect_report(payload, railroads, ic_total, ic_linked, store):
    named = sum(1 for L in payload['locs'] if L['nm'])
    return {
        'built':  datetime.now().replace(microsecond=0).isoformat(),
        'trains': len(payload['trains']),
        'locations': {'total': len(payload['locs']), 'named': named,
                      'manual': sum(1 for v in store.values() if v[1] == 'manual'),
                      'map':    sum(1 for v in store.values() if v[1] == 'map')},
        'interchange': {'markers': ic_total, 'linked': ic_linked},
        'railroads': {
            rr['code']: {
                'trains': len(rr['trains']),
                'types':  len(rr['types']),
                'file':   os.path.basename(rr['file']),
                'bytes':  os.path.getsize(rr['file']),
            } for rr in railroads},
    }


def diff_report(old, new):
    """Print what changed since the previous build. This is the line that catches
    a half-downloaded .ini: the roster count falls off a cliff and says so."""
    if not old:
        print("\n  first build — no previous report to compare against")
        return
    print(f"\n  changes since the last build ({old.get('built', '?')}):")
    oldrr, newrr = old.get('railroads', {}), new['railroads']
    rows = []
    for code in sorted(set(oldrr) | set(newrr)):
        was = oldrr.get(code, {}).get('trains')
        now = newrr.get(code, {}).get('trains')
        if was == now:
            continue
        if was is None:
            rows.append(f"    {code:10s} new roster, {now:,} trains")
        elif now is None:
            rows.append(f"    {code:10s} roster removed (was {was:,} trains)")
        else:
            d = now - was
            warn = "   <-- check the source file" if was and now / was < 0.5 else ""
            rows.append(f"    {code:10s} {was:6,} -> {now:6,}  ({d:+,}){warn}")
    print('\n'.join(rows) if rows else "    no change to any roster's train count")

    ow, nw = old.get('locations', {}).get('named'), new['locations']['named']
    if ow is not None and ow != nw:
        print(f"    {'locations':10s} {ow:6,} -> {nw:6,} named  ({nw - ow:+,})")


# ---------------------------------------------------------------------------
# MIM families (mims.csv)
#
# One interface map hosts several controllable yard identities plus its
# virtual off-map destinations. mims.csv is derived from the game's .yrd map
# files by mim_import.py (the .yrd inputs are game data and stay uncommitted;
# the derived table is committed, same split as TSARs/ vs locations.csv).
# ---------------------------------------------------------------------------

MIMS_PATH = 'mims.csv'
MIM_KINDS = ('yard', 'vid', 'ref')


def load_mims(path, names, anom):
    """mims.csv -> payload families [[mother, [member, is_vid], ...], ...].
    'ref' rows are cross-references to a neighbouring map, not membership,
    so they are skipped. Ids are validated against the location name store
    (not this build's subset) so --only builds do not false-alarm."""
    fams = {}
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8', errors='replace', newline='') as fh:
        for row in csv.reader(fh):
            if not row or row[0] == 'mother_id':
                continue
            if len(row) < 3 or row[2] not in MIM_KINDS:
                anom.add("mims.csv row is not mother,member,kind[,...]",
                         repr(row)[:70])
                continue
            mo, m, kind = row[0].strip(), row[1].strip(), row[2]
            for i in (mo, m):
                if i not in names:
                    anom.add("mims.csv id is not a known location",
                             f"{i} in {row[:3]}")
            if kind != 'ref':
                fams.setdefault(mo, []).append([m, 1 if kind == 'vid' else 0])
    return [[mo] + ms for mo, ms in
            sorted(fams.items(), key=lambda kv: int(kv[0]))]


# ---------------------------------------------------------------------------
# location geography (geo.csv)
#
# Derived from the game's .his map files by his_import.py: per-identity
# latitude/longitude and the railroads present, where the map author recorded
# them (~64% of maps; older .his files predate the fields). Structural facts
# only — descriptions and author/yardmaster names are deliberately not
# extracted (republication is on hold).
# ---------------------------------------------------------------------------

GEO_PATH = 'geo.csv'


def load_geo(path, names, anom):
    """geo.csv -> payload rows [id, lat, lon, rr]."""
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding='utf-8', errors='replace', newline='') as fh:
        for row in csv.reader(fh):
            if not row or row[0] == 'id':
                continue
            if len(row) < 3:
                anom.add("geo.csv row is not id,lat,lon[,...]", repr(row)[:70])
                continue
            lid, lat, lon = row[0].strip(), row[1], row[2]
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                anom.add("geo.csv coordinate is not a number", repr(row)[:70])
                continue
            if lid not in names:
                anom.add("geo.csv id is not a known location", lid)
            if not (14 < lat < 72 and -170 < lon < -50):
                anom.add("geo.csv coordinate outside North America", repr(row)[:70])
                continue
            rr = row[3].strip() if len(row) > 3 else ''
            rows.append([lid, lat, lon, rr])
    return rows


def build(files, out, title, loc_path, use_cache=True):
    anom = Anomalies()
    railroads = []
    for f in files:
        rr = parse_tsar(f, anom)
        railroads.append(rr)
        print(f"  {rr['code']:10s} {len(rr['trains']):5d} trains  "
              f"{len(rr['types']):3d} {'operators' if rr['meta']['multi'] else 'types':9s} "
              f"({os.path.basename(f)})")

    # Every declared type on a single-railroad roster must have a freight
    # classification, the table must not go stale, and every class named in
    # it must be a real class — all three directions loud.
    for rr in railroads:
        if rr['meta']['multi']:
            continue
        table = FREIGHT_CLASS.get(rr['code'], {})
        for ty in rr['types']:
            if ty not in table:
                anom.add("train type has no freight classification "
                         "(add to FREIGHT_CLASS)", f"{ty!r} in {rr['code']}")
        for ty, cls in table.items():
            if ty not in rr['types']:
                anom.add("FREIGHT_CLASS lists a type the roster no longer "
                         "declares", f"{ty!r} in {rr['code']}")
            for c in ((cls,) if isinstance(cls, str) else cls):
                if c not in FREIGHT_CLASSES:
                    anom.add("FREIGHT_CLASS names an unknown class",
                             f"{c!r} for {ty!r} in {rr['code']}")

    scraped = scrape_location_names(railroads)
    store = load_location_store(loc_path) if use_cache else {}
    store, added, changed = merge_location_store(store, scraped)
    names = {lid: nm for lid, (nm, _) in store.items()}

    payload, ic_total, ic_linked = build_payload(railroads, names)

    loc_ids = {l['id'] for l in payload['locs']}
    pairs, points = load_interchange_data(IX_FOLDER, railroads, loc_ids, anom)
    payload['ixp'] = pairs
    payload['ix'] = points

    payload['mims'] = load_mims(MIMS_PATH, names, anom)
    if payload['mims']:
        nmem = sum(len(f) - 1 for f in payload['mims'])
        print(f"  MIM families: {len(payload['mims'])} maps with "
              f"{nmem} co-located yards / virtual destinations")

    payload['geo'] = load_geo(GEO_PATH, names, anom)
    if payload['geo']:
        print(f"  geography: {len(payload['geo'])} located identities")

    outdir = os.path.dirname(os.path.abspath(out))
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(HTML_TEMPLATE.replace('__TITLE__', title)
                              .replace('__DATA__', dump_payload(payload)))

    report_path = os.path.join(os.path.dirname(out) or '.', REPORT_NAME)
    old = None
    if os.path.isfile(report_path):
        try:
            with open(report_path, encoding='utf-8') as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            pass  # unreadable previous report is not worth failing a build over
    new = collect_report(payload, railroads, ic_total, ic_linked, store)

    if use_cache:
        save_location_store(loc_path, store)

    named = new['locations']['named']
    print(f"\n  {new['trains']:,} trains across {len(railroads)} railroad(s): "
          f"{', '.join(r['code'] for r in railroads)}")
    print(f"  {new['locations']['total']:,} locations, {named:,} named "
          f"({len(scraped):,} from this build's notes, "
          f"{new['locations']['map']:,} from the map-ID screen, "
          f"{new['locations']['manual']:,} hand-named)")
    if use_cache and (added or changed):
        print(f"  {os.path.basename(loc_path)}: {added:,} new name(s), "
              f"{changed:,} updated")
    rate = ic_linked / ic_total if ic_total else 1.0
    print(f"  {ic_linked:,}/{ic_total:,} interchange markers linked "
          f"({rate:.0%}) to a train")
    if ic_total and rate < IC_LINK_FLOOR:
        anom.add(f"only {rate:.0%} of interchange markers resolve — the game may "
                 f"have renumbered its rosters (RR_REGISTRY 'idx' values)")

    diff_report(old, new)
    with open(report_path, 'w', encoding='utf-8') as fh:
        json.dump(new, fh, indent=1, sort_keys=True)
        fh.write('\n')

    anom.report()
    print(f"\n  wrote {out}  ({os.path.getsize(out)/1024/1024:.1f} MB)")
    print(f"  wrote {report_path}")
    return len(anom)


# ---------------------------------------------------------------------------
# The self-contained page. __DATA__ is replaced with the JSON payload and
# __TITLE__ with the page title. All CSS + JS are inline so the file is
# portable and works from file:// with no server.
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0e1116; --panel:#161b22; --panel2:#1c232d; --line:#2a333f;
  --ink:#e8edf3; --muted:#8b97a6; --dim:#5f6b7a;
  --accent:#e8b923; --active:#3fb950; --expired:#8b5cf6; --future:#58a6ff;
  --chip:#222b36;
  --mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;line-height:1.45}
button,input,select{font-family:inherit;font-size:inherit;color:inherit}

/* ---- masthead ---- */
.mast{position:sticky;top:0;z-index:30;background:linear-gradient(180deg,#11161d,#0e1116);
  border-bottom:1px solid var(--line)}
.mast .row{display:flex;align-items:center;gap:14px;padding:12px 20px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:10px;margin-right:auto}
.brand b{font-size:19px;letter-spacing:.5px}
.brand .tk{font-family:var(--mono);color:var(--accent);font-size:12px;
  border:1px solid var(--line);padding:1px 7px;border-radius:4px}
.brand span.sub{color:var(--muted);font-size:12px}
.count{font-family:var(--mono);color:var(--muted);font-size:12px;white-space:nowrap}
.count b{color:var(--ink)}

/* ---- controls ---- */
.controls{display:flex;gap:16px;padding:0 20px 14px;flex-wrap:wrap;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim)}
.inp{background:var(--panel);border:1px solid var(--line);border-radius:7px;
  padding:8px 11px;min-width:190px;outline:none}
.inp:focus{border-color:var(--accent)}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{font-family:var(--mono);font-size:12px;padding:5px 11px;border-radius:20px;cursor:pointer;
  background:var(--chip);border:1px solid var(--line);color:var(--muted);user-select:none;
  transition:.12s}
.pill:hover{color:var(--ink)}
.pill.on{color:#0e1116;font-weight:700}
.seg{display:flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.seg button{background:var(--panel);border:0;padding:8px 12px;cursor:pointer;color:var(--muted)}
.seg button.on{background:var(--panel2);color:var(--ink)}
.seg button+button{border-left:1px solid var(--line)}

/* location combobox */
.combo{position:relative}
.combo .menu{position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:40;max-height:320px;
  overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 12px 40px rgba(0,0,0,.5);display:none}
.combo .menu.open{display:block}
.combo .opt{padding:8px 11px;cursor:pointer;display:flex;gap:10px;align-items:baseline}
.combo .opt:hover,.combo .opt.hi{background:var(--panel2)}
.combo .opt .lid{font-family:var(--mono);color:var(--accent);min-width:46px}
.combo .opt .lnm{color:var(--ink)}
.combo .opt .lnm.unk{color:var(--dim);font-style:italic}
.combo .opt .cnt{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--dim)}
.clearbtn{background:none;border:0;color:var(--dim);cursor:pointer;font-size:12px;padding:2px 4px}
.clearbtn:hover{color:var(--ink)}

/* ---- location banner ---- */
.locbar{margin:0 20px 6px;padding:10px 14px;border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;background:var(--panel);display:none;align-items:center;gap:12px;flex-wrap:wrap}
.locbar.show{display:flex}
.locbar .seg button{padding:5px 10px;font-size:12px}
.locbar .sibs{flex-basis:100%;display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;margin-top:2px}
.locbar .siblbl{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim)}
.sib{background:var(--chip);border:1px solid var(--line);border-radius:14px;padding:3px 10px;
  font-size:12px;color:var(--ink);cursor:pointer}
.sib:hover{border-color:var(--accent)}
.sib.vid{color:var(--muted);font-style:italic}
.sib.vid::after{content:"ᵛ";margin-left:3px;color:var(--dim);font-style:normal}
.sib.cur{border-color:var(--accent);cursor:default}

/* ---- location details ---- */
.dpanel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  max-width:1180px;margin:9px auto;padding:14px 18px}
.dpanel h4{margin:14px 0 7px;font-size:11px;letter-spacing:.7px;text-transform:uppercase;color:var(--dim)}
.dpanel h4:first-child{margin-top:0}
.dpanel .drow{padding:3px 0;font-size:13px}
.dpanel .dim{color:var(--muted)}
.dpanel .ext{color:var(--future)}

/* ---- car routing ---- */
.routebar{margin:0 20px 6px;padding:10px 14px;border:1px solid var(--line);border-left:3px solid var(--future);
  border-radius:8px;background:var(--panel);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.routebar .rtitle{font-weight:600}
.routebar .beta{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--future);
  border:1px solid var(--future);border-radius:4px;padding:1px 5px;margin-left:8px}
.routebar .rchk{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}
.gobtn{background:var(--accent);color:#0e1116;border:0;border-radius:7px;padding:8px 12px;
  cursor:pointer;font-weight:600}
.rhead{max-width:1180px;margin:10px auto;color:var(--muted);font-size:13px;line-height:1.5}
.rhead b{color:var(--ink)}
.warn{color:#f0b429}
.corr{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:10px auto;
  max-width:1180px;padding:10px 14px}
.chead{font-size:12px;color:var(--muted);margin-bottom:6px}
.chead b{color:var(--ink)}
.leg{display:flex;gap:10px;align-items:baseline;padding:6px 4px;cursor:pointer;border-radius:6px}
.leg:hover{background:var(--panel2)}
.leg .chev{color:var(--dim);font-size:10px}
.leg .lty{font-size:11px;color:var(--dim)}
.leg .lod{font-size:12px;color:var(--muted)}
.legnote{margin:0 0 4px 46px;font-size:12px;color:var(--muted)}
.legnote .txt{color:var(--ink)}
.legnote.ok{color:var(--accent)}
.legnote.bad{color:#f0b429}

/* ---- yard sheet ---- */
.srow{display:grid;grid-template-columns:44px 88px 170px minmax(200px,1fr) 2fr;gap:12px;
  align-items:baseline;padding:8px 14px;border-bottom:1px solid var(--line);cursor:pointer;
  max-width:1180px;margin:0 auto}
.srow:hover{background:var(--panel)}
.srow.shead{cursor:default;font-size:10px;text-transform:uppercase;letter-spacing:.7px;
  color:var(--dim);padding-top:2px}
.srow.shead:hover{background:none}
.srow .role{font-family:var(--mono);font-size:12px;color:var(--accent)}
.srow .ssym{font-family:var(--mono);font-size:13px;font-weight:700;letter-spacing:.5px}
.srow .sod{font-size:12px;color:var(--muted)}
.srow .stxt{font-size:13px}
.srow .stxt.none{color:var(--dim)}
.locbar .lid{font-family:var(--mono);color:var(--accent)}
.locbar .big{font-size:15px;font-weight:600}

/* ---- results ---- */
.wrap{padding:8px 20px 60px;max-width:1180px;margin:0 auto}
.empty{color:var(--muted);text-align:center;padding:60px 20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:9px 0;overflow:hidden}
.card>.head{display:flex;align-items:center;gap:12px;padding:12px 14px;cursor:pointer}
.card>.head:hover{background:var(--panel2)}
.card.flash{border-color:var(--accent);box-shadow:0 0 0 2px rgba(232,185,35,.25)}
.rr{font-family:var(--mono);font-size:11px;font-weight:700;padding:3px 8px;border-radius:5px;color:#0e1116;white-space:nowrap}
.sym{font-family:var(--mono);font-size:15px;font-weight:700;letter-spacing:.5px;white-space:nowrap}
.tnm{font-size:13px;color:var(--muted);font-style:italic;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:190px}
.ty{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:20px;padding:2px 9px;white-space:nowrap}
.od{color:var(--muted);font-size:13px;margin-left:2px}
.od b{color:var(--ink);font-weight:500}
.od .arr{color:var(--dim);margin:0 6px}
.spacer{margin-left:auto}
.status{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:5px;white-space:nowrap}
.status.active{color:var(--active);border:1px solid rgba(63,185,80,.4)}
.status.expired{color:var(--expired);border:1px solid rgba(139,92,246,.4)}
.status.future{color:var(--future);border:1px solid rgba(88,166,255,.4)}
.chev{color:var(--dim);transition:transform .15s;font-family:var(--mono)}
.card.open .chev{transform:rotate(90deg)}
.here{font-size:12px;color:var(--accent);font-family:var(--mono);white-space:nowrap;
  border:1px dashed rgba(232,185,35,.5);padding:2px 8px;border-radius:5px}

.body{display:none;border-top:1px solid var(--line);padding:14px;background:var(--panel2)}
.card.open .body{display:block}
.body h4{margin:0 0 7px;font-size:11px;letter-spacing:.7px;text-transform:uppercase;color:var(--dim)}
.dates{font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:14px}
.route{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:16px}
.node{font-family:var(--mono);font-size:12px;padding:3px 8px;border-radius:5px;background:var(--chip);
  border:1px solid var(--line);color:var(--muted);cursor:pointer}
.node:hover{border-color:var(--accent)}
.node.hit{border-color:var(--accent);color:var(--accent)}
.node.end{color:var(--ink)}
.node .id{color:var(--dim)}
.instr{display:flex;flex-direction:column;gap:8px}
.line{display:flex;gap:10px;align-items:flex-start}
.line .tag{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:4px;margin-top:1px;white-space:nowrap}
.tag.yard{background:rgba(232,185,35,.14);color:var(--accent);border:1px solid rgba(232,185,35,.3)}
.tag.ic{background:rgba(88,166,255,.14);color:var(--future);border:1px solid rgba(88,166,255,.3)}
.line .yn{font-family:var(--mono);color:var(--ink)}
.line .yn .id{color:var(--dim)}
.line .txt{color:var(--ink);white-space:pre-wrap}
.line.plain .txt{color:var(--muted)}
.brk{height:4px}
.jump{color:var(--future);background:none;border:0;padding:0;font:inherit;cursor:pointer;
  text-decoration:underline dotted;text-underline-offset:2px}
.jump:hover{color:var(--ink)}
.odlink{background:none;border:0;padding:0;font:inherit;font-weight:600;color:inherit;cursor:pointer;
  text-decoration:underline dotted;text-underline-offset:2px}
.odlink:hover{color:var(--accent)}
.morebtn{display:block;margin:22px auto;background:var(--panel);border:1px solid var(--line);
  color:var(--ink);padding:10px 22px;border-radius:8px;cursor:pointer}
.morebtn:hover{border-color:var(--accent)}
/* ---- small screens ---- */
.fbtn{display:none;background:var(--chip);border:1px solid var(--line);border-radius:7px;
  color:var(--ink);padding:8px 12px;cursor:pointer;font-size:13px}
@media(max-width:700px){
  .od{display:none}.brand span.sub{display:none}.tnm{display:none}.here{display:none}
  .mast .row{padding:10px 12px;gap:10px}
  /* filters collapse behind a toggle so results are above the fold */
  .fbtn{display:inline-block}
  .mast.open .fbtn{border-color:var(--accent);color:var(--accent)}
  .controls{display:none}
  .mast.open .controls{display:flex;gap:10px;padding:0 12px 12px}
  .controls .field{width:100%}
  .controls .inp,.controls select.inp{width:100%;box-sizing:border-box;min-width:0}
  .combo .menu{max-height:45vh}
  /* comfortable touch targets */
  .seg button{padding:10px 12px}
  .pill{padding:8px 13px;font-size:13px}
  .node{padding:8px 10px;font-size:13px}
  .sib{padding:7px 12px;font-size:13px}
  .locbar{margin:0 8px 6px;padding:8px 10px;gap:8px}
  .locbar .seg button{padding:8px 10px}
  .locnote{display:none}
  .wrap{padding:8px 8px 60px}
  .card>.head{flex-wrap:wrap;row-gap:4px;padding:10px 10px}
  .ty{max-width:44vw;overflow:hidden;text-overflow:ellipsis}
  .body{padding:12px 10px}
  /* yard sheet: each row stacks into train line / route line / work line */
  .srow{display:flex;flex-wrap:wrap;gap:6px 10px;padding:10px 8px}
  .srow.shead{display:none}
  .srow .sod{flex-basis:100%}
  .srow .stxt{flex-basis:100%}
  .dpanel{margin:9px 0;padding:12px}
}
</style>
</head>
<body>
<header class="mast">
  <div class="row">
    <div class="brand">
      <span class="tk">TSAR</span><b>__TITLE__</b>
      <span class="sub">find a train by location, type &amp; railroad</span>
    </div>
    <button class="fbtn" id="fbtn">Filters</button>
    <div class="count" id="count"></div>
  </div>
  <div class="controls">
    <div class="field combo" id="loccombo">
      <label>Location</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input class="inp" id="locinput" placeholder="yard name or ID…" autocomplete="off">
        <button class="clearbtn" id="locclear" title="clear" style="display:none">clear ✕</button>
      </div>
      <div class="menu" id="locmenu"></div>
    </div>
    <div class="field" id="rrfield">
      <label>Railroad</label>
      <div class="pills" id="rrpills"></div>
    </div>
    <div class="field">
      <label id="typelabel">Type</label>
      <select class="inp" id="typesel"></select>
    </div>
    <div class="field">
      <label>Symbol</label>
      <input class="inp" id="syminput" placeholder="symbol or train name" autocomplete="off" style="min-width:170px">
    </div>
    <div class="field">
      <label>Status</label>
      <div class="seg" id="statseg">
        <button data-s="active" class="on">Active</button>
        <button data-s="all">All</button>
        <button data-s="expired">Expired</button>
      </div>
    </div>
  </div>
</header>

<div class="routebar" id="routebar">
  <span class="rtitle">Route a car<span class="beta">beta</span></span>
  <div class="combo">
    <input class="inp" id="rfin" placeholder="from yard…" autocomplete="off">
    <div class="menu" id="rfmenu"></div>
  </div>
  <span style="color:var(--dim)">→</span>
  <div class="combo">
    <input class="inp" id="rtin" placeholder="to yard…" autocomplete="off">
    <div class="menu" id="rtmenu"></div>
  </div>
  <label class="rchk">car type
    <select class="inp" id="rcartype" style="min-width:0;padding:6px 8px">
      <option value="carload">Manifest car</option>
      <option value="intermodal">Container / trailer</option>
      <option value="auto">Autorack</option>
      <option value="any">Any freight</option>
    </select>
  </label>
  <button class="gobtn" id="rgo" style="display:none">find routes</button>
  <button class="clearbtn" id="rclear" style="display:none">clear ✕</button>
</div>

<div class="locbar" id="locbar"></div>
<main class="wrap" id="wrap"></main>

<script>
const DATA = __DATA__;
const SEG_YARD=0, SEG_IC=1, SEG_TEXT=2, SEG_BREAK=3;

// Compare against the real current date, not the build date, so the page keeps
// telling the truth about what is active after the .ini files are refreshed.
const TODAY = new Date().toISOString().slice(0,10);

// railroad lookup: code -> {c,n,k,m,ty,px}
const RR = {};
DATA.rrs.forEach(r => RR[r.c] = r);
const rrColor = c => (RR[c] ? RR[c].k : "#2dd4a7");

// location name lookup
const LOC = {};
DATA.locs.forEach(l => { if(l.nm) LOC[l.id] = l.nm; });
const locLabel = id => LOC[id] ? LOC[id] : "#"+id;

// Full symbol = <prefix><symbol>, or "<mark> <symbol>" on the multi-operator
// rosters where the prefix is the operator's reporting mark. The prefix comes
// from each railroad's own [TypeInfo] block, so nothing here assumes any one
// road's symbol format, length or number placeholder ("-##", "%-##", "-XX", …).
function fullSym(t){
  const rr = RR[t.rr]; if(!rr) return t.s;
  const p = rr.px[t.ty] || "";
  return p ? (rr.m ? p+" "+t.s : p+t.s) : t.s;
}
// t.w = yards this train "works": every @@ yard tagged in its instructions.
// Not a subset of the route — some trains are told to work a yard they do not
// list as a stop, so it counts toward "touches this location" in its own right.
// t.ww narrows that to yards with real CAR work for the routing graph: a tag
// whose text only says the TRAIN is serviced there (fuel, crew) is where the
// graph would otherwise board cars onto unit trains at fuel stops.
// t.op = operator identity. Same as the roster code except on the two
// multi-operator rosters, where the real railroad is the type's reporting
// mark (IHB, AMTK, …). The UI keeps one SHORTLINE/PASSENGER pill; routing
// semantics (interchanges, home road, carload pool) speak operator.
const SERVICE_RE=/^\s*(train\s+)?(service|fuel|crew)( ?(point|stop|change|check))?s?\W*$/i;
DATA.trains.forEach((t,i) => {
  t.i = i; t.fs = fullSym(t);
  const rrm = RR[t.rr];
  t.op = (rrm && rrm.m) ? (rrm.px[t.ty] || t.ty) : t.rr;
  t.w = []; t.wn = {};
  t.g.forEach(g=>{
    if(g[0]===SEG_YARD){
      if(!t.w.includes(g[1])) t.w.push(g[1]);
      if(g[2]) (t.wn[g[1]]=t.wn[g[1]]||[]).push(g[2]);
    }
  });
  t.ww = t.w.filter(y=>{ const n=t.wn[y]; return !(n && n.every(x=>SERVICE_RE.test(x))); });
  t.icl = t.g.filter(g=>g[0]===SEG_IC && g[2]!==undefined).map(g=>g[2]);
});

// interchange agreements (interchange_data/): partner table + exchange points
const IXP = DATA.ixp || {};
const IXY = {};        // "A|B" -> Set of agreed exchange yard ids (both ways)
const IXOPYARD = new Set();   // "op|yard": op has an agreed exchange point here
(DATA.ix||[]).forEach(([a,b,y])=>{
  (IXY[a+"|"+b]=IXY[a+"|"+b]||new Set()).add(y);
  (IXY[b+"|"+a]=IXY[b+"|"+a]||new Set()).add(y);
  IXOPYARD.add(a+"|"+y); IXOPYARD.add(b+"|"+y);
});
const ixPartners =(a,b)=>(IXP[a]&&IXP[a].p.includes(b))||(IXP[b]&&IXP[b].p.includes(a));
const ixPreferred=(a,b)=>(IXP[a]&&IXP[a].f.includes(b))||(IXP[b]&&IXP[b].f.includes(a));
// Shortline-only marks. A mark that is also a Class I roster code is not
// shortline-only: the game lists "Iowa Northern (CN)" under CN's mark, and
// those trains interchange as CN itself.
const SLOPS=new Set();
{
  const classI=new Set(DATA.rrs.filter(r=>!r.m).map(r=>r.c));
  DATA.trains.forEach(t=>{ const r=RR[t.rr];
    if(r&&r.m&&!r.pax&&!classI.has(t.op)) SLOPS.add(t.op); });
}

// MIM families: one interface map hosts several yard identities plus its
// virtual off-map destinations. FAM maps every id in a family to the family.
const FAM = {};
(DATA.mims||[]).forEach(f=>{
  const fam = {mo: f[0], ms: f.slice(1)};   // ms entries: [id, isVid]
  FAM[fam.mo] = fam;
  fam.ms.forEach(m=>{ FAM[m[0]] = fam; });
});

// geography: id -> [lat, lon, railroads-text], where the map author recorded it
const GEO = {};
(DATA.geo||[]).forEach(g=>{ GEO[g[0]] = [g[1], g[2], g[3]]; });

// One interface map is one physical place, so every family member aliases to
// its mother map for routing. Vids included: vid traffic round-trips through
// the mother map with the same consist, so cars at vid zones sit in the same
// classification pool as everything else on the map (Zach's ruling).
const mapOf = id => FAM[id] ? FAM[id].mo : id;
const famIds = id => FAM[id] ? [FAM[id].mo, ...FAM[id].ms.map(m=>m[0])] : [id];
const geoOf = id => {
  if(GEO[id]) return GEO[id];
  const f=FAM[id];
  if(f){ if(GEO[f.mo]) return GEO[f.mo];
         for(const m of f.ms) if(GEO[m[0]]) return GEO[m[0]]; }
  return null;
};
const opIxAt =(op,id)=>famIds(id).some(fy=>IXOPYARD.has(op+"|"+fy));
const ixHasAt=(yset,id)=>famIds(id).some(fy=>yset.has(fy));


// how many trains touch each location (for the picker)
const locCount = {};
DATA.trains.forEach(t=>{
  const seen=new Set(t.r); seen.add(t.o); seen.add(t.d); t.w.forEach(id=>seen.add(id));
  seen.forEach(id=>{ if(id) locCount[id]=(locCount[id]||0)+1; });
});

function status(t){
  if(t.f && t.f>TODAY) return "future";
  if(t.x && t.x!=="2099-12-31" && t.x<TODAY) return "expired";
  return "active";
}

// ---- state ----
const state = {loc:"", locmode:"", view:"cards", rrs:new Set(DATA.rrs.map(r=>r.c)), type:"", sym:"", stat:"active", shown:0};
const PAGE = 300;

// ---- build controls ----
const rrpills = document.getElementById("rrpills");
const pillOf = {};
function paintPill(rr){
  const p = pillOf[rr], on = state.rrs.has(rr);
  p.classList.toggle("on", on);
  p.style.background = on ? rrColor(rr) : "var(--chip)";
  p.style.color      = on ? "#0e1116"   : "var(--muted)";
  p.style.borderColor= on ? rrColor(rr) : "var(--line)";
}
DATA.rrs.forEach(r=>{
  const p=document.createElement("div");
  p.className="pill"; p.textContent=r.c; p.title=r.n+" — "+r.ty.length+(r.m?" operators":" train types");
  pillOf[r.c]=p; paintPill(r.c);
  p.onclick=()=>{
    if(state.rrs.has(r.c)) state.rrs.delete(r.c); else state.rrs.add(r.c);
    paintPill(r.c); render();
  };
  rrpills.appendChild(p);
});
if(DATA.rrs.length<2) document.getElementById("rrfield").style.display="none";

// The type list is scoped to the trains matching every OTHER filter (pooling
// all nine rosters' declared types would put 380-odd entries, including 254
// shortline operators, into a single dropdown). A selected type that falls out
// of the set stays listed at (0) — filters never change themselves silently.
// On the multi-operator rosters the list is operators, so the label follows suit.
const typesel=document.getElementById("typesel");
const typelabel=document.getElementById("typelabel");
function syncTypes(base){
  const counts={};
  base.forEach(t=>{ counts[t.ty]=(counts[t.ty]||0)+1; });
  if(state.type && !(state.type in counts)) counts[state.type]=0;
  const all=Object.keys(counts).sort();
  const picked=DATA.rrs.filter(r=>state.rrs.has(r.c));
  const multiOnly = picked.length>0 && picked.every(r=>r.m);
  typelabel.textContent = multiOnly ? "Operator" : "Type";
  typesel.innerHTML='<option value="">'+(multiOnly?"All operators":"All types")+'</option>'+
    all.map(t=>`<option value="${esc(t)}"${t===state.type?" selected":""}>${esc(t)} (${counts[t]})</option>`).join("");
}
typesel.onchange=()=>{state.type=typesel.value;render();};

// On small screens the filter block collapses behind this toggle; picking a
// location closes it again so the results come straight back into view.
const NARROW=window.matchMedia("(max-width:700px)");
const mastEl=document.querySelector(".mast");
document.getElementById("fbtn").onclick=()=>mastEl.classList.toggle("open");
NARROW.addEventListener("change",()=>render());

const symin=document.getElementById("syminput");
symin.oninput=debounce(()=>{state.sym=symin.value.trim().toUpperCase();render();},120);

document.querySelectorAll("#statseg button").forEach(b=>{
  b.onclick=()=>{document.querySelectorAll("#statseg button").forEach(x=>x.classList.remove("on"));
    b.classList.add("on");state.stat=b.dataset.s;render();};
});
function setStat(s){
  state.stat=s;
  document.querySelectorAll("#statseg button").forEach(x=>x.classList.toggle("on",x.dataset.s===s));
}

// ---- location combobox ----
const locin=document.getElementById("locinput");
const locmenu=document.getElementById("locmenu");
const locclear=document.getElementById("locclear");
let hi=-1, opts=[];

const pickList = DATA.locs
  .map(l=>({id:l.id,nm:l.nm,c:locCount[l.id]||0}))
  .sort((a,b)=>b.c-a.c);

function openMenu(q){
  q=q.trim().toLowerCase();
  opts = pickList.filter(o=>!q || o.id.includes(q) || o.nm.toLowerCase().includes(q)).slice(0,60);
  locmenu.innerHTML = opts.map((o,i)=>
    `<div class="opt" data-i="${i}"><span class="lid">${o.id}</span>`+
    `<span class="lnm ${o.nm?'':'unk'}">${o.nm?esc(o.nm):'unnamed'}</span>`+
    `<span class="cnt">${o.c} trains</span></div>`).join("")
    || `<div class="opt"><span class="lnm unk">no match</span></div>`;
  locmenu.classList.add("open"); hi=-1;
  locmenu.querySelectorAll(".opt").forEach(el=>{
    el.onclick=()=>choose(opts[+el.dataset.i]);
  });
}
function choose(o){
  if(!o) return;
  rstate.res=null;      // picking a location always leaves the route view
  state.loc=o.id; locin.value = o.id+(o.nm?"  "+o.nm:"");
  locmenu.classList.remove("open"); locclear.style.display="";
  mastEl.classList.remove("open");
  render();
}
locin.onfocus=()=>openMenu(locin.value);
locin.oninput=()=>{state.loc="";state.locmode="";state.view="cards";locclear.style.display=locin.value?"":"none";openMenu(locin.value);};
locin.onkeydown=e=>{
  const els=locmenu.querySelectorAll(".opt");
  if(e.key==="ArrowDown"){hi=Math.min(hi+1,opts.length-1);e.preventDefault();}
  else if(e.key==="ArrowUp"){hi=Math.max(hi-1,0);e.preventDefault();}
  else if(e.key==="Enter"){if(opts[hi])choose(opts[hi]);return;}
  else return;
  els.forEach((el,i)=>el.classList.toggle("hi",i===hi));
  if(els[hi])els[hi].scrollIntoView({block:"nearest"});
};
locclear.onclick=()=>{state.loc="";state.locmode="";state.view="cards";locin.value="";locclear.style.display="none";render();};
document.addEventListener("click",e=>{
  if(!document.getElementById("loccombo").contains(e.target))locmenu.classList.remove("open");
});

const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// instruction that applies at a specific location (for the head-of-card hint)
function instrAt(t,locId){
  for(const g of t.g){
    if(g[0]===SEG_YARD && g[1]===locId)
      return g[2] || "(stops here — no special instruction)";
  }
  return "";
}

// every instruction line tagged at a location (a train can have several)
function allInstrAt(t,locId){
  const out=[];
  t.g.forEach(g=>{ if(g[0]===SEG_YARD && g[1]===locId && g[2]) out.push(g[2]); });
  return out;
}

// ---- filtering ----
// locmode narrows the location match: "o" originates there, "d" terminates
// there, "w" works it (@@ tag in the instructions), "" any of those or a
// route stop. Pass a mode to override state (used for the locbar counts).
function filteredBase(locmode){
  if(locmode===undefined) locmode=state.locmode;
  return DATA.trains.filter(t=>{
    if(!state.rrs.has(t.rr)) return false;
    if(state.sym){
      // match "NPSS", "M-NPSS", "MNPSS" and the train's name alike
      const hay=(t.fs+" "+t.fs.replace(/[^A-Za-z0-9]/g,"")+" "+t.s+" "+t.nm).toUpperCase();
      if(!hay.includes(state.sym) &&
         !hay.includes(state.sym.replace(/[^A-Z0-9]/g,""))) return false;
    }
    if(state.loc){
      const L=state.loc;
      if(locmode==="o"){ if(t.o!==L) return false; }
      else if(locmode==="d"){ if(t.d!==L) return false; }
      else if(locmode==="w"){ if(!t.w.includes(L)) return false; }
      else if(t.o!==L && t.d!==L && !t.r.includes(L) && !t.w.includes(L)) return false;
    }
    if(state.stat!=="all" && status(t)!==state.stat) return false;
    return true;
  });
}
function filtered(){
  const base=filteredBase();
  return state.type ? base.filter(t=>t.ty===state.type) : base;
}

// ---- render ----
const wrap=document.getElementById("wrap");
const countEl=document.getElementById("count");
const locbar=document.getElementById("locbar");

function render(reset=true){
  if(rstate.res){ renderRoutes(); return; }
  const base=filteredBase();
  if(reset) syncTypes(base);
  const list = state.type ? base.filter(t=>t.ty===state.type) : base;
  if(reset) state.shown=0;

  countEl.innerHTML=`<b>${list.length.toLocaleString()}</b> of ${DATA.trains.length.toLocaleString()} trains`;

  if(state.loc){
    const L=state.loc;
    // counts for each mode come from the trains matching every OTHER filter,
    // so an inactive mode's count is what you would get by clicking it
    const poolBase = state.locmode ? filteredBase("") : base;
    const pool = state.type ? poolBase.filter(t=>t.ty===state.type) : poolBase;
    const n={o:0,d:0,w:0};
    pool.forEach(t=>{ if(t.o===L)n.o++; if(t.d===L)n.d++; if(t.w.includes(L))n.w++; });
    const verb={o:"originate here",d:"terminate here",w:"work this yard",
                "":"touch this location"}[state.locmode];
    locbar.className="locbar show";
    const modeLabels=NARROW.matches
      ? [["","All"],["o","Orig"],["d","Term"],["w","Works"]]
      : [["","All"],["o","Originates"],["d","Terminates"],["w","Works"]];
    const counts={"":pool.length,o:n.o,d:n.d,w:n.w};
    locbar.innerHTML=`<span class="lid">#${L}</span>`+
      `<span class="big">${LOC[L]?esc(LOC[L]):"Unnamed location"}</span>`+
      `<span class="seg" id="locmodeseg">`+
        modeLabels.map(([v,lb])=>`<button data-m="${v}"${v===state.locmode?' class="on"':''}>${lb} (${counts[v]})</button>`).join("")+
      `</span>`+
      `<span class="seg" id="viewseg">`+
        [["cards","Cards"],["sheet",NARROW.matches?"Sheet":"Yard sheet"],["details","Details"]]
          .map(([v,lb])=>`<button data-v="${v}"${v===state.view?' class="on"':''}>${lb}</button>`).join("")+
      `</span>`+
      `<span class="locnote" style="color:var(--muted)">${list.length} train(s) ${verb}</span>`;
    // sibling identities on the same interface map, mother yard first
    const fam=FAM[L];
    if(fam){
      locbar.innerHTML+=`<span class="sibs"><span class="siblbl">same map</span>`+
        [[fam.mo,0]].concat(fam.ms).filter(m=>m[0]!==L)
          .map(m=>`<button class="sib${m[1]?' vid':''}" data-loc="${m[0]}" `+
            `title="${m[1]?'virtual destination (off-map)':'yard on the same interface map'}">`+
            `${esc(locLabel(m[0]))}</button>`).join("")+
        `</span>`;
      locbar.querySelectorAll(".sib").forEach(b=>{
        b.onclick=()=>gotoLoc(b.dataset.loc);
      });
    }
    locbar.querySelectorAll("#locmodeseg button").forEach(b=>{
      b.onclick=()=>{ state.locmode=b.dataset.m; render(); };
    });
    locbar.querySelectorAll("#viewseg button").forEach(b=>{
      b.onclick=()=>{ state.view=b.dataset.v; render(); };
    });
  } else locbar.className="locbar";

  // details view: facts about the place instead of a train list
  if(state.loc && state.view==="details"){
    wrap.innerHTML="";
    wrap.appendChild(detailsPanel(state.loc));
    return;
  }

  // yard sheet: a compact switchlist of the same filtered set, one row per
  // train with its full instruction text at this yard. Only meaningful with a
  // location selected; role order (originate, terminate, work, pass) then symbol.
  const sheet = state.loc && state.view==="sheet";
  if(sheet) list.sort((a,b)=>roleRank(a)-roleRank(b) || (a.fs<b.fs?-1:a.fs>b.fs?1:0));

  if(reset) wrap.innerHTML="";
  const slice=list.slice(state.shown, state.shown+PAGE);

  if(list.length===0 && reset){
    wrap.innerHTML=`<div class="empty">No trains match these filters.<br>
      Try widening the status to <b>All</b> or clearing the location.</div>`;
    return;
  }
  const frag=document.createDocumentFragment();
  if(sheet && reset){
    const h=document.createElement("div"); h.className="srow shead";
    h.innerHTML=`<span>Role</span><span>RR</span><span>Symbol</span>`+
      `<span>Origin → Destination</span><span>Work at this yard</span>`;
    frag.appendChild(h);
  }
  slice.forEach(t=>frag.appendChild(sheet?sheetRow(t):card(t)));
  const old=document.getElementById("moreWrap"); if(old) old.remove();
  wrap.appendChild(frag);
  state.shown+=slice.length;

  if(state.shown<list.length){
    const mw=document.createElement("div"); mw.id="moreWrap";
    const b=document.createElement("button"); b.className="morebtn";
    b.textContent=`Show more  (${(list.length-state.shown).toLocaleString()} left)`;
    b.onclick=()=>render(false);
    mw.appendChild(b); wrap.appendChild(mw);
  }
}

// role of a train relative to the selected location
function roleRank(t){
  const L=state.loc;
  if(t.o===L) return 0;
  if(t.d===L) return 1;
  if(t.w.includes(L)) return 2;
  return 3;
}
function roleTag(t){
  const L=state.loc, r=[];
  if(t.o===L) r.push("O");
  if(t.d===L) r.push("T");
  if(t.w.includes(L)) r.push("W");
  return r.join("·") || "–";
}

function sheetRow(t){
  const el=document.createElement("div");
  el.className="srow"; el.dataset.uid=t.i;
  const work=allInstrAt(t,state.loc);
  el.innerHTML=
    `<span class="role" title="O originates · T terminates · W works">${roleTag(t)}</span>`+
    `<span><span class="rr" style="background:${rrColor(t.rr)}">${esc(t.rr)}</span></span>`+
    `<span class="ssym">${esc(t.fs)}</span>`+
    `<span class="sod">${esc(locLabel(t.o))} → ${esc(locLabel(t.d))}</span>`+
    `<span class="stxt${work.length?'':' none'}">${work.length?work.map(esc).join("<br>"):"—"}</span>`;
  el.onclick=()=>{ state.view="cards"; render(); reveal(t.i); };
  return el;
}

// ---- location details ----
const MI=3959, RAD=Math.PI/180;
// Routable work yards. TSAR authors sometimes tag a block's onward pickups on
// LATER trains into a train's own notes (MALSS carries MSSNP's Mason City /
// Boone / Grand Island work), which would forge board/alight points the train
// never visits. A work yard is routable only if it is on the route, or
// coordinates prove it lies along the train's corridor; unprovable off-route
// tags stay display-only. Added coordinates promote real ones automatically.
// Work has direction (Zach's rule): an explicit "pick up"/"add"/"lift" is a
// door ONTO the train, an explicit "set out"/"drop"/"deliver" is a door OFF
// it, and nothing else counts — MSSNP only picks up at Mason City, so a car
// cannot alight there; the Mason City block is built at Boone by MNPSS and
// set out on arrival. Origin (board) and destination (alight) stay implicit.
const PICKUP_RE=/\b(pi+cks?[\s-]?up|lift|add)\b/i,          // "picks up", "Piick Up" typo
      SETOUT_RE=/\b(set[\s-]?(?:out|off)|drop|deliver)\b/i; // "Set Off" is NS house style
DATA.trains.forEach(t=>{
  t.wr=t.ww.filter(y=>{
    if(y===t.o||y===t.d||t.r.includes(y)) return true;
    const go=geoOf(t.o), gd=geoOf(t.d), gy=geoOf(y);
    if(!go||!gd||!gy) return false;
    return distMi(go,gy)+distMi(gy,gd) <= Math.max(distMi(go,gd),50)*1.2;
  });
  t.wb=t.wr.filter(y=>(t.wn[y]||[]).some(n=>PICKUP_RE.test(n)));   // boardable
  t.wa=t.wr.filter(y=>(t.wn[y]||[]).some(n=>SETOUT_RE.test(n)));   // alightable
});
function distMi(a,b){
  const s=Math.sin((b[0]-a[0])*RAD/2)**2 +
          Math.cos(a[0]*RAD)*Math.cos(b[0]*RAD)*Math.sin((b[1]-a[1])*RAD/2)**2;
  return 2*MI*Math.asin(Math.sqrt(s));
}
function compass(a,b){
  const y=Math.sin((b[1]-a[1])*RAD)*Math.cos(b[0]*RAD);
  const x=Math.cos(a[0]*RAD)*Math.sin(b[0]*RAD) -
          Math.sin(a[0]*RAD)*Math.cos(b[0]*RAD)*Math.cos((b[1]-a[1])*RAD);
  const deg=(Math.atan2(y,x)/RAD+360)%360;
  return ["N","NE","E","SE","S","SW","W","NW"][Math.round(deg/45)%8];
}

function detailsPanel(L){
  const el=document.createElement("div"); el.className="dpanel";
  const g=GEO[L], fam=FAM[L];
  let h=`<h4>Location</h4><div class="drow"><span class="lid">#${L}</span> `+
    `<b>${esc(locLabel(L))}</b></div>`;
  if(g){
    h+=`<div class="drow dim">${g[0].toFixed(4)}, ${g[1].toFixed(4)} · `+
      `<a class="ext" href="https://www.openrailwaymap.org/?style=standard&lat=${g[0]}&lon=${g[1]}&zoom=13" `+
      `target="_blank" rel="noopener">OpenRailwayMap ↗</a> · `+
      `<a class="ext" href="https://www.openstreetmap.org/?mlat=${g[0]}&mlon=${g[1]}#map=12/${g[0]}/${g[1]}" `+
      `target="_blank" rel="noopener">OpenStreetMap ↗</a></div>`;
    if(g[2]) h+=`<div class="drow">Railroads: <b>${esc(g[2])}</b></div>`;
  } else {
    h+=`<div class="drow dim">No coordinates recorded for this map.</div>`;
  }
  if(fam){
    h+=`<h4>Same interface map</h4>`;
    const rows=[[fam.mo,0,1]].concat(fam.ms.map(m=>[m[0],m[1],0]));
    h+=rows.map(([id,vid,mo])=>{
      const gg=GEO[id];
      return `<div class="drow">`+
        `<button class="sib${vid?' vid':''}${id===L?' cur':''}" data-loc="${id}">${esc(locLabel(id))}</button>`+
        `<span class="dim"> ${mo?"interface map":vid?"virtual destination (off-map)":"co-located yard"}`+
        (gg&&gg[2]?` · ${esc(gg[2])}`:"")+`</span></div>`;
    }).join("");
  }
  if(g){
    const near=Object.keys(GEO)
      .filter(id=>id!==L && !(fam&&(id===fam.mo||fam.ms.some(m=>m[0]===id))))
      .map(id=>[id, distMi(g, GEO[id])])
      .sort((a,b)=>a[1]-b[1]).slice(0,8);
    if(near.length){
      h+=`<h4>Nearby</h4>`+near.map(([id,d])=>
        `<div class="drow"><button class="sib" data-loc="${id}">${esc(locLabel(id))}</button>`+
        `<span class="dim"> ${d<10?d.toFixed(1):Math.round(d)} mi ${compass(g,GEO[id])}</span></div>`).join("");
    }
  }
  el.innerHTML=h;
  el.querySelectorAll(".sib").forEach(b=>{
    if(!b.classList.contains("cur")) b.onclick=()=>gotoLoc(b.dataset.loc);
  });
  return el;
}

function card(t){
  const st=status(t);
  const el=document.createElement("div"); el.className="card"; el.dataset.uid=t.i;
  const head=document.createElement("div"); head.className="head";
  const here = state.loc ? instrAt(t,state.loc) : "";
  head.innerHTML=
    `<span class="rr" style="background:${rrColor(t.rr)}">${esc(t.rr)}</span>`+
    `<span class="sym">${esc(t.fs)}</span>`+
    (t.nm?`<span class="tnm" title="${esc(t.nm)}">${esc(t.nm)}</span>`:"")+
    `<span class="ty">${esc(t.ty)}</span>`+
    `<span class="od"><button class="odlink" data-loc="${t.o}">${esc(locLabel(t.o))}</button>`+
    `<span class="arr">→</span><button class="odlink" data-loc="${t.d}">${esc(locLabel(t.d))}</button></span>`+
    `<span class="spacer"></span>`+
    (here?`<span class="here" title="what this train does here">@ ${esc(here.length>44?here.slice(0,42)+'…':here)}</span>`:"")+
    `<span class="status ${st}">${st}</span>`+
    `<span class="chev">▸</span>`;
  head.querySelectorAll("[data-loc]").forEach(b=>{
    b.onclick=e=>{e.stopPropagation(); gotoLoc(b.dataset.loc);};
  });
  head.onclick=()=>openCard(el,t);
  el.appendChild(head);
  const body=document.createElement("div"); body.className="body";
  el.appendChild(body);
  return el;
}

function openCard(el,t,force){
  const willOpen = force ? true : !el.classList.contains("open");
  el.classList.toggle("open", willOpen);
  if(willOpen && !el.dataset.built){ buildBody(el,t); el.dataset.built="1"; }
}

function buildBody(el,t){
  const body=el.querySelector(".body");
  const exp = t.x==="2099-12-31" ? "no expiry" : (t.x||"—");
  const eff = t.f==="2000-01-01" ? "always" : (t.f||"—");
  const nodes=t.r.map((id,i)=>{
    const end=i===0||i===t.r.length-1;
    const hit=state.loc&&id===state.loc;
    const nm=LOC[id];
    return `<button class="node ${hit?'hit':''} ${end?'end':''}" data-loc="${id}">`+
      (nm?esc(nm):"")+`<span class="id">${nm?" ":""}#${id}</span></button>`;
  }).join("");

  const instr=t.g.map(g=>{
    if(g[0]===SEG_YARD){
      const nm = g[3] !== undefined ? g[3] : (LOC[g[1]]||"");
      return `<div class="line"><span class="tag yard">${g[1]}</span>`+
        `<span><span class="yn">${nm?esc(nm):""}<span class="id"> #${g[1]}</span></span>`+
        (g[2]?` — <span class="txt">${esc(g[2])}</span>`:"")+`</span></div>`;
    }
    if(g[0]===SEG_IC){
      const link = g[2]!==undefined
        ? `<button class="jump" data-jump="${g[2]}">${esc(g[1])}</button>`
        : `<b>${esc(g[1])}</b>`;
      return `<div class="line"><span class="tag ic">INTERCHANGE</span>`+
        `<span class="txt">Connects with ${link}</span></div>`;
    }
    if(g[0]===SEG_BREAK) return `<div class="brk"></div>`;
    return `<div class="line plain"><span class="txt">${esc(g[1])}</span></div>`;
  }).join("") || `<div class="line plain"><span class="txt">No instructions recorded.</span></div>`;

  body.innerHTML=
    `<div class="dates">Effective ${esc(eff)} · Expires ${esc(exp)}</div>`+
    `<h4>Route · ${t.r.length} stop${t.r.length===1?"":"s"}</h4><div class="route">${nodes}</div>`+
    `<h4>Instructions</h4><div class="instr">${instr}</div>`;

  body.querySelectorAll("[data-jump]").forEach(b=>{
    b.onclick=e=>{e.stopPropagation(); jumpTo(+b.dataset.jump);};
  });
  body.querySelectorAll("[data-loc]").forEach(b=>{
    b.onclick=e=>{e.stopPropagation(); gotoLoc(b.dataset.loc);};
  });
}

// Page through the current card list until the train's card exists, then
// open it, scroll to it and flash it. Assumes the filters already admit it.
function reveal(uid){
  const t=DATA.trains[uid];
  requestAnimationFrame(()=>{
    let el=wrap.querySelector(`.card[data-uid="${uid}"]`);
    while(!el && state.shown<filtered().length){ render(false); el=wrap.querySelector(`.card[data-uid="${uid}"]`); }
    if(!el) return;
    openCard(el,t,true);
    el.scrollIntoView({block:"center",behavior:"smooth"});
    el.classList.add("flash"); setTimeout(()=>el.classList.remove("flash"),1600);
  });
}

// Follow an interchange link: widen the filters just enough to reveal the
// partner train, then scroll to it and open it.
function jumpTo(uid){
  const t=DATA.trains[uid]; if(!t) return;
  rstate.res=null;      // interchange links leave the route view too
  if(!state.rrs.has(t.rr)){ state.rrs.add(t.rr); paintPill(t.rr); }
  state.type="";
  state.loc=""; state.locmode=""; state.view="cards"; locin.value=""; locclear.style.display="none";
  state.sym=t.fs.replace(/[^A-Za-z0-9]/g,"").toUpperCase(); symin.value=t.fs;
  setStat("all");
  render();
  reveal(uid);
}

// Click on a route stop / O-D endpoint: focus the location filter on it.
function gotoLoc(id){
  choose({id, nm: LOC[id] || ""});
  window.scrollTo({top: 0, behavior: "smooth"});
}

function debounce(fn,ms){let h;return(...a)=>{clearTimeout(h);h=setTimeout(()=>fn(...a),ms);};}

// ---- car routing (beta) ----
// Structure only, no text interpretation: a car boards where a train
// boards where a train originates or explicitly picks up, rides forward in
// route order, alights where it explicitly sets out or terminates,
// where it works or terminates; trains hand off wherever drop and pickup
// share a physical map (same MIM family — the yardmaster classifies across
// yard identities) and at resolved *IC* links. Chains rank by (starts on the
// origin's home road, fewest interchanges, fewest trains, least geographic
// wandering) and collapse into corridors — one entry per distinct railroad +
// transfer-map sequence. The TSAR text at each hand-off is quoted verbatim:
// the graph proposes, the player judges.
// freight capability set of a train, from the per-roster classification
// table ("carload+intermodal" for mixed service). Multi rosters: shortline
// operators haul carload, passenger is passenger.
function fclasses(t){
  const r=RR[t.rr]; if(!r) return ["carload"];
  if(r.m) return r.pax ? ["passenger"] : ["carload"];
  return (r.fc[t.ty] || "nonrev").split("+");
}
const rstate={from:null,to:null,res:null};
const rfin=document.getElementById("rfin"), rtin=document.getElementById("rtin");
const rgo=document.getElementById("rgo"), rclear=document.getElementById("rclear");
const rcartype=document.getElementById("rcartype");

function wirePicker(inp,menu,key){
  let ropts=[];
  function open(q){
    q=q.trim().toLowerCase();
    ropts=pickList.filter(o=>!q||o.id.includes(q)||o.nm.toLowerCase().includes(q)).slice(0,60);
    menu.innerHTML=ropts.map((o,i)=>
      `<div class="opt" data-i="${i}"><span class="lid">${o.id}</span>`+
      `<span class="lnm ${o.nm?'':'unk'}">${o.nm?esc(o.nm):'unnamed'}</span>`+
      `<span class="cnt">${o.c} trains</span></div>`).join("")
      || `<div class="opt"><span class="lnm unk">no match</span></div>`;
    menu.classList.add("open");
    menu.querySelectorAll(".opt").forEach(el=>{ el.onclick=()=>{
      const o=ropts[+el.dataset.i]; if(!o) return;
      rstate[key]=o; inp.value=o.id+(o.nm?"  "+o.nm:"");
      menu.classList.remove("open"); routeNow();
    };});
  }
  inp.onfocus=()=>open(inp.value);
  inp.oninput=()=>{ rstate[key]=null; open(inp.value); syncRouteBtns(); };
  document.addEventListener("click",e=>{ if(!inp.parentElement.contains(e.target)) menu.classList.remove("open"); });
}
wirePicker(rfin,document.getElementById("rfmenu"),"from");
wirePicker(rtin,document.getElementById("rtmenu"),"to");
rcartype.onchange=()=>{ if(rstate.res) routeNow(); };
rgo.onclick=()=>routeNow();
rclear.onclick=()=>{ rstate.from=rstate.to=rstate.res=null; rfin.value=""; rtin.value="";
  syncRouteBtns(); render(); };
function syncRouteBtns(){
  rgo.style.display=(rstate.from&&rstate.to)?"":"none";
  rclear.style.display=(rfin.value||rtin.value)?"":"none";
}
function routeNow(){
  syncRouteBtns();
  if(!(rstate.from&&rstate.to)||rstate.from.id===rstate.to.id) return;
  rstate.res=findRoutes(rstate.from.id,rstate.to.id,rcartype.value);
  render();
}

function findRoutes(src,dst,carType){
  // pool by capability: passenger, light power and non-revenue types never
  // carry a routed car; otherwise a train qualifies when its capability set
  // includes the selected car type ("any" = any revenue freight service)
  const canCarry=t=>{
    const cs=fclasses(t);
    if(cs.includes("passenger")||cs.includes("engines")||cs.includes("nonrev")) return false;
    return carType==="any" || cs.includes(carType);
  };
  const srcMap=mapOf(src), dstMap=mapOf(dst);
  if(srcMap===dstMap) return {src,dst,homeRR:null,corridors:[],total:0,truncated:false};
  const pool=DATA.trains.filter(t=>status(t)==="active"&&canCarry(t));
  const inPool=new Set(pool.map(t=>t.i));
  // boarding index by physical map; entries keep the train's own yard id
  const boardAt={};
  pool.forEach(t=>{
    const seen=new Set(t.wb); seen.add(t.o);
    seen.forEach(y=>{ if(y){const k=mapOf(y);(boardAt[k]=boardAt[k]||[]).push([t,y]);} });
  });
  const homeCnt={};
  pool.forEach(t=>{ if(t.o){ const k=mapOf(t.o), c=homeCnt[k]=homeCnt[k]||{}; c[t.op]=(c[t.op]||0)+1; } });
  const homeRR=homeCnt[srcMap]?Object.entries(homeCnt[srcMap]).sort((a,b)=>b[1]-a[1])[0][0]:null;

  const arriveAt=t=>{
    if(t.d&&mapOf(t.d)===dstMap) return t.d;
    return t.wa.find(y=>mapOf(y)===dstMap);
  };
  const orderOk=(t,y1,y2)=>{
    const i1=t.r.indexOf(y1), i2=t.r.indexOf(y2);
    return (i1<0||i2<0)?true:i2>i1;
  };
  // Many different prefixes reach the same (train, boarding yard); exploring
  // each one re-walks an identical suffix. A few per state keeps corridor
  // variety without the exponential blowup.
  const MAXH=5, CAP_RES=400, CAP_Q=400000, PER_STATE=4;
  const chains=[], seenChain=new Set(), visits={};
  let truncated=false;
  const push=(q,path,t,y)=>{
    const k=t.i+"@"+y, v=visits[k]||0;
    if(v>=PER_STATE) return;
    visits[k]=v+1; q.push(path.concat([[t,y]]));
  };
  const q=[];
  (boardAt[srcMap]||[]).forEach(([t,y])=>push(q,[],t,y));
  for(let qi=0;qi<q.length;qi++){
    if(chains.length>=CAP_RES||q.length>CAP_Q){ truncated=true; break; }
    const path=q[qi], last=path[path.length-1], t=last[0], by=last[1];
    const ay=arriveAt(t);
    if(ay!==undefined&&mapOf(by)!==dstMap&&orderOk(t,by,ay)){
      const key=path.map(p=>p[0].i).join(">");
      if(!seenChain.has(key)){ seenChain.add(key); chains.push(path); }
      continue;
    }
    if(path.length>=MAXH) continue;
    const used=new Set(path.map(p=>p[0].i));
    const drops=t.d?[t.d]:[];
    t.wa.forEach(y=>{ if(!drops.includes(y)) drops.push(y); });
    const dmaps=new Set();
    for(const y of drops){
      const m=mapOf(y);
      if(m===mapOf(by)||dmaps.has(m)||!orderOk(t,by,y)) continue;
      dmaps.add(m);
      for(const [u,yu] of (boardAt[m]||[])) if(!used.has(u.i)) push(q,path,u,yu);
    }
    for(const uid of t.icl){
      const u=DATA.trains[uid];
      if(u&&inPool.has(u.i)&&!used.has(u.i)) push(q,path,u,u.o);
    }
  }

  // Agreement-aware ranking: a hand-off at a known exchange point for that
  // pair is blessed (more so for a preferred partner); an op-change involving
  // a shortline that the agreement table says has no such partner is heavily
  // demoted; Class I <-> Class I hand-offs have no agreement data and stay
  // neutral. A "foreign" first leg is fine when the origin yard is an agreed
  // exchange point for that operator (a car AT an interchange yard is exactly
  // where a partner picks it up). Hand-offs riding a family alias (drop and
  // pickup are different identities on one map) pay a token +3 so exact-yard
  // chains keep their edge, and where coordinates are known a corridor pays
  // 1pt per 25 miles it wanders beyond the direct origin->destination line.
  // A hand-off with no coordinates is bridged over (its detour is invisible),
  // so it pays +1: on otherwise-equal scores, verifiable geometry wins.
  const gS=geoOf(src), gD=geoOf(dst);
  const direct=(gS&&gD)?distMi(gS,gD):null;
  const score=res=>{
    const ops=res.map(p=>p[0].op);
    let ic=0, adj=0;
    for(let i=1;i<res.length;i++){
      const prev=res[i-1][0], y=res[i][1];
      if(!(prev.d===y||prev.wa.includes(y))) adj+=3;
      const a=ops[i-1], b=ops[i];
      if(a===b) continue;
      ic++;
      const yset=IXY[a+"|"+b];
      if(yset&&ixHasAt(yset,y)) adj += ixPreferred(a,b)?-30:-15;
      else if(yset) adj += 10;
      else if(!ixPartners(a,b)&&(SLOPS.has(a)||SLOPS.has(b))) adj += 2000;
    }
    let geo=0;
    if(direct!=null){
      const pts=[gS];
      for(let i=1;i<res.length;i++){
        const g=geoOf(res[i][1]);
        if(g) pts.push(g); else geo+=1;
      }
      pts.push(gD);
      let d=0; for(let i=1;i<pts.length;i++) d+=distMi(pts[i-1],pts[i]);
      geo+=Math.round(Math.max(0,d-direct)/25);
    }
    const foreign=(homeRR&&ops[0]!==homeRR&&!opIxAt(ops[0],src))?1:0;
    return foreign*10000+ic*100+res.length+adj+geo;
  };
  const corr={};
  chains.forEach(res=>{
    const key=res.map(p=>p[0].op).join(",")+"|"+res.map(p=>mapOf(p[1])).join(",");
    const cur=corr[key];
    if(!cur) corr[key]={best:res,n:1};
    else { cur.n++; if(score(res)<score(cur.best)) cur.best=res; }
  });
  const corridors=Object.values(corr).sort((a,b)=>score(a.best)-score(b.best));
  return {src,dst,homeRR,corridors,total:chains.length,truncated,score};
}

function renderRoutes(){
  const R=rstate.res;
  locbar.className="locbar";
  countEl.innerHTML=`<b>${R.corridors.length}</b> corridor(s), ${R.total} chain(s)`;
  wrap.innerHTML="";
  const hd=document.createElement("div"); hd.className="rhead";
  hd.innerHTML=`<b>${esc(locLabel(R.src))}</b> → <b>${esc(locLabel(R.dst))}</b>`+
    ` · <b>${esc(rcartype.options[rcartype.selectedIndex].text)}</b> · `+
    `candidate routings built only from where active trains originate, terminate and work. `+
    `The TSARs' own instructions are quoted at each hand-off — judge them before trusting a chain.`+
    (R.homeRR?` Home road at the origin looks like <b>${esc(R.homeRR)}</b>.`:"")+
    (R.truncated?` <span class="warn">Search hit its size cap; distant results may be incomplete.</span>`:"");
  wrap.appendChild(hd);
  if(!R.corridors.length){
    const d=document.createElement("div"); d.className="empty";
    d.innerHTML = mapOf(R.src)===mapOf(R.dst)
      ? `Origin and destination are identities on the same interface map — `+
        `no road train needed; the yardmaster classifies the car across.`
      : `No chain found within 5 trains.`+
        (rcartype.value!=="any"?`<br>Try car type “Any freight”.`:``);
    wrap.appendChild(d); return;
  }
  R.corridors.slice(0,30).forEach(c=>wrap.appendChild(corridorCard(c)));
  if(R.corridors.length>30){
    const d=document.createElement("div"); d.className="rhead";
    d.textContent=`…and ${R.corridors.length-30} more corridor(s) not shown.`;
    wrap.appendChild(d);
  }
}

function corridorCard(c){
  const R=rstate.res, res=c.best, ops=res.map(p=>p[0].op);
  const dstMap=mapOf(R.dst);
  let ic=0; for(let i=1;i<ops.length;i++) if(ops[i]!==ops[i-1]) ic++;
  const el=document.createElement("div"); el.className="corr";
  const h=document.createElement("div"); h.className="chead";
  h.innerHTML=`<b>${res.length} train${res.length>1?"s":""}</b> · ${ic} interchange${ic===1?"":"s"}`+
    (c.n>1?` · ${c.n} variants`:"")+
    ((R.homeRR&&ops[0]!==R.homeRR&&!opIxAt(ops[0],R.src))
      ?` · <span class="warn">starts on a foreign road</span>`:"");
  el.appendChild(h);
  res.forEach((p,i)=>{
    const t=p[0], by=p[1];
    const ay=i===res.length-1
      ?((t.d&&mapOf(t.d)===dstMap)?t.d:(t.wa.find(y=>mapOf(y)===dstMap)||R.dst))
      :res[i+1][1];
    if(i>0&&t.op!==res[i-1][0].op){
      const a=res[i-1][0].op, b=t.op, yset=IXY[a+"|"+b];
      const nl=document.createElement("div");
      if(yset&&ixHasAt(yset,by)){
        nl.className="legnote ok";
        nl.textContent=`✓ ${a}–${b} exchange point`+(ixPreferred(a,b)?" (preferred partner)":"");
        el.appendChild(nl);
      } else if(!yset&&!ixPartners(a,b)&&(SLOPS.has(a)||SLOPS.has(b))){
        nl.className="legnote bad";
        nl.textContent=`⚠ no agreement between ${a} and ${b} in the interchange data`;
        el.appendChild(nl);
      }
    }
    if(i>0){
      const prev=res[i-1][0];
      const pd=(prev.d?[prev.d]:[]).concat(prev.wa);
      if(!pd.includes(by)&&pd.some(y=>mapOf(y)===mapOf(by))&&FAM[by]){
        const nl=document.createElement("div"); nl.className="legnote";
        nl.textContent=`⇄ same-map hand-off — drop and pickup are different `+
          `identities on the ${locLabel(FAM[by].mo)} map`;
        el.appendChild(nl);
      }
    }
    const leg=document.createElement("div"); leg.className="leg";
    leg.innerHTML=
      `<span class="chev">▸</span>`+
      `<span class="rr" style="background:${rrColor(t.rr)}">${esc(t.op)}</span>`+
      `<span class="ssym">${esc(t.fs)}</span>`+
      `<span class="lty">${esc(t.ty)}</span>`+
      `<span class="lod">${esc(locLabel(by))} → ${esc(locLabel(ay))}</span>`;
    el.appendChild(leg);
    if(i===res.length-1&&ay!==R.dst&&mapOf(ay)===dstMap){
      const nl=document.createElement("div"); nl.className="legnote ok";
      nl.textContent=`✓ ${locLabel(ay)} shares its map with ${locLabel(R.dst)}`;
      el.appendChild(nl);
    }
    const notes=[];
    [by,ay].forEach(y=>famIds(y).forEach(fy=>(t.wn[fy]||[]).forEach(n=>notes.push([fy,n]))));
    notes.forEach(([y,n])=>{
      const nl=document.createElement("div"); nl.className="legnote";
      nl.innerHTML=`@ ${esc(locLabel(y))}: <span class="txt">${esc(n)}</span>`;
      el.appendChild(nl);
    });
    let cardEl=null;
    leg.onclick=()=>{
      if(cardEl){ cardEl.remove(); cardEl=null; return; }
      cardEl=card(t); openCard(cardEl,t,true); leg.after(cardEl);
    };
  });
  return el;
}

render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(
        description="Build a static train-finder site from TSAR .ini files.")
    ap.add_argument('files', nargs='*', help='TSAR_<RR>.ini files (default: everything in --folder)')
    ap.add_argument('-f', '--folder', default='TSARs', help='folder of TSAR_*.ini files')
    ap.add_argument('-o', '--out', default=os.path.join('docs', 'index.html'))
    ap.add_argument('-l', '--locations', default='locations.csv',
                    help='location name store, CSV of id,name,source')
    ap.add_argument('-t', '--title', default='Train Finder')
    ap.add_argument('--only', help='comma separated railroad codes to include, e.g. UP,BNSF')
    ap.add_argument('--strict', action='store_true',
                    help='exit non-zero if anything about the input format is unrecognised')
    ap.add_argument('--no-location-cache', action='store_true',
                    help='do not read or update the location name store')
    a = ap.parse_args()

    files = a.files
    if not files:
        if not os.path.isdir(a.folder):
            sys.exit(f"no files given and folder not found: {a.folder}")
        files = sorted(glob.glob(os.path.join(a.folder, '*.ini')))
        if not files:
            sys.exit(f"no .ini files in {a.folder}")
    for f in files:
        if not os.path.isfile(f):
            sys.exit(f"file not found: {f}")

    if a.only:
        want = {c.strip().upper() for c in a.only.split(',') if c.strip()}
        files = [f for f in files if railroad_from_filename(f) in want]
        if not files:
            sys.exit(f"--only {a.only} matched none of the available files")

    print(f"Building train site from {len(files)} file(s)…")
    n = build(files, a.out, a.title, a.locations, use_cache=not a.no_location_cache)
    if n and a.strict:
        sys.exit(f"\n{n} unrecognised construct(s) in the input; --strict, so failing.")


if __name__ == '__main__':
    main()
