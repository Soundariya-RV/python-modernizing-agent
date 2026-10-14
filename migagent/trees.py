"""Working copies of the target repo: the Python 2 baseline and the migrated tree.

Both are copies, so running tests never writes __pycache__ or build output
into the repo. The baseline copy puts every migrated file back to the version
it was migrated from (the git commit recorded in files[].migration, or the
copy in .migration/baseline/), so Python 2 runs see Python 2 code.
"""
from __future__ import annotations

import os
import shutil
import subprocess

IGNORE = shutil.ignore_patterns('.git', '.migration', '__pycache__', '*.pyc', '*.codemig.tmp')


def copy_repo(repo: str, dest: str) -> str:
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(repo, dest, symlinks=True, ignore=IGNORE)
    return dest


def baseline_source(repo: str, entry: dict):
    """The Python 2 bytes a migrated file came from, or None if it was never migrated."""
    mig = entry.get('migration')
    if entry.get('status', 'pending') == 'pending' or not mig:
        return None
    if mig.get('baseline_copy'):
        with open(os.path.join(repo, mig['baseline_copy']), 'rb') as f:
            return f.read()
    if mig.get('baseline_ref'):
        p = subprocess.run(['git', '-C', repo, 'show', '%s:%s' % (mig['baseline_ref'], entry['path'])],
                           capture_output=True)
        if p.returncode == 0:
            return p.stdout
    raise RuntimeError('no baseline recorded for %s' % entry['path'])


def baseline_tree(repo: str, state: dict, dest: str) -> str:
    copy_repo(repo, dest)
    for f in state['files']:
        src = baseline_source(repo, f)
        if src is not None:
            with open(os.path.join(dest, f['path']), 'wb') as fh:
                fh.write(src)
    return dest
