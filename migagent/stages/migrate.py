"""Stage `migrate`: the Code Migration Agent (demo steps 3-4).

Takes the units of migration_order in order and migrates each unit whose files
are all `pending` and whose dependencies are already migrated. Each unit is one
model session. Migrated files become `in_progress`.
"""
from __future__ import annotations

import os
import subprocess

from .. import loop, state
from ..cli import make_backend, say
from ..codemig import prompts
from ..codemig.rules_backend import RulesBackend
from ..codemig.tools import CodeMigrationTools, git_baseline

NAME = 'code_migration'


def add_args(ap):
    g = ap.add_argument_group('stage migrate (Code Migration)')
    g.add_argument('--max-units', type=int, default=0, help='stop after this many units (0 = all pending)')
    g.add_argument('--retry-blocked', action='store_true',
                   help='treat files blocked only by this agent as pending again (after editing decisions.md)')
    g.add_argument('--commit', action='store_true', help='git commit the migrated source files after each unit')


def pick_units(st: dict, only_file: str = None, retry_blocked: bool = False):
    """[(unit_no, files)] to migrate, plus notes on units skipped.

    A unit is migrated when all its files are pending and every file it depends
    on outside the unit is already migrated (not pending, not blocked).
    """
    by_path = {f['path']: f for f in st['files']}
    if retry_blocked:
        for f in st['files']:
            own = [b for b in f.get('blockers', []) if b.get('agent') == 'code_migration']
            if f.get('status') == 'blocked' and own and len(own) == len(f.get('blockers', [])):
                f['status'], f['blockers'] = 'pending', []
    todo, notes = [], []
    planned = set()
    for n, files in enumerate(st['migration_order'], 1):
        if only_file and only_file not in files:
            continue
        statuses = [by_path[p].get('status', 'pending') for p in files]
        deps = sorted({d for p in files for d in by_path[p].get('depends_on', []) if d not in files})
        waiting = [d for d in deps if d in by_path and d not in planned
                   and by_path[d].get('status', 'pending') in ('pending', 'blocked')]
        if all(s == 'pending' for s in statuses) and waiting:
            notes.append('unit %d %s waits for %s (%s)' % (n, files, ', '.join(waiting), ', '.join(
                by_path[d].get('status', 'pending') for d in waiting)))
        elif all(s == 'pending' for s in statuses):
            todo.append((n, files))
            planned.update(files)
        elif 'blocked' in statuses:
            notes.append('unit %d %s skipped: blocked (%s)' % (n, files, ', '.join(
                p for p, s in zip(files, statuses) if s == 'blocked')))
        elif only_file:
            notes.append('unit %d %s already migrated: %s' % (n, files, statuses))
    return todo, notes


def run(a, log_path: str) -> dict:
    st = state.load_state(a.repo)
    if not st:
        return {'finished': False, 'reason': 'no migration_state.json: run --stage map first'}
    todo, notes = pick_units(st, a.file, a.retry_blocked)
    if a.retry_blocked:
        state.write_state(a.repo, st)
    if a.max_units:
        todo = todo[:a.max_units]
    by_path = {f['path']: f for f in st['files']}
    for n, files in todo:
        dirty = [p for p in files if git_baseline(a.repo, p).get('dirty') and by_path[p].get('status') == 'pending']
        if dirty:
            return {'finished': False, 'reason': 'unit %d: %s changed since git HEAD but is still pending; commit '
                    'or restore it first' % (n, ', '.join(dirty))}
    say(a, 'units to migrate: %s' % ([f for _, f in todo] or 'none'))
    for note in notes:
        say(a, '  ' + note)
    catalogue = os.path.join(a.repo, 'rule_catalogue.md')
    units = []
    for n, files in todo:
        backend = make_backend(a, RulesBackend)
        tools = CodeMigrationTools(a.repo, a.py2, a.py3, n, files, backend.model, run_log=log_path,
                                   command_timeout=a.command_timeout)
        file_lines = '\n'.join('- %s (%s, %d lines; verification: %s)' % (
            p, tools.plans[p].entry.get('kind', '?'), len(tools.plans[p].lines),
            tools.plans[p].entry.get('verification_command', '?')) for p in files)
        user = prompts.USER.format(repo=a.repo, unit=n, position=n, total=len(st['migration_order']),
                                   files=', '.join(files), file_lines=file_lines,
                                   catalogue=catalogue if os.path.exists(catalogue) else 'not found (R1-R13)',
                                   n_decisions=len(tools.prior), first=files[0])
        say(a, '\n-- unit %d: %s' % (n, ', '.join(files)))
        res = loop.run(backend, tools, prompts.SYSTEM, user, a.max_steps or 40, log_path,
                       on_step=lambda s: say(a, s))
        res.update(unit=n, files=files, outputs=tools.outputs)
        if res['finished']:
            now = {f['path']: f['status'] for f in state.load_state(a.repo)['files']}
            res['statuses'] = {p: now[p] for p in files}
            say(a, '   statuses: %s' % res['statuses'])
            if a.commit:
                res['commit'] = commit(a.repo, n, files, res['statuses'])
        units.append(res)
        if not res['finished']:
            say(a, 'unit %d did not finish (%s); stopping before later units' % (n, res['reason']))
            break
    ok = all(u['finished'] for u in units)
    return {'finished': ok, 'reason': 'all units finished' if ok else 'a unit did not finish',
            'units': units, 'skipped': notes}


def commit(repo: str, unit: int, files, statuses) -> str:
    migrated = [p for p in files if statuses.get(p) == 'in_progress']
    if not migrated:
        return 'nothing to commit'
    st = {f['path']: f for f in state.load_state(repo)['files']}
    rules = sorted({r for p in migrated for r in st[p].get('rules', [])}, key=lambda r: int(r[1:]))
    msg = 'code_migration: unit %d (%s)\n\nRules: %s\n' % (unit, ', '.join(migrated), ', '.join(rules) or 'none')
    subprocess.run(['git', '-C', repo, 'add', '--'] + migrated, check=True)
    p = subprocess.run(['git', '-C', repo, 'commit', '-m', msg, '--'] + migrated, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr) else 'committed'
