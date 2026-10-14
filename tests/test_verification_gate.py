"""Tests for the Verification Gate.

Needs PY2_BIN and PY3_BIN. The fixture repo (tests/fixtures/py2mig) is first
migrated through the Code Migration tools, then verified. No model is needed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from migagent import loop, state  # noqa: E402
from migagent.codemig.tools import CodeMigrationTools  # noqa: E402
from migagent.verify import prompts  # noqa: E402
from migagent.verify.compare import same, same_case  # noqa: E402
from migagent.verify.rules_backend import RulesBackend  # noqa: E402
from migagent.verify.tools import Env, VerificationGateTools  # noqa: E402
from test_code_migration import PY2, PY3, make_repo, needs_py2  # noqa: E402

PICKS = {('renamed-from-import', 5): 'text', ('octal-literal', 9): 'octal', ('py2-dunder', 16): 'rename',
         ('div-dunder', 19): 'true', ('division', 24): 'floor', ('division', 28): 'true',
         ('basestring', 43): 'str', ('range-list', 53): 'list', ('exec-statement', 72): 'call',
         ('raise-comma', 81): 'call', ('module-attr', 97): 'py3', ('unicode', 105): 'str'}


def migrate(repo, picks=PICKS):
    t = CodeMigrationTools(repo, PY2, PY3, 1, ['mathx.py'], 'test')
    sites = list(t.plans['mathx.py'].sites.values())
    choices = [{'site_id': next(s['id'] for s in sites if (s['kind'], s['line']) == k), 'option': o, 'reason': 't'}
               for k, o in picks.items()]
    assert t.tool_choose('mathx.py', choices)['ok']
    assert t.tool_finish_unit('fixture')['ok']
    t2 = CodeMigrationTools(repo, PY2, PY3, 2, ['mathx_test.py'], 'test')
    sid = t2.plans['mathx_test.py'].unresolved()[0]
    t2.tool_choose('mathx_test.py', [{'site_id': sid, 'option': 'true', 'reason': 'Ratio.__truediv__'}])
    assert t2.tool_finish_unit('fixture tests')['ok']


class CompareTest(unittest.TestCase):
    def test_equivalence_rules(self):
        s2 = {'t': 'str2', 'hex': '6162'}
        self.assertTrue(same(s2, {'t': 'text', 'hex': '6162'})[0])         # Py2 str -> Py3 str
        self.assertTrue(same(s2, {'t': 'bytes', 'hex': '6162'})[0])        # Py2 str -> Py3 bytes
        self.assertFalse(same({'t': 'text', 'hex': '6162'}, {'t': 'bytes', 'hex': '6162'})[0])
        self.assertFalse(same({'t': 'list', 'v': []}, {'t': 'iter', 'v': []})[0])
        self.assertTrue(same({'t': 'exc', 'type': 'ValueError', 'msg': 'a'},
                             {'t': 'exc', 'type': 'ValueError', 'msg': 'b'})[0])
        self.assertFalse(same_case({'value': None, 'stdout': 'x\n'}, {'value': None, 'stdout': 'x \n'})[0])


class PickFilesTest(unittest.TestCase):
    def test_waits_until_everything_its_checks_run_is_migrated(self):
        from migagent.stages.verify import pick_files, runs_with
        mig = {'baseline_ref': 'x'}
        st = {'migration_order': [['pkg/__init__.py'], ['pkg/a.py'], ['tests/test_a.py']], 'files': [
            {'path': 'pkg/__init__.py', 'status': 'pending', 'depends_on': []},
            {'path': 'pkg/a.py', 'status': 'in_progress', 'migration': mig, 'depends_on': [],
             'verification_command': '{PY3} tests/test_a.py'},
            {'path': 'tests/test_a.py', 'status': 'in_progress', 'migration': mig, 'depends_on': ['pkg/a.py']}]}
        self.assertEqual(runs_with(st, 'pkg/a.py'), {'pkg/a.py', 'tests/test_a.py', 'pkg/__init__.py'})
        tmp = tempfile.mkdtemp()
        try:
            for f in st['files']:
                os.makedirs(os.path.join(tmp, os.path.dirname(f['path'])), exist_ok=True)
                open(os.path.join(tmp, f['path']), 'w').close()
            todo, notes = pick_files(tmp, st)
            self.assertEqual(todo, [])                                   # package __init__ still Python 2
            self.assertTrue(any('pkg/__init__.py' in n for n in notes))
            st['files'][0].update(status='in_progress', migration=mig)
            self.assertEqual(pick_files(tmp, st)[0], ['pkg/__init__.py', 'pkg/a.py', 'tests/test_a.py'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@needs_py2
class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gate(self, path, **kw):
        env = Env(self.repo, PY2, PY3, os.path.join(self.tmp, 'work-%s' % len(os.listdir(self.tmp))))
        return VerificationGateTools(env, path, 'test', **kw)

    def entry(self, path):
        return next(f for f in state.load_state(self.repo)['files'] if f['path'] == path)

    def test_correct_migration_passes(self):
        migrate(self.repo)
        g = self.gate('mathx.py', min_cases=3, min_model_cases=3)
        r = g.tool_add_cases([{'expr': 'mathx.half(-7)'}, {'expr': 'mathx.mean([1, 2, 4])'},
                              {'expr': 'mathx.describe({"x": 1})'}, {'expr': 'mathx.squares(2)'},
                              {'expr': 'mathx.kinds(2**64)'}, {'expr': 'mathx.show(1, 2)'}])
        self.assertEqual(r['added'], 6, r)
        res = g.tool_run_checks()
        self.assertEqual(res['checks']['tests']['same_outcome'], 6)
        self.assertEqual(res['verdict'], 'equivalent', res['differences'])
        self.assertTrue(g.tool_finish_unit('ok')['ok'])
        e = self.entry('mathx.py')
        self.assertEqual((e['status'], e['needs_repair']), ('passing', False))
        self.assertTrue(os.path.exists(os.path.join(self.repo, e['verification']['characterization_file'])))

    def test_wrong_division_is_caught_and_sent_to_repair(self):
        migrate(self.repo, {**PICKS, ('division', 24): 'true'})     # half(): / where Py2 floored
        g = self.gate('mathx.py', min_cases=1, min_model_cases=1)
        g.tool_add_cases([{'expr': 'mathx.half(7)'}])
        res = g.tool_run_checks()
        self.assertEqual(res['verdict'], 'not_equivalent')
        ids = [d.get('id') for d in res['differences']]
        self.assertIn('mathx_test.MathxTest.test_division', ids)
        char = next(d for d in res['differences'] if d['check'] == 'characterization')
        self.assertEqual((char['py2'], char['py3']), ('3', '3.5'))
        self.assertIn('error', g.tool_accept_divergence(['mathx_test.MathxTest.test_division'], 'no'))
        self.assertTrue(g.tool_finish_unit('broken')['ok'])
        e = self.entry('mathx.py')
        self.assertEqual((e['status'], e['needs_repair']), ('in_progress', True))
        self.assertTrue(any('test_division' in (f.get('id') or '') and f.get('trace') for f in e['verification']['failures']))

    def test_case_validation(self):
        migrate(self.repo)
        g = self.gate('mathx.py')
        r = g.tool_add_cases([{'expr': 'hash(mathx.half(2))'}, {'expr': 'print("x")'}, {'expr': 'f"{1}"'},
                              {'expr': 'mathx.half(2) if True'}, {'expr': 'os.getcwd()'}, {'expr': 'mathx.half(2)'}])
        self.assertEqual(r['added'], 1)
        whys = {x['expr']: x['why'] for x in r['rejected']}
        self.assertIn('hash', whys['hash(mathx.half(2))'])
        self.assertIn('Python 2', whys['print("x")'])        # an expression only in Python 3
        self.assertIn('Python 2', whys['f"{1}"'])
        self.assertEqual(len(whys), 5)

    def test_finish_needs_checks_and_own_cases(self):
        migrate(self.repo)
        g = self.gate('mathx.py', min_cases=2, min_model_cases=2)
        self.assertFalse(g.tool_finish_unit('x')['ok'])
        g.tool_add_cases([{'expr': 'mathx.half(9)'}])
        self.assertEqual(g.tool_run_checks()['verdict'], 'incomplete')
        r = g.tool_finish_unit('x')
        self.assertFalse(r['ok'])
        self.assertIn('2 of your own', r['missing'][0])

    def test_accepted_divergence_is_recorded_and_reused(self):
        migrate(self.repo)
        g = self.gate('mathx.py', min_cases=1, min_model_cases=1)
        g.tool_add_cases([{'expr': 'mathx.describe({1: 2, "a": 3})'}, {'expr': 'mathx.label(1)'}])
        res = g.tool_run_checks()
        diff = [d['id'] for d in res['differences'] if d['check'] == 'characterization']
        self.assertEqual(len(diff), 1, res)   # Py2 sorts int and str keys together, Py3 raises TypeError
        self.assertTrue(g.tool_accept_divergence(diff, 'interpreter difference, not the migration')['ok'])
        self.assertEqual(g.tool_run_checks()['checks']['characterization']['accepted'], len(diff))
        self.assertTrue(g.tool_finish_unit('accepted')['ok'])
        g2 = self.gate('mathx.py', min_cases=1, min_model_cases=1)
        res2 = g2.tool_run_checks()
        self.assertEqual(res2['checks']['characterization']['accepted'], len(diff))
        self.assertEqual(sum(1 for k in state.load_decisions(self.repo) if k.startswith('divergence:')), len(diff))

    def test_chained_verification_command(self):
        migrate(self.repo)
        st = state.load_state(self.repo)
        f = next(x for x in st['files'] if x['path'] == 'mathx.py')
        f['verification_command'] = '{PY3} mathx_test.py && {PY3} -c "import mathx"'
        state.write_state(self.repo, st)
        g = self.gate('mathx.py', min_cases=1, min_model_cases=1)
        self.assertEqual((g.test_files, g.commands), (['mathx_test.py'], [['{PY3}', '-c', 'import mathx']]))
        g.tool_add_cases([{'expr': 'mathx.half(9)'}])
        res = g.tool_run_checks()
        self.assertEqual(res['checks']['tests']['same_outcome'], 6)
        self.assertEqual(res['checks']['command']['verdict'], 'equivalent')
        self.assertEqual(res['verdict'], 'equivalent', res['differences'])

    def test_rules_backend_end_to_end(self):
        migrate(self.repo)
        env = Env(self.repo, PY2, PY3, os.path.join(self.tmp, 'w'))
        for path in ('mathx.py', 'mathx_test.py'):
            tools = VerificationGateTools(env, path, 'rules', min_cases=1, min_model_cases=0)
            res = loop.run(RulesBackend(), tools, prompts.SYSTEM, 'verify %s' % path, 10,
                           os.path.join(self.tmp, 't.jsonl'), on_step=lambda s: None)
            self.assertTrue(res['finished'], res)
        self.assertEqual(self.entry('mathx_test.py')['status'], 'passing')
        self.assertIn(self.entry('mathx.py')['verification']['verdict'], ('equivalent', 'not_equivalent'))


if __name__ == '__main__':
    unittest.main()
