"""Results folder for one modernize.py invocation, and the status report.

<results>/<repo>-<timestamp>-<stage>/
  console.log          everything printed (also shown on the terminal)
  before/, after/      migration_state.json, progress.md, decisions.md around the run
  after/characterization/   the Verification Gate's cases, if any
  runs/<agent>/<ts>/   trajectory.jsonl and result.json of every agent session
  status.txt           the status report after the run
  EXIT                 exit code (written last: other jobs can wait for it)
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import List

from . import state

RECORDS = (state.STATE_FILE, state.PROGRESS_FILE, state.DECISIONS_FILE)


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)
            st.flush()
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


class Results:
    def __init__(self, root: str, repo: str, stamp: str, stage: str):
        self.dir = os.path.join(root, '%s-%s-%s' % (os.path.basename(repo), stamp, stage))
        self.repo = repo
        os.makedirs(self.dir, exist_ok=True)
        self.log = open(os.path.join(self.dir, 'console.log'), 'a', encoding='utf-8')
        sys.stdout = Tee(sys.__stdout__, self.log)
        sys.stderr = Tee(sys.__stderr__, self.log)
        self.run_dirs: List[str] = []
        self.snapshot('before')

    def snapshot(self, name: str):
        d = os.path.join(self.dir, name)
        os.makedirs(d, exist_ok=True)
        for f in RECORDS:
            p = os.path.join(self.repo, f)
            if os.path.exists(p):
                shutil.copy2(p, d)

    def close(self, code: int):
        self.snapshot('after')
        char = os.path.join(self.repo, '.migration', 'characterization')
        if os.path.isdir(char):
            shutil.copytree(char, os.path.join(self.dir, 'after', 'characterization'), dirs_exist_ok=True)
        for rd in self.run_dirs:
            if os.path.isdir(rd):
                agent = os.path.basename(os.path.dirname(rd))
                shutil.copytree(rd, os.path.join(self.dir, 'runs', agent, os.path.basename(rd)), dirs_exist_ok=True)
        with open(os.path.join(self.dir, 'status.txt'), 'w', encoding='utf-8') as f:
            f.write(status_report(self.repo) + '\n')
        print('\nresults: %s' % self.dir, flush=True)
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        self.log.close()
        with open(os.path.join(self.dir, 'EXIT'), 'w') as f:
            f.write('%d\n' % code)


def status_report(repo: str) -> str:
    st = state.load_state(repo)
    if not st:
        return 'no migration_state.json in %s: nothing mapped yet (run --stage map)' % repo
    lines = ['repo: %s   updated: %s' % (repo, st.get('updated_at')), '',
             '%-26s %-12s %-10s %-16s %-6s %s' % ('file', 'status', 'rules', 'verdict', 'repair', 'decisions'),
             '-' * 90]
    counts = {}
    for unit in st['migration_order']:
        for p in unit:
            f = next(x for x in st['files'] if x['path'] == p)
            s = f.get('status', 'pending')
            counts[s] = counts.get(s, 0) + 1
            v = (f.get('verification') or {}).get('verdict', '-')
            lines.append('%-26s %-12s %-10s %-16s %-6s %s' % (
                p, s, ','.join(f.get('rules', [])) or '-', v, 'yes' if f.get('needs_repair') else '-',
                ','.join(f.get('decisions', [])) or '-'))
    total = len(st['files'])
    c = st.get('consistency') or {}
    passing = counts.get('passing', 0)
    done = passing + counts.get('failing', 0)
    lines += ['', 'files: %d (%s)' % (total, ', '.join('%d %s' % (n, s) for s, n in sorted(counts.items()))),
              'cross-file consistency: %s%s' % (c.get('verdict', 'not run'), ' (%s)' % c['at'][:16] if c.get('at') else ''),
              'success criterion (every file passing or failing, >95%% passing): %s - %d/%d passing (%.0f%%)' % (
                  'met' if done == total and total and passing / total > 0.95 else 'not met', passing, total,
                  100.0 * passing / total if total else 0)]
    blockers = [(f['path'], b) for f in st['files'] for b in f.get('blockers', [])] + [('repo', b) for b in st.get('blockers', [])]
    if blockers:
        lines += ['', 'blockers:'] + ['  %s: %s' % (p, (b.get('description') or b.get('subject', ''))[:110])
                                       for p, b in blockers]
    return '\n'.join(lines)
