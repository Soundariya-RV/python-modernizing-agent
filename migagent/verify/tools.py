"""Tools the Verification Gate (Qwen 32B) calls for one migrated file.

Split of responsibility:
  facts     every check runs under both interpreters and is compared here:
            the repo's own tests (per-test outcome, testrun.py), the file's
            verification command (exit codes), and characterization cases
            (charrun.py: Py2 baseline result = required Py3 result).
  judgement which characterization inputs exercise the file (cases are
            validated: one expression, valid in Py2 and Py3, deterministic),
            whether a characterization difference comes from the interpreter
            and is accepted (recorded in decisions.md), and the summary.

Verdicts: equivalent -> status `passing`; not_equivalent -> stays
`in_progress` with needs_repair and the failure log for the Repair agent;
unverifiable (the Python 2 baseline itself cannot run the check) -> `blocked`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from typing import Dict, List, Optional

from .. import state, trees
from ..loop import Toolbox
from .compare import same_case, show

HERE = os.path.dirname(os.path.abspath(__file__))
CHAR_DIR = os.path.join('.migration', 'characterization')
MAX_CASES = 150
BANNED_CALLS = re.compile(r'\b(hash|id|open|file|input|raw_input|exec|eval|execfile|compile|__import__|globals|'
                          r'locals|vars|exit|quit|reload|help|dir)\s*\(')
BANNED_MODULES = re.compile(r'\b(os|sys|subprocess|random|time|datetime|socket|shutil|tempfile|threading|'
                            r'urllib\d?|httplib|requests)\s*\.')
VERIFY_HINTS = {   # from rule_catalogue.md "Verify"
    'R1': 'calls that print: printed output is compared byte for byte (spacing, trailing newline)',
    'R2': 'loops over ranges: empty ranges, negative steps',
    'R3': 'dict iteration (results are compared sorted), has_key on present and missing keys',
    'R4': 'drive the exception path: the exception type is compared',
    'R5': 'input handling',
    'R6': 'text and bytes: non-ASCII text, packed bytes (compared as hex)',
    'R7': 'call with text, bytes and a non-string',
    'R8': 'integers on both sides of 2**31, 2**32, 2**63 and 2**64; hex() and repr() of results',
    'R9': 'repr strings, including of large integers',
    'R10': 'equal and unequal operands',
    'R11': 'code paths that use the renamed module',
    'R12': 'namespace contents after exec',
    'R13': 'division with negative operands, odd/even pairs, large ints, and the type of the result',
}
SEED_ONE = ['0', '1', '-1', '7', '-7', '255', '2**31', '2**32', '2**63', '2**64', "''", "'a'", "'1'", 'None',
            '1.5', '[]', '(1, 2)']
SEED_TWO = ['7, 2', '-7, 2', '0, 1', "'a', 'b'", '2**64, 3', '1, None']


def _worst(verdicts: List[str]) -> str:
    for v in ('unverifiable', 'not_equivalent', 'not_run'):
        if v in verdicts:
            return v
    return 'equivalent'


def _combine_tests(results: List[dict]) -> dict:
    if len(results) == 1:
        return results[0]
    out = {'verdict': _worst([r['verdict'] for r in results]),
           'test_file': ', '.join(r['test_file'] for r in results), 'per_file': results}
    for k in ('tests', 'same_outcome', 'py2_pass', 'py3_pass'):
        out[k] = sum(r.get(k, 0) for r in results)
    return out


def _combine_commands(results: List[dict]) -> dict:
    if len(results) == 1:
        return results[0]
    bad = [r for r in results if r['verdict'] != 'equivalent']
    first = bad[0] if bad else results[0]
    return {'verdict': _worst([r['verdict'] for r in results]), 'command': ' && '.join(r['command'] for r in results),
            'py2_exit': first['py2_exit'], 'py3_exit': first['py3_exit'], 'per_command': results}


def _fn(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': desc,
        'parameters': {'type': 'object', 'properties': props or {}, 'required': required or []}}}


class Env:
    """Trees and interpreters shared by every file of one gate run, with a test-run cache."""

    def __init__(self, repo: str, py2: str, py3: str, workdir: str, timeout: int = 600):
        self.repo, self.py2, self.py3, self.timeout = repo, py2, py3, timeout
        self.workdir = workdir
        self.state = state.load_state(repo)
        self.base = trees.baseline_tree(repo, self.state, os.path.join(workdir, 'py2-baseline'))
        self.mig = trees.copy_repo(repo, os.path.join(workdir, 'py3-migrated'))
        self.tmp = os.path.join(workdir, 'tmp')
        os.makedirs(self.tmp, exist_ok=True)
        self.cache: Dict[tuple, dict] = {}

    def env(self) -> dict:
        return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp'),
                'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1',
                'TMPDIR': self.tmp}

    def py(self, which: str) -> List[str]:
        return [self.py2, '-E', '-s'] if which == 'py2' else [self.py3, '-s']

    def tree(self, which: str) -> str:
        return self.base if which == 'py2' else self.mig

    def run_json(self, which: str, script: str, args: List[str], key: Optional[tuple] = None) -> dict:
        if key and key in self.cache:
            return self.cache[key]
        out = os.path.join(self.tmp, '%s-%s.json' % (which, hashlib.sha1(repr((script, args)).encode()).hexdigest()[:10]))
        try:
            p = subprocess.run(self.py(which) + [os.path.join(HERE, script), self.tree(which)] + args + [out],
                               cwd=self.tree(which), capture_output=True, text=True, timeout=self.timeout,
                               env=self.env())
            with open(out, encoding='utf-8') as f:
                res = json.load(f)
        except subprocess.TimeoutExpired:
            res = {'harness_error': 'timed out after %ds' % self.timeout}
        except (OSError, ValueError) as e:
            res = {'harness_error': '%s: %s' % (type(e).__name__, (p.stderr or '')[-1500:] if 'p' in locals() else e)}
        if key:
            self.cache[key] = res
        return res

    def run_command(self, which: str, args: List[str]) -> dict:
        key = ('cmd', which, tuple(args))
        if key in self.cache:
            return self.cache[key]
        cmd = [self.py2 if a == '{PY3}' or a == '{PY2}' else a for a in args] if which == 'py2' else \
              [self.py3 if a in ('{PY3}', '{PY2}') else a for a in args]
        cmd = [a.replace('{TMP}', os.path.join(self.tmp, which)) for a in cmd]
        try:
            p = subprocess.run(cmd, cwd=self.tree(which), capture_output=True, text=True, timeout=self.timeout,
                               env=self.env())
            res = {'exit': p.returncode, 'tail': (p.stdout + p.stderr)[-1500:]}
        except subprocess.TimeoutExpired:
            res = {'exit': None, 'tail': 'timed out after %ds' % self.timeout}
        self.cache[key] = res
        return res


class VerificationGateTools(Toolbox):
    AGENT = 'verification_gate'

    def __init__(self, env: Env, path: str, model: str, run_log: str = '', min_cases: int = 10,
                 min_model_cases: int = 5):
        self.env = env
        self.repo = env.repo
        self.path = path
        self.model = model
        self.run_log = run_log
        self.min_cases, self.min_model_cases = min_cases, min_model_cases
        st = state.load_state(self.repo)
        self.entry = next(f for f in st['files'] if f['path'] == path)
        self.prior = state.load_decisions(self.repo)
        self.module = self.entry.get('module') or os.path.splitext(path)[0].replace('/', '.')
        self.char_needed = self.entry.get('kind') == 'library' or bool(self.entry.get('needs_characterization_tests'))
        self.char_note = None
        self.char_file = os.path.join(self.repo, CHAR_DIR, path + '.json')
        self.cases: List[dict] = []
        self.setup: List[str] = []
        if os.path.exists(self.char_file):
            with open(self.char_file, encoding='utf-8') as f:
                saved = json.load(f)
            self.cases, self.setup = saved.get('cases', []), saved.get('setup', [])
        self.results: Optional[dict] = None
        self.diffs: Dict[str, dict] = {}            # case id -> last observed difference
        self.accepted: Dict[str, dict] = {}         # case id -> {reason, decision}
        self.finished = False
        self.outputs: Dict[str, str] = {}
        cmd = self.entry.get('verification_command') or ''
        # a verification command can chain several runs with &&: each test file is compared test by
        # test, anything else by exit code
        self.parts = [shlex.split(x) for x in cmd.split('&&') if x.strip()]
        self.cmd = self.parts[0] if self.parts else []
        self.test_files: List[str] = []
        self.commands: List[List[str]] = []
        kinds = {f['path']: f.get('kind') for f in st['files']}
        for part in self.parts:
            if len(part) >= 2 and part[0] in ('{PY3}', '{PY2}') and kinds.get(part[1]) == 'test':
                self.test_files.append(part[1])
            else:
                self.commands.append(part)
        self.test_file = ', '.join(self.test_files) or None
        self._api = None
        if self.char_needed and not self.api():
            self.char_needed = False      # nothing public to call: the tests or the command are the evidence
            self.char_note = 'no public API to characterize (no public function, class or constant)'

    # ------------------------------------------------------------- schemas
    def schemas(self):
        return [
            _fn('list_checks', 'What will be checked for this file, its public API, the catalogue rules that '
                'were applied (with what to exercise for each), and the characterization cases so far.'),
            _fn('show_lines', 'Show lines of the file, Python 2 baseline version (default) or migrated, at most 60.',
                {'start': {'type': 'integer'}, 'end': {'type': 'integer'},
                 'version': {'type': 'string', 'enum': ['baseline', 'migrated']}}, ['start', 'end']),
            _fn('seed_cases', 'Add simple generated cases: every public function, class and constant called '
                'with a fixed pool of values. Cheap coverage; add meaningful cases of your own as well.'),
            _fn('add_cases', 'Add characterization cases. Each expr is ONE Python expression valid in both '
                'Python 2 and 3, using the module by its import name. setup lines (optional) run once before '
                'the cases, e.g. "net = mod.Network(\'10.0.0.0/8\')". Invalid or non-deterministic cases '
                'are rejected with the reason.',
                {'cases': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'expr': {'type': 'string'}, 'note': {'type': 'string'}}, 'required': ['expr']}},
                 'setup': {'type': 'array', 'items': {'type': 'string'}}}, ['cases']),
            _fn('run_checks', 'Run every check under the Python 2 baseline and the migrated Python 3 code, and '
                'compare. Returns the verdict per check and each difference.'),
            _fn('accept_divergence', 'Accept characterization differences that come from the interpreter, not '
                'from the migration (for example something Python 2 cannot do at all). Recorded in '
                'decisions.md. Test-suite and command differences cannot be accepted.',
                {'case_ids': {'type': 'array', 'items': {'type': 'string'}}, 'reason': {'type': 'string'}},
                ['case_ids', 'reason']),
            _fn('finish_unit', 'Record the verdict in migration_state.json, decisions.md and progress.md. '
                'Fails with what is missing if checks have not run on the current cases.',
                {'summary': {'type': 'string', 'description': '2-5 sentences for progress.md'}}, ['summary']),
        ]

    # ------------------------------------------------------------- tools
    def api(self) -> List[dict]:
        if self._api is None:
            p = subprocess.run([self.env.py2, '-E', '-s', os.path.join(HERE, 'py2_api.py'),
                                os.path.join(self.env.base, self.path)], capture_output=True, text=True)
            self._api = json.loads(p.stdout)['api'] if p.returncode == 0 else []
        return self._api

    def tool_list_checks(self):
        checks = []
        if self.test_files:
            checks.append('tests: run %s under Py2 (baseline) and Py3 (migrated); every test must have the '
                          'same outcome' % self.test_file)
        for c in self.commands:
            checks.append('command: %s under both; Py3 must succeed where Py2 succeeds' % ' '.join(c))
        if self.char_note:
            checks.append('characterization: not required, %s' % self.char_note)
        if self.char_needed:
            checks.append('characterization: at least %d cases (%d of your own) on module %s; the Py2 result '
                          'of each case is the required Py3 result' % (self.min_cases, self.min_model_cases,
                                                                        self.module))
        rules = self.entry.get('rules', [])
        out = {'path': self.path, 'kind': self.entry.get('kind'), 'module': self.module, 'checks': checks,
               'rules_applied': rules,
               'verify_hints': {r: VERIFY_HINTS[r] for r in rules if r in VERIFY_HINTS},
               'decisions': self.entry.get('decisions', []),
               'cases_so_far': len(self.cases),
               'model_cases_so_far': sum(1 for c in self.cases if c.get('author') == 'model')}
        if self.char_needed:
            api = self.api()
            out['api'] = ['%s %s(%s)%s' % (a['kind'], a['name'], ', '.join(a['params']),
                                           ' - ' + a['doc'] if a['doc'] else '') for a in api[:80]]
            if len(api) > 80:
                out['api_more'] = len(api) - 80
            out['setup_so_far'] = self.setup
        return out

    def tool_show_lines(self, start: int, end: int, version: str = 'baseline'):
        tree = self.env.base if version != 'migrated' else self.env.mig
        with open(os.path.join(tree, self.path), encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
        start, end = max(1, int(start)), min(len(lines), int(end))
        end = min(end, start + 59)
        return {'version': version, 'text': '\n'.join('%d: %s' % (n, lines[n - 1]) for n in range(start, end + 1))}

    def tool_seed_cases(self):
        if not self.char_needed:
            return {'note': 'characterization is not required for this file'}
        exprs = []
        m = self.module
        for a in self.api():
            if a['kind'] == 'constant':
                exprs.append('%s.%s' % (m, a['name']))
            elif a['kind'] in ('function', 'class') and not any(p.startswith('*') for p in a['params']):
                n = a['required']
                if n == 0:
                    exprs.append('%s.%s()' % (m, a['name']))
                elif n == 1:
                    exprs += ['%s.%s(%s)' % (m, a['name'], v) for v in SEED_ONE]
                elif n == 2:
                    exprs += ['%s.%s(%s)' % (m, a['name'], v) for v in SEED_TWO]
        return self._add([{'expr': e, 'note': 'seed'} for e in exprs[:80]], [], 'seed')

    def tool_add_cases(self, cases: List[dict], setup: Optional[List[str]] = None):
        if not self.char_needed:
            return {'error': 'characterization is not required for this file'}
        if isinstance(cases, dict):
            cases = [cases]
        cases = [c if isinstance(c, dict) else {'expr': str(c)} for c in (cases or [])]
        return self._add(cases, list(setup or []), 'model')

    def _add(self, cases: List[dict], setup: List[str], author: str):
        rejected, ok = [], []
        known = {c['expr'] for c in self.cases}
        cand = []
        for c in cases:
            e = (c.get('expr') or '').strip()
            why = self._banned(e)
            if not e:
                continue
            if e in known:
                rejected.append({'expr': e, 'why': 'duplicate'})
            elif why:
                rejected.append({'expr': e, 'why': why})
            else:
                cand.append((e, c.get('note', '')))
                known.add(e)
        new_setup = [s for s in setup if s.strip() and s not in self.setup]
        bad_setup = [s for s in new_setup if self._banned(s)]
        if bad_setup:
            return {'error': 'setup lines rejected: %s' % [(s, self._banned(s)) for s in bad_setup]}
        errs = self._compile_both([e for e, _ in cand], new_setup)
        if errs.get('setup'):
            return {'error': 'setup does not compile: %s' % errs['setup']}
        for e, note in cand:
            if e in errs['cases']:
                rejected.append({'expr': e, 'why': errs['cases'][e]})
            elif len(self.cases) >= MAX_CASES:
                rejected.append({'expr': e, 'why': 'limit of %d cases reached' % MAX_CASES})
            else:
                self.cases.append({'id': 'C%03d' % (len(self.cases) + 1), 'expr': e, 'note': note, 'author': author})
                ok.append(self.cases[-1]['id'])
        self.setup += new_setup
        self.results = None
        return {'added': len(ok), 'rejected': rejected[:20], 'total_cases': len(self.cases),
                'model_cases': sum(1 for c in self.cases if c.get('author') == 'model')}

    @staticmethod
    def _banned(e: str) -> Optional[str]:
        if len(e) > 300:
            return 'longer than 300 characters'
        if BANNED_CALLS.search(e):
            return 'calls %s(): not deterministic or has side effects' % BANNED_CALLS.search(e).group(1)
        if BANNED_MODULES.search(e):
            return 'uses module %s: not deterministic or has side effects' % BANNED_MODULES.search(e).group(1)
        if 'lambda' in e and 'print' in e:
            return 'print inside lambda is not valid in both versions'
        return None

    def _compile_both(self, exprs: List[str], setup: List[str]) -> dict:
        code = ('import json, sys\nd = json.load(sys.stdin)\nout = {"cases": {}, "setup": None}\n'
                'for e in d["cases"]:\n'
                '    try:\n        compile(e, "<case>", "eval")\n'
                '    except SyntaxError as x:\n        out["cases"][e] = "Python %d: %s" % (sys.version_info[0], x.msg)\n'
                'for s in d["setup"]:\n'
                '    try:\n        compile(s, "<setup>", "exec")\n'
                '    except SyntaxError as x:\n        out["setup"] = "Python %d: %s: %s" % (sys.version_info[0], s, x.msg)\n'
                'print(json.dumps(out))\n')
        merged = {'cases': {}, 'setup': None}
        for py in (self.env.py2, self.env.py3):
            p = subprocess.run([py, '-c', code], input=json.dumps({'cases': exprs, 'setup': setup}),
                               capture_output=True, text=True)
            r = json.loads(p.stdout) if p.returncode == 0 else {'cases': {}, 'setup': p.stderr[-300:]}
            for e, why in r['cases'].items():
                merged['cases'].setdefault(e, why)
            merged['setup'] = merged['setup'] or r['setup']
        return merged

    def tool_run_checks(self):
        res = {'checks': {}}
        failures = []
        verdicts = []
        tests, commands = [], []
        for tf in self.test_files:
            c = self._check_tests(tf, failures)
            (commands if 'command' in c else tests).append(c)
        for cmd in self.commands:
            commands.append(self._check_command(cmd, failures))
        if tests:
            res['checks']['tests'] = _combine_tests(tests)
            verdicts.append(res['checks']['tests']['verdict'])
        if commands:
            res['checks']['command'] = _combine_commands(commands)
            verdicts.append(res['checks']['command']['verdict'])
        if self.char_needed:
            c = self._check_characterization(failures)
            res['checks']['characterization'] = c
            verdicts.append(c['verdict'])
        if 'unverifiable' in verdicts:
            verdict = 'unverifiable'
        elif 'not_equivalent' in verdicts:
            verdict = 'not_equivalent'
        elif 'not_run' in verdicts:
            verdict = 'incomplete'
        elif verdicts:
            verdict = 'equivalent'
        else:
            verdict = 'unverifiable'
            failures.append({'check': 'none', 'why': 'no verification command and no characterization'})
        res['verdict'] = verdict
        res['failures'] = failures
        self.results = res
        view = {'verdict': verdict, 'checks': res['checks'], 'differences': failures[:12]}
        if len(failures) > 12:
            view['more_differences'] = len(failures) - 12
        return view

    def _check_tests(self, test_file: str, failures) -> dict:
        r2 = self.env.run_json('py2', 'testrun.py', [test_file], ('tests', 'py2', test_file))
        r3 = self.env.run_json('py3', 'testrun.py', [test_file], ('tests', 'py3', test_file))
        if r2.get('harness_error') or r2.get('import_error') or not r2.get('n'):
            if r2.get('n') == 0 and not r2.get('import_error') and not r2.get('harness_error'):
                return self._check_command(['{PY3}', test_file], failures)   # not unittest: compare the run
            failures.append({'check': 'tests', 'why': 'the Python 2 baseline cannot run %s' % test_file,
                             'py2': (r2.get('import_error') or r2.get('harness_error') or '')[-800:]})
            return {'verdict': 'unverifiable', 'test_file': test_file, 'tests': 0, 'same_outcome': 0,
                    'py2_pass': 0, 'py3_pass': 0}
        if r3.get('import_error') or r3.get('harness_error'):
            failures.append({'check': 'tests', 'why': '%s does not import under Python 3' % test_file,
                             'py3': (r3.get('import_error') or r3.get('harness_error'))[-1200:]})
            return {'verdict': 'not_equivalent', 'test_file': test_file, 'tests': r2['n'], 'same_outcome': 0,
                    'py2_pass': sum(1 for t in r2['tests'].values() if t['outcome'] == 'pass'), 'py3_pass': 0}
        t2, t3 = r2['tests'], r3['tests']
        same_n = 0
        for tid in sorted(t2):
            a, b = t2[tid], t3.get(tid)
            if b and a['outcome'] == b['outcome'] and a.get('exc') == b.get('exc'):
                same_n += 1
                continue
            failures.append({'check': 'tests', 'id': tid, 'py2': a['outcome'],
                             'py3': b['outcome'] if b else 'missing',
                             'why': (b or {}).get('msg', 'test not found under Python 3')[:200],
                             'trace': (b or {}).get('trace', '')[-1200:]})
        py2_pass = sum(1 for t in t2.values() if t['outcome'] == 'pass')
        py3_pass = sum(1 for t in t3.values() if t['outcome'] == 'pass')
        return {'verdict': 'equivalent' if same_n == len(t2) else 'not_equivalent', 'test_file': test_file,
                'tests': len(t2), 'same_outcome': same_n, 'py2_pass': py2_pass, 'py3_pass': py3_pass,
                'extra_py3_tests': sorted(set(t3) - set(t2))[:5]}

    def _check_command(self, cmd: List[str], failures) -> dict:
        r2 = self.env.run_command('py2', cmd)
        r3 = self.env.run_command('py3', cmd)
        c = {'command': ' '.join(cmd), 'py2_exit': r2['exit'], 'py3_exit': r3['exit']}
        if r2['exit'] != 0:
            failures.append({'check': 'command', 'why': 'fails on the Python 2 baseline too (exit %s)' % r2['exit'],
                             'py2': r2['tail'][-800:]})
            c['verdict'] = 'unverifiable'
        elif r3['exit'] != 0:
            failures.append({'check': 'command', 'why': 'succeeds under Python 2, exits %s under Python 3' % r3['exit'],
                             'py3': r3['tail'][-1200:]})
            c['verdict'] = 'not_equivalent'
        else:
            c['verdict'] = 'equivalent'
        return c

    def _check_characterization(self, failures) -> dict:
        n_model = sum(1 for c in self.cases if c.get('author') == 'model')
        if not self.cases:
            return {'verdict': 'not_run', 'cases': 0, 'note': 'no cases yet'}
        spec_path = os.path.join(self.env.tmp, 'cases-%s.json' % hashlib.sha1(self.path.encode()).hexdigest()[:8])
        with open(spec_path, 'w', encoding='utf-8') as f:
            json.dump({'module': self.module, 'setup': self.setup, 'cases': self.cases}, f)
        r2 = self.env.run_json('py2', 'charrun.py', [spec_path])
        r3 = self.env.run_json('py3', 'charrun.py', [spec_path])
        if r2.get('harness_error') or r2.get('setup_error'):
            failures.append({'check': 'characterization', 'why': 'cases cannot run on the Python 2 baseline: %s'
                             % (r2.get('setup_error') or r2.get('harness_error'))})
            return {'verdict': 'unverifiable', 'cases': len(self.cases)}
        if r3.get('harness_error') or r3.get('setup_error'):
            failures.append({'check': 'characterization', 'why': 'module or setup fails under Python 3: %s'
                             % (r3.get('setup_error') or r3.get('harness_error'))})
            return {'verdict': 'not_equivalent', 'cases': len(self.cases), 'matched': 0}
        self.py2_recording = r2
        matched, mismatched, accepted = 0, 0, 0
        for c in self.cases:
            a, b = r2['results'][c['id']], r3['results'][c['id']]
            ok, why = same_case(a, b)
            if ok:
                matched += 1
                continue
            self.diffs[c['id']] = {'py2': show(a['value']), 'py3': show(b['value']), 'why': why}
            prior = self.prior.get(self._div_key(c))
            if c['id'] not in self.accepted and prior and prior.get('option', '').strip('`') == 'accept':
                self.accepted[c['id']] = {'reason': prior.get('reason', ''), 'decision': prior['id'], 'how': 'reused'}
            if c['id'] in self.accepted:
                accepted += 1
                continue
            mismatched += 1
            failures.append({'check': 'characterization', 'id': c['id'], 'expr': c['expr'],
                             'py2': show(a['value']) + (' printed %r' % a['stdout'] if a.get('stdout') else ''),
                             'py3': show(b['value']) + (' printed %r' % b['stdout'] if b.get('stdout') else ''),
                             'why': why})
        enough = len(self.cases) >= self.min_cases and n_model >= self.min_model_cases
        verdict = 'not_equivalent' if mismatched else ('equivalent' if enough else 'not_run')
        res = {'verdict': verdict, 'cases': len(self.cases), 'model_cases': n_model, 'matched': matched,
               'accepted': accepted, 'mismatched': mismatched}
        if not enough:
            res['note'] = 'need at least %d cases, %d of your own' % (self.min_cases, self.min_model_cases)
        return res

    def _div_key(self, case: dict) -> str:
        return 'divergence:%s:%s' % (self.path, case['expr'])

    def tool_accept_divergence(self, case_ids: List[str], reason: str):
        if not self.results:
            return {'error': 'run_checks first'}
        if not reason.strip():
            return {'error': 'reason is required'}
        if isinstance(case_ids, str):
            case_ids = [case_ids]
        open_ids = {f['id'] for f in self.results['failures'] if f['check'] == 'characterization' and f.get('id')}
        bad = [c for c in case_ids if c not in open_ids]
        if bad:
            return {'error': 'not characterization differences from the last run_checks: %s (tests and '
                             'commands cannot be accepted)' % bad}
        for c in case_ids:
            self.accepted[c] = {'reason': reason.strip(), 'decision': None, 'how': 'model'}
        self.results = None
        return {'ok': True, 'accepted': case_ids, 'next': 'run_checks again'}

    def tool_finish_unit(self, summary: str):
        missing = []
        if self.results is None:
            missing.append('run_checks on the current cases')
        else:
            ch = self.results['checks'].get('characterization')
            if ch and ch['verdict'] == 'not_run':
                missing.append('characterization: %s' % ch.get('note', 'add cases and run_checks'))
        if not summary.strip():
            missing.append('summary is empty')
        if missing:
            return {'ok': False, 'missing': missing}
        self._write(summary.strip())
        self.finished = True
        return {'ok': True, 'verdict': self.results['verdict'], 'status': self.status, 'written': self.outputs}

    # ------------------------------------------------------------- writing
    def _write(self, summary: str):
        today = state.now()
        verdict = self.results['verdict']
        self.status = {'equivalent': 'passing', 'not_equivalent': 'in_progress', 'unverifiable': 'blocked'}[verdict]
        new_dec = []
        next_id = state.next_decision_id(self.repo)
        for cid, a in sorted(self.accepted.items()):
            if a['how'] != 'model':
                continue
            case = next(c for c in self.cases if c['id'] == cid)
            fail = self.diffs.get(cid, {})
            d = {'id': 'D-%03d' % next_id, 'title': 'accepted divergence: %s' % case['expr'][:60],
                 'key': self._div_key(case), 'kind': 'divergence', 'action': 'accept', 'option': 'accept',
                 'where': self.path, 'context': 'characterization case `%s`' % case['expr'],
                 'decision': 'accept the Python 3 behaviour for this case', 'reason': a['reason'],
                 'agent': self.AGENT, 'model': self.model, 'date': today}
            d['context'] += fail and ' (Py2 %s, Py3 %s)' % (fail.get('py2'), fail.get('py3')) or ''
            a['decision'] = d['id']
            next_id += 1
            new_dec.append(d)
        os.makedirs(os.path.dirname(self.char_file), exist_ok=True)
        if self.char_needed and self.cases:
            with open(self.char_file, 'w', encoding='utf-8', newline='\n') as f:
                json.dump({'module': self.module, 'setup': self.setup, 'cases': self.cases,
                           'py2_baseline': getattr(self, 'py2_recording', {}).get('results', {}),
                           'recorded_at': today}, f, indent=1)
        st = state.load_state(self.repo)
        entry = next(f for f in st['files'] if f['path'] == self.path)
        with open(os.path.join(self.repo, self.path), 'rb') as f:
            current = hashlib.sha256(f.read()).hexdigest()
        ver = {'agent': self.AGENT, 'at': today, 'verdict': verdict, 'model': self.model,
               'checks': self.results['checks'], 'failures': self.results['failures'][:30],
               'accepted_divergences': [{'case': c, 'decision': a['decision'], 'reason': a['reason']}
                                        for c, a in sorted(self.accepted.items())],
               'migrated_sha256': current, 'baseline_ref': (entry.get('migration') or {}).get('baseline_ref'),
               'interpreters': {'py2': self.env.py2, 'py3': self.env.py3}}
        if self.char_note:
            ver['characterization_note'] = self.char_note
        if self.char_needed and self.cases:
            ver['characterization_file'] = os.path.relpath(self.char_file, self.repo).replace(os.sep, '/')
        entry['verification'] = ver
        entry['last_verified'] = today
        entry['status'] = self.status
        entry['needs_repair'] = verdict == 'not_equivalent'
        dec = list(entry.get('decisions', []))
        for d in new_dec:
            if d['id'] not in dec:
                dec.append(d['id'])
        for a in self.accepted.values():
            if a.get('decision') and a['decision'] not in dec:
                dec.append(a['decision'])
        entry['decisions'] = dec
        blockers = [b for b in entry.get('blockers', []) if b.get('agent') != self.AGENT]
        if verdict == 'unverifiable':
            blockers.append({'subject': 'unverifiable:%s' % self.path, 'agent': self.AGENT, 'raised_at': today,
                             'description': '; '.join(f['why'] for f in self.results['failures'])[:500]})
        entry['blockers'] = blockers
        entry.setdefault('history', []).append({'at': today, 'agent': self.AGENT, 'event': 'verified',
                                                'verdict': verdict, 'status': self.status})
        st['updated_at'] = today
        st['agents'] = dict(st.get('agents', {}), **{self.AGENT: {'last_run': today, 'model': self.model,
                                                                 'run_log': self.run_log}})
        self.outputs['migration_state'] = state.write_state(self.repo, st)
        dp = state.append_decisions(self.repo, new_dec)
        if dp:
            self.outputs['decisions'] = dp
        self.outputs['progress'] = state.append_progress(self.repo, self._progress_md(today, ver, new_dec, summary))

    def _progress_md(self, today, ver, new_dec, summary) -> str:
        lines = ['## %s - Verification Gate: `%s` -> %s (%s)' % (today[:16].replace('T', ' '), self.path,
                                                                 self.status, ver['verdict']), '',
                 '- **Model:** %s' % self.model]
        if self.run_log:
            lines.append('- **Run log:** `%s`' % self.run_log)
        lines += ['', '| Check | Result |', '|---|---|']
        for name, c in ver['checks'].items():
            if name == 'tests' and 'tests' in c:
                txt = '%d/%d tests same outcome (Py2 pass %d, Py3 pass %d), `%s`' % (
                    c['same_outcome'], c['tests'], c['py2_pass'], c['py3_pass'], c.get('test_file'))
            elif name == 'characterization' and 'matched' in c:
                txt = '%d/%d cases match, %d accepted, %d differ (%d written by the model)' % (
                    c['matched'], c['cases'], c.get('accepted', 0), c.get('mismatched', 0), c.get('model_cases', 0))
            elif 'py2_exit' in c:
                txt = '`%s`: Py2 exit %s, Py3 exit %s' % (c['command'], c['py2_exit'], c['py3_exit'])
            else:
                txt = json.dumps(c)[:200]
            lines.append('| %s | %s: %s |' % (name, c['verdict'], txt.replace('|', '\\|')))
        if ver['failures']:
            lines += ['', 'Differences (for the Repair agent):']
            for f in ver['failures'][:10]:
                what = f.get('id') or f.get('check')
                lines.append('- `%s` %s: %s%s' % (what, f['check'], f.get('why', ''),
                                                 (' (Py2 `%s`, Py3 `%s`)' % (f.get('py2'), f.get('py3'))
                                                  if f['check'] == 'characterization' else '')))
        if ver['accepted_divergences']:
            lines.append('- **Accepted divergences:** %s' % ', '.join(
                '%s (%s)' % (a['case'], a['decision']) for a in ver['accepted_divergences']))
        lines += ['', '**Model summary** (not checked by the tools; the facts are above): ' + summary]
        return '\n'.join(lines)
