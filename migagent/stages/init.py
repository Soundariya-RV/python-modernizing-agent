"""Stage `init`: make any Python 2 repository ready for the agents.

Runs when the repository has no Agent.md (or with --reinit). It inspects the
repo with the Python 2 scanner, writes an Agent.md with environment, build and
test commands, and copies the rule catalogue if the repo has none. Every command
is run under Python 2 (the baseline) and its Python 3 counterpart before it is
written down, and the result is recorded next to it:

  - a Python 3 line that fails on the baseline is marked `EXPECTED TO FAIL`
    (that is the starting point of a migration);
  - a Python 2 line that fails on the baseline is written as failing, without
    the EXPECTED marker, so the Dependency Mapper records it as a blocker.

Facts only: no model is involved, so the commands the mapper verifies next are
the ones this stage actually ran.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from typing import List

from .. import state
from ..cli import say

NAME = 'init'
AGENT_MD_NAMES = ('Agent.md', 'agent.md', 'AGENT.md', 'agents.md', 'AGENTS.md')
DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
SCAN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'depmap', 'py2_scan.py')
COMPILE = ('import sys; [compile(open(f, "rb").read(), f, "exec") for f in sys.argv[1:]]')


def add_args(ap):
    g = ap.add_argument_group('stage init (Agent.md for a repo that has none)')
    g.add_argument('--reinit', action='store_true', help='write Agent.md again although one exists')


def agent_md_path(repo: str):
    return next((os.path.join(repo, n) for n in AGENT_MD_NAMES if os.path.exists(os.path.join(repo, n))), None)


def needed(a) -> bool:
    return a.reinit or agent_md_path(a.repo) is None


def _run(line: str, repo: str, a) -> dict:
    env = dict(os.environ, PY2_BIN=a.py2, PY3_BIN=a.py3)
    try:
        p = subprocess.run(['bash', '-c', line], cwd=repo, env=env, capture_output=True, text=True,
                           timeout=a.command_timeout)
        out = (p.stdout + p.stderr).strip().splitlines()
        return {'exit': p.returncode, 'last': out[-1][:160] if out else ''}
    except subprocess.TimeoutExpired:
        return {'exit': None, 'last': 'timed out after %ds' % a.command_timeout}


def _readme(repo: str) -> str:
    for p in sorted(glob.glob(os.path.join(repo, 'README*'))):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                paras = [x.strip() for x in f.read().split('\n\n')]
        except OSError:
            continue
        for x in paras:
            if x and not x.startswith(('#', '=', '-', '[', '!', '.. ', '<')) and len(x) > 40:
                return ' '.join(x.split())[:700]
    return ''


def _is_test(rec: dict) -> bool:
    base = os.path.basename(rec['path'])
    return bool(rec.get('testcase_classes')) or base.startswith('test_') or base.endswith('_test.py') \
        or base == 'tests.py'


def _test_line(rec: dict, py: str) -> str:
    if rec.get('has_main'):
        return '"%s" %s' % (py, rec['path'])
    d, base = os.path.split(rec['path'])
    return '"%s" -m unittest discover -s %s -p %s' % (py, d or '.', base)


def run(a, log_path: str) -> dict:
    repo = a.repo
    name = os.path.basename(repo)
    cmd = [a.py2, '-E', '-s', SCAN, repo]
    for e in a.exclude:
        cmd += ['--exclude', e]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return {'finished': False, 'reason': 'the Python 2 scan failed: %s' % p.stderr[-500:]}
    scan = json.loads(p.stdout)
    recs = scan['files'] if isinstance(scan['files'], list) else list(scan['files'].values())
    py_files = sorted(r['path'] for r in recs)
    tests = [r for r in recs if _is_test(r)]
    say(a, '%d Python files, %d test files' % (len(py_files), len(tests)))
    results: List[dict] = []

    def block(title: str, intro: str, pairs) -> List[str]:
        """pairs: (py2 line, py3 line or None). Runs both and annotates them."""
        out = ['## %s' % title, '', intro, '', '```bash']
        for l2, l3 in pairs:
            r2 = _run(l2.replace('"$PY2_BIN"', '"%s"' % a.py2), repo, a)
            note = '' if r2['exit'] == 0 else '   # FAILS on the Python 2 baseline (exit %s): %s' % (
                r2['exit'], r2['last'][:90])
            out.append(l2 + note)
            results.append({'line': l2, 'python': 2, **r2})
            if l3:
                r3 = _run(l3.replace('"$PY3_BIN"', '"%s"' % a.py3), repo, a)
                note3 = '' if r3['exit'] == 0 else '   # EXPECTED TO FAIL on the Python 2 baseline (exit %s): %s' % (
                    r3['exit'], r3['last'][:80])
                out.append(l3 + note3)
                results.append({'line': l3, 'python': 3, **r3})
        out += ['```', '']
        return out

    files_arg = ' '.join(py_files) if len(py_files) <= 150 else None
    compile_line = ('"$PY2_BIN" -c \'%s\' %s' % (COMPILE, files_arg) if files_arg else
                    'find . -name "*.py" -not -path "./.git/*" -print0 | xargs -0 "$PY2_BIN" -c \'%s\'' % COMPILE)
    md = ['# Agent.md: %s, Python 2.7 -> 3 migration' % name, '',
          'Generated by python-modernizing-agent (stage init) on %s. Every command below was run under' % state.now()[:10],
          'Python 2.7 (the baseline) and Python 3 before it was written down; a failure is noted on',
          'its line. Edit this file freely: the Dependency Mapper re-verifies every command.', '',
          '## 1. Project', '', _readme(repo) or '(no README found)', '',
          '**Goal.** Migrate this repository from Python 2.7 to Python 3 with every change proven by',
          'execution: the same tests and characterization inputs run under both interpreters and the',
          'outputs must match.', '',
          '## 2. Repository layout', '']
    for r in recs:
        kind = 'test' if _is_test(r) else 'package' if r.get('is_package_init') else \
            'packaging' if os.path.basename(r['path']) == 'setup.py' else 'module'
        md.append('- `%s` (%s, %d lines)' % (r['path'], kind, r.get('lines', 0)))
    md += ['', '']
    md += block('3. Environment', 'The two interpreters (set PY2_BIN and PY3_BIN).',
                [('"$PY2_BIN" -V', '"$PY3_BIN" -V')])
    build = [(compile_line, compile_line.replace('$PY2_BIN', '$PY3_BIN'))]
    if os.path.exists(os.path.join(repo, 'setup.py')):
        bb = '"${TMPDIR:-/tmp}/%s-build"' % name
        build.append(('"$PY2_BIN" setup.py build --build-base %s-py2' % bb,
                      '"$PY3_BIN" setup.py build --build-base %s-py3' % bb))
    md += block('4. Build', 'Every file compiles' + ('; setup.py packages it (build output outside the repo).'
                                                    if len(build) > 1 else '.'), build)
    if tests:
        md += block('5. Test', 'The repository\'s own tests. Python 2 is the required behaviour.',
                    [(_test_line(r, '$PY2_BIN'), _test_line(r, '$PY3_BIN')) for r in tests])
    else:
        md += ['## 5. Test', '', 'No test suite found. The Verification Gate writes characterization cases for',
               'every library module instead: what the Python 2 version returns for a range of inputs is the',
               'required behaviour of the Python 3 version.', '']
    md += ['## 6. Agent working files', '',
           '| File | Purpose |', '|---|---|',
           '| `Agent.md` | this entry point |',
           '| `rule_catalogue.md` | the closed set of accepted conversions (R1-R13, R16, R29, R31) |',
           '| `migration_state.json` | one row per file: status, order, rules, verification (written by the agents) |',
           '| `progress.md` | human-readable log of what the agents did |',
           '| `decisions.md` | ambiguous cases, the decision and why, reused for the same pattern |', '']
    path = agent_md_path(repo) or os.path.join(repo, 'Agent.md')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(md))
    copied = False
    if not os.path.exists(os.path.join(repo, 'rule_catalogue.md')):
        shutil.copyfile(os.path.join(DATA, 'rule_catalogue.md'), os.path.join(repo, 'rule_catalogue.md'))
        copied = True
    fails2 = [r for r in results if r['python'] == 2 and r['exit'] != 0]
    say(a, 'wrote %s (%d commands, %d fail on the Python 2 baseline)%s' % (
        path, len(results), len(fails2), '; copied rule_catalogue.md' if copied else ''))
    state.append_progress(repo, '\n'.join([
        '## %s - init: Agent.md generated' % state.now()[:16].replace('T', ' '), '',
        '- **Files:** %d Python files, %d test files' % (len(py_files), len(tests)),
        '- **Commands run:** %d (%d fail on the Python 2 baseline)' % (len(results), len(fails2)),
        '- **Rule catalogue:** %s' % ('copied the default' if copied else 'the repository\'s own'),
        ] + ['- **Fails on the baseline:** `%s` (exit %s)' % (r['line'], r['exit']) for r in fails2]))
    return {'finished': True, 'reason': 'Agent.md written', 'agent_md': path, 'commands': results,
            'catalogue_copied': copied}
