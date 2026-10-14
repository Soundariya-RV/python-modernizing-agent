"""Repository-wide checks for the Cross-File Consistency agent (facts only).

  R16      renamed stdlib modules are all-or-nothing across the repo
  R29      files of an import cycle share one status and one migration unit
  imports  every library/test module imports under Python 3 (migrated tree)
  R31      public API (names, signatures, special methods) is unchanged between
           the Python 2 baseline and the migrated code, and who uses each change

Each finding: {id, check, severity, path, subject, detail, evidence}.
severity: violation (fails the run, no judgement), needs_judgement (an API
change the model must accept or reject), info.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tokenize
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(os.path.dirname(HERE), 'codemig', 'py2_sites.py')
API = os.path.join(os.path.dirname(HERE), 'verify', 'py2_api.py')
RENAMED = {'ConfigParser': 'configparser', 'Queue': 'queue', 'cStringIO': 'io', 'StringIO': 'io',
           'urllib2': 'urllib.request'}
IMPLICIT = {   # special methods Python calls for you: a removal can matter without any visible caller
    '__hex__': 'hex(x) in Python 2 (Python 3 hex() uses __index__)',
    '__oct__': 'oct(x) in Python 2 (Python 3 oct() uses __index__)',
    '__nonzero__': 'bool(x), if x, while x in Python 2 (Python 3 uses __bool__)',
    '__unicode__': 'unicode(x) in Python 2 (Python 3 str() uses __str__)',
    '__cmp__': 'every comparison and sorted() in Python 2 (Python 3 uses __eq__/__lt__ ...)',
    '__div__': 'a / b in Python 2 (Python 3 uses __truediv__)',
    '__getslice__': 'x[i:j] in Python 2 (Python 3 passes a slice to __getitem__)',
    '__long__': 'long(x) in Python 2', '__coerce__': 'mixed-type arithmetic in Python 2',
}


class Finder:
    def __init__(self):
        self.findings: List[dict] = []

    def add(self, check, severity, path, subject, detail, evidence=None):
        f = {'id': 'F%03d' % (len(self.findings) + 1), 'check': check, 'severity': severity, 'path': path,
             'subject': subject, 'detail': detail, 'evidence': evidence or {}}
        self.findings.append(f)
        return f


def r16_renames(fd: Finder, st: dict, tree: str, py2: str) -> dict:
    """Old module names still imported, per module, against files that migrated that module."""
    old_users: Dict[str, List[str]] = {}
    for f in st['files']:
        p = subprocess.run([py2, '-E', '-s', SITES, os.path.join(tree, f['path']), '--residual'],
                           capture_output=True, text=True)
        if p.returncode != 0:
            continue
        for r in json.loads(p.stdout).get('residual', []):
            if r['rule'] == 'R11':
                old_users.setdefault(r['what'].split()[-1], []).append('%s:%d' % (f['path'], r['line']))
    migrated_by: Dict[str, List[str]] = {}
    for f in st['files']:
        for c in f.get('changes', []):
            if c.get('rule') == 'R11':
                mod = next((m for m in RENAMED if m in c.get('before', '')), None)
                if mod:
                    migrated_by.setdefault(mod, []).append(f['path'])
    status = {f['path']: f.get('status', 'pending') for f in st['files']}
    summary = {}
    for mod in sorted(set(old_users) | set(migrated_by)):
        users = sorted(set(old_users.get(mod, [])))
        files = sorted({u.rsplit(':', 1)[0] for u in users})
        done = sorted(set(migrated_by.get(mod, [])))
        on_old_migrated = [p for p in files if status.get(p) != 'pending']
        if users and (done or on_old_migrated):
            fd.add('R16', 'violation', ', '.join(files), mod,
                   '%s is migrated in %s but still imported under its old name in %s' % (
                       mod, ', '.join(done) or '(none)', ', '.join(users)),
                   {'old_name_uses': users, 'migrated_in': done})
            summary[mod] = 'mixed'
        elif users:
            summary[mod] = 'untouched (only files not migrated yet use it)'
        else:
            summary[mod] = 'fully migrated'
    return {'modules': summary}


def r29_cycles(fd: Finder, st: dict) -> dict:
    by = {f['path']: f for f in st['files']}
    cycles = [u for u in st.get('migration_order', []) if len(u) > 1]
    for unit in cycles:
        statuses = {p: by[p].get('status', 'pending') for p in unit}
        units = {p: (by[p].get('migration') or {}).get('unit') for p in unit}
        if len(set(statuses.values())) > 1 or len(set(units.values())) > 1:
            fd.add('R29', 'violation', ', '.join(unit), 'cycle %s' % ' <-> '.join(unit),
                   'files of one import cycle must be migrated and verified together', {'statuses': statuses,
                                                                                         'migration_units': units})
    return {'cycles': len(cycles)}


def imports(fd: Finder, st: dict, tree: str, py3: str, timeout: int = 120) -> dict:
    ok, failed = 0, 0
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONHASHSEED': '0', 'LANG': 'C.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'HOME': os.environ.get('HOME', '/tmp')}
    for f in st['files']:
        path = f['path']
        if f.get('kind') in ('library', 'test'):
            mod = f.get('module') or os.path.splitext(path)[0].replace('/', '.')
            code = ('import importlib, sys; sys.path[:0] = [".", %r]; importlib.import_module(%r)'
                    % (os.path.dirname(path) or '.', mod))
            how = 'import %s' % mod
        else:
            code = 'import sys; compile(open(%r, "rb").read(), %r, "exec")' % (path, path)
            how = 'compile %s (a %s is not imported: importing it runs it)' % (path, f.get('kind'))
        try:
            p = subprocess.run([py3, '-s', '-c', code], cwd=tree, capture_output=True, text=True,
                               timeout=timeout, env=env)
            err = None if p.returncode == 0 else (p.stderr.strip().splitlines() or ['exit %d' % p.returncode])[-1]
            trace = p.stderr[-1500:]
        except subprocess.TimeoutExpired:
            err, trace = 'timed out after %ds' % timeout, ''
        if err is None:
            ok += 1
            continue
        failed += 1
        pending = f.get('status', 'pending') == 'pending'
        fd.add('imports', 'info' if pending else 'violation', path, how,
               ('%s fails under Python 3 (not migrated yet): %s' if pending else
                '%s fails under Python 3: %s') % (how, err[:200]), {'trace': trace})
    return {'ok': ok, 'failed': failed}


def api_of(python: str, path: str, py2: bool) -> Dict[str, dict]:
    flags = ['-E', '-s'] if py2 else ['-s']
    p = subprocess.run([python] + flags + [API, path, '--dunders'], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError('API scan failed for %s: %s' % (path, p.stderr[-500:]))
    return {a['name']: a for a in json.loads(p.stdout)['api']}


def references(tree: str, files: List[str], name: str, skip: str) -> List[str]:
    """Where `name` (a module attribute or method) is used in other files of the migrated tree."""
    short = name.split('.')[-1]
    hits = []
    for rel in files:
        if rel == skip:
            continue
        try:
            with open(os.path.join(tree, rel), 'rb') as f:
                src = f.read()
        except OSError:
            continue
        try:
            toks = list(tokenize.tokenize(io.BytesIO(src).readline))
            for i, t in enumerate(toks):
                if t.type == tokenize.NAME and t.string == short:
                    hits.append('%s:%d: %s' % (rel, t.start[0], t.line.strip()[:120]))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            for n, line in enumerate(src.decode('utf-8', 'replace').splitlines(), 1):
                if re.search(r'\b%s\b' % re.escape(short), line):
                    hits.append('%s:%d: %s' % (rel, n, line.strip()[:120]))
    return hits[:30]


def r31_api(fd: Finder, st: dict, base_tree: str, mig_tree: str, py2: str, py3: str, decisions: Dict[str, dict]) -> dict:
    files = [f['path'] for f in st['files']]
    compared = 0
    for f in st['files']:
        if f.get('kind') != 'library' or not f.get('migration') or f.get('status') == 'pending':
            continue
        try:
            before = api_of(py2, os.path.join(base_tree, f['path']), True)
            after = api_of(py3, os.path.join(mig_tree, f['path']), False)
        except RuntimeError as e:
            fd.add('R31', 'violation', f['path'], 'API scan', str(e))
            continue
        compared += 1
        recorded = [d for d in decisions.values() if d['id'] in f.get('decisions', [])]
        for name in sorted(set(before) | set(after)):
            a, b = before.get(name), after.get(name)
            if a and b and a['params'] == b['params'] and a['kind'] == b['kind']:
                continue
            what = 'removed' if not b else 'added' if not a else 'signature changed'
            if what == 'added':
                fd.add('R31', 'info', f['path'], name, '%s added (new API does not break callers)' % name)
                continue
            member = name.split('.')[-1]
            dec = [d['id'] for d in recorded if member in d.get('key', '') or member in d.get('context', '')]
            refs = references(mig_tree, files, name, f['path'])
            ev = {'baseline': a, 'migrated': b, 'references_in_other_files': refs, 'recorded_by': dec}
            if member in IMPLICIT:
                ev['implicit_use'] = IMPLICIT[member]
            prior = decisions.get('api:%s:%s' % (f['path'], name))
            sev = 'needs_judgement'
            if prior and prior.get('option', '').strip('`') in ('accept', 'violation'):
                ev['reused_decision'] = prior['id']
                sev = 'info' if prior['option'].strip('`') == 'accept' else 'violation'
            detail = '%s %s%s' % (name, what, ' (recorded in %s)' % ', '.join(dec) if dec else ' (no decision records it)')
            fd.add('R31', sev, f['path'], name, detail, ev)
    return {'modules_compared': compared}
