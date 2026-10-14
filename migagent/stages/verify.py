"""Stage `verify`: the Verification Gate (demo step 5).

For every migrated file, in migration order: the repo's own tests, the file's
verification command and characterization cases, under the Python 2 baseline and
the migrated Python 3 code. equivalent -> passing; not_equivalent -> in_progress
with needs_repair; unverifiable -> blocked.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile

from .. import loop, state
from ..cli import make_backend, say
from ..verify import prompts
from ..verify.rules_backend import RulesBackend
from ..verify.tools import Env, VerificationGateTools

NAME = 'verification_gate'


def add_args(ap):
    g = ap.add_argument_group('stage verify (Verification Gate)')
    g.add_argument('--reverify', action='store_true',
                   help='also verify files already passing or unchanged since their last verdict')
    g.add_argument('--min-cases', type=int, default=10, help='characterization cases required (seeds count)')
    g.add_argument('--min-model-cases', type=int, default=None,
                   help='of which written by the model (default 5; 0 with --backend rules)')


def runs_with(st: dict, path: str) -> set:
    """Every file whose code runs when this file's checks run: the file, the test files of its
    verification command, the package __init__ files above each, and their dependencies (transitively)."""
    by = {f['path']: f for f in st['files']}
    start = {path}
    for part in (by[path].get('verification_command') or '').split('&&'):
        toks = shlex.split(part)
        if len(toks) >= 2 and toks[1] in by:
            start.add(toks[1])
    seen, stack = set(), list(start)
    while stack:
        p = stack.pop()
        if p in seen or p not in by:
            continue
        seen.add(p)
        stack += by[p].get('depends_on', [])
        d = os.path.dirname(p)
        while d:
            stack.append(d + '/__init__.py')
            d = os.path.dirname(d)
    return seen


def pick_files(repo: str, st: dict, only: str = None, reverify: bool = False):
    by_path = {f['path']: f for f in st['files']}
    todo, notes = [], []
    for unit in st['migration_order']:
        for p in unit:
            f = by_path[p]
            if only and p != only:
                continue
            if not f.get('migration') or f.get('status') not in ('in_progress', 'passing'):
                if only:
                    notes.append('%s: status %s, not migrated' % (p, f.get('status')))
                continue
            if f['status'] == 'passing' and not reverify:
                continue
            waiting = sorted(q for q in runs_with(st, p) if by_path[q].get('status', 'pending') in ('pending', 'blocked'))
            if waiting:
                notes.append('%s waits: its checks also run %d files not migrated yet (%s%s)' % (
                    p, len(waiting), ', '.join(waiting[:4]), ', ...' if len(waiting) > 4 else ''))
                continue
            v = f.get('verification') or {}
            with open(os.path.join(repo, p), 'rb') as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            if v.get('migrated_sha256') == sha and not reverify:
                notes.append('%s: unchanged since its last verdict (%s); --reverify to run again' % (p, v.get('verdict')))
                continue
            todo.append(p)
    return todo, notes


def run(a, log_path: str) -> dict:
    st = state.load_state(a.repo)
    if not st:
        return {'finished': False, 'reason': 'no migration_state.json: run --stage map first'}
    min_model = a.min_model_cases if a.min_model_cases is not None else (0 if a.backend == 'rules' else 5)
    todo, notes = pick_files(a.repo, st, a.file, a.reverify)
    say(a, 'files to verify: %s' % (todo or 'none'))
    for n in notes:
        say(a, '  ' + n)
    work = tempfile.mkdtemp(prefix='vgate-')
    files = []
    try:
        env = Env(a.repo, a.py2, a.py3, work, a.command_timeout)
        for path in todo:
            backend = make_backend(a, RulesBackend)
            tools = VerificationGateTools(env, path, backend.model, run_log=log_path, min_cases=a.min_cases,
                                          min_model_cases=min_model)
            user = prompts.USER.format(repo=a.repo, path=path, kind=tools.entry.get('kind'),
                                       rules=', '.join(tools.entry.get('rules', [])) or 'none',
                                       module=tools.module, command=tools.entry.get('verification_command'),
                                       char='yes, at least %d cases (%d your own)' % (a.min_cases, min_model)
                                       if tools.char_needed else 'no')
            system = prompts.SYSTEM.replace('{min_model}', str(min_model))
            say(a, '\n-- verify %s' % path)
            res = loop.run(backend, tools, system, user, a.max_steps or 30, log_path, on_step=lambda s: say(a, s))
            res.update(path=path, outputs=tools.outputs)
            if res['finished']:
                res['verdict'], res['status'] = tools.results['verdict'], tools.status
                say(a, '   verdict: %s -> %s' % (res['verdict'], res['status']))
            files.append(res)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ok = all(f['finished'] for f in files)
    return {'finished': ok, 'reason': 'all files verified' if ok else 'a file did not finish',
            'files': files, 'notes': notes}
