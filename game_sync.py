#!/usr/bin/env python3
"""Mirror the Dropbox game folder into game_data/ and snapshot it.

Fill Your Manifest keeps everything (TSAR rosters, per-yard map bundles,
saved trains, live car inventories) in one folder that Dropbox syncs between
Zach's machines. Rather than hand-copying rosters into TSARs/ and map files
into sample_yard_data/ after every update, this script:

  1. rsyncs the useful, non-image subset of that folder into game_data/
     (gitignored by the main repo — game data is not ours to redistribute)
  2. commits the result into a private git repo INSIDE game_data/, so
     `git -C game_data log` / `diff` show exactly what the game changed
     between syncs (rosters, map files, trains, car inventories, sorts)

Why a mirror and not reading Dropbox directly: the game rewrites files while
you play, Dropbox can leave rarely-used files as online-only placeholders
that block on read, and a snapshot is what makes the folder diffable.

Excluded: images (map .jpg, connection diagram .png — 4.6 GB of the 5 GB),
in-game Messages/ and chat, construction/, .DS_Store, and the two rosters
that are not real data: TSAR_KCS.ini (placeholder, one dummy train) and
TSAR_Tutorial.ini (the game's tutorial roster).

Usage:
    python3 game_sync.py                 # mirror + snapshot, print what changed
    python3 game_sync.py --dry-run       # show what rsync would do, no snapshot
    python3 game_sync.py --no-snapshot   # mirror only
    --game-dir DIR   source (default: $FYM_GAME_DIR, else ~/Dropbox/Freight Yard Manager)
    --dest DIR       mirror (default: game_data)
"""
import argparse
import datetime
import os
import subprocess
import sys

DEFAULT_GAME_DIR = os.path.expanduser('~/Dropbox/Freight Yard Manager')
DEFAULT_DEST = 'game_data'

# rsync filter rules, first match wins. Everything not excluded is copied.
EXCLUDES = [
    '.git/',            # the mirror's own history — never delete or overwrite
    '.gitattributes',   # written by snapshot(); not in the game folder
    '.DS_Store',
    '*.jpg', '*.png',   # map images and connection diagrams
    'Messages/',        # in-game mail
    'FYMChat31.ini',    # in-game chat
    'construction/',    # scratch space for map authoring
    'connections/',     # only PNGs; the repo keeps its own untracked copy
    'TSAR_KCS.ini',     # placeholder roster, one dummy train (also in backup/)
    'TSAR_Tutorial.ini',
]


def placeholders(root):
    """Files Dropbox holds as online-only stubs (0 blocks on disk but a
    size); reading one blocks until Dropbox downloads it."""
    n = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ('Messages', 'construction', 'connections')]
        for f in fns:
            if f.endswith(('.jpg', '.png')):
                continue
            try:
                st = os.stat(os.path.join(dp, f))
            except OSError:
                continue
            if st.st_size > 0 and st.st_blocks == 0:
                n += 1
    return n


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, **kw)


def rsync(src, dest, dry_run):
    cmd = ['rsync', '-a', '--delete']
    if dry_run:
        cmd += ['-n', '-v']
    for pat in EXCLUDES:
        cmd += ['--exclude', pat]
    cmd += [src.rstrip('/') + '/', dest.rstrip('/') + '/']
    r = run(cmd)
    if r.returncode:
        sys.exit(f'rsync failed with exit code {r.returncode}')


def snapshot(dest):
    """Commit the mirror into its own repo; return a per-folder summary or
    None when nothing changed."""
    git = ['git', '-C', dest]
    if not os.path.isdir(os.path.join(dest, '.git')):
        run(git + ['init', '-q'], check=True)
    attrs = os.path.join(dest, '.gitattributes')
    if not os.path.exists(attrs):
        # binary-ish game files: never try to diff them as text by default
        with open(attrs, 'w') as fh:
            fh.write('*.zrn binary\n*.zip binary\n*.zr1 binary\n')
    run(git + ['add', '-A'], check=True)
    if run(git + ['diff', '--cached', '--quiet']).returncode == 0:
        return None
    status = run(git + ['diff', '--cached', '--name-status'],
                 capture_output=True, check=True).stdout
    per = {}      # top folder -> {'A': [...], 'D': [...], 'M': [...]}
    for line in status.splitlines():
        code, path = line.split('\t', 1)
        code = code[0]
        if code == 'R':
            code = 'M'
        top = path.split('/', 1)[0] if '/' in path else '(root)'
        per.setdefault(top, {'A': [], 'D': [], 'M': []})[code].append(path)
    stamp = datetime.datetime.now().replace(microsecond=0).isoformat()
    run(git + ['commit', '-q', '-m', f'sync {stamp}'], check=True)
    return per


def print_summary(per):
    print('  changes since the last sync:')
    for top in sorted(per):
        a, d, m = (per[top][k] for k in 'ADM')
        parts = [f'+{len(a)}' if a else '', f'-{len(d)}' if d else '', f'~{len(m)}' if m else '']
        line = f'    {top:<14} ' + ' '.join(p for p in parts if p)
        if top == 'TSARs':
            # name the rosters themselves; the game's TSARs/backup/ copies
            # change in lockstep and would only repeat every name
            names = sorted(os.path.basename(p) for p in a + m + d
                           if p.count('/') == 1)
            if names:
                line += '   ' + ', '.join(names)
        print(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--game-dir', default=os.environ.get('FYM_GAME_DIR', DEFAULT_GAME_DIR))
    ap.add_argument('--dest', default=DEFAULT_DEST)
    ap.add_argument('--dry-run', action='store_true', help='show what rsync would do; no snapshot')
    ap.add_argument('--no-snapshot', action='store_true', help='mirror only, do not commit')
    a = ap.parse_args()

    src = os.path.expanduser(a.game_dir)
    if not os.path.isdir(src):
        sys.exit(f'game folder not found: {src}\n'
                 f'(set FYM_GAME_DIR or pass --game-dir; is Dropbox signed in on this machine?)')
    if not os.path.isdir(os.path.join(src, 'TSARs')):
        sys.exit(f'{src} has no TSARs/ folder — is this really the game folder?')

    n = placeholders(src)
    if n:
        print(f'  {n} online-only file(s) will be fetched by Dropbox during this sync '
              f'(first run can take a while)', flush=True)
    print(f'  mirroring {src}\n         -> {a.dest}', flush=True)
    os.makedirs(a.dest, exist_ok=True)
    rsync(src, a.dest, a.dry_run)
    if a.dry_run or a.no_snapshot:
        return
    per = snapshot(a.dest)
    if per is None:
        print('  no changes since the last sync')
    else:
        print_summary(per)
    print(f'  history: git -C {a.dest} log --stat')


if __name__ == '__main__':
    main()
