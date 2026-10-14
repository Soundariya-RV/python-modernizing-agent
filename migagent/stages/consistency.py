"""Stage `consistency`: the Cross-File Consistency agent.

R16 renames all-or-nothing, R29 import cycles together, every module imports
under Python 3, R31 public API unchanged (the model judges each API change).
"""
from __future__ import annotations

from .. import loop, state
from ..cli import make_backend, say
from ..consistency import prompts
from ..consistency.rules_backend import RulesBackend
from ..consistency.tools import ConsistencyTools

NAME = 'cross_file_consistency'


def add_args(ap):
    pass


def run(a, log_path: str) -> dict:
    st = state.load_state(a.repo)
    if not st:
        return {'finished': False, 'reason': 'no migration_state.json: run --stage map first'}
    backend = make_backend(a, RulesBackend)
    tools = ConsistencyTools(a.repo, a.py2, a.py3, backend.model, run_log=log_path)
    counts = {}
    for f in st['files']:
        counts[f.get('status', 'pending')] = counts.get(f.get('status', 'pending'), 0) + 1
    user = prompts.USER.format(repo=a.repo, n_files=len(st['files']),
                               statuses=', '.join('%d %s' % (n, s) for s, n in sorted(counts.items())),
                               n_units=len(st['migration_order']),
                               n_cycles=sum(1 for u in st['migration_order'] if len(u) > 1))
    res = loop.run(backend, tools, prompts.SYSTEM, user, a.max_steps or 30, log_path, on_step=lambda s: say(a, s))
    res['outputs'] = tools.outputs
    if res['finished']:
        res['verdict'] = tools.verdict
        say(a, '   verdict: %s' % tools.verdict)
    return res
