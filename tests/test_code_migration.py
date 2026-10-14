"""Tests for the Code Migration Agent.

Needs PY2_BIN (CPython 2.7) and PY3_BIN; skipped otherwise. No model is
needed: choices are made through the tools directly or by the rules backend.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from migagent import loop, state  # noqa: E402
from migagent.codemig import prompts  # noqa: E402
from migagent.codemig.rewrite import Conflict, Edit, render  # noqa: E402
from migagent.codemig.rules_backend import RulesBackend  # noqa: E402
from migagent.codemig.tools import CodeMigrationTools, run_py2_json  # noqa: E402

FIXTURE = os.path.join(HERE, 'fixtures', 'py2mig')
PY2, PY3 = os.environ.get('PY2_BIN'), os.environ.get('PY3_BIN') or sys.executable
needs_py2 = unittest.skipUnless(PY2, 'set PY2_BIN to a CPython 2.7 interpreter')


def sites_of(path):
    return run_py2_json(PY2, 'py2_sites.py', [path])['sites']


def make_repo(tmp):
    repo = os.path.join(tmp, 'py2mig')
    shutil.copytree(FIXTURE, repo)
    files = [
        {'path': 'mathx.py', 'status': 'pending', 'unit': 1, 'kind': 'library',
         'verification_command': '{PY3} mathx_test.py', 'rules': [], 'blockers': [], 'decisions': []},
        {'path': 'mathx_test.py', 'status': 'pending', 'unit': 2, 'kind': 'test',
         'verification_command': '{PY3} mathx_test.py', 'rules': [], 'blockers': [], 'decisions': []}]
    state.write_state(repo, {'schema_version': 1, 'repo': 'py2mig', 'files': files,
                             'migration_order': [['mathx.py'], ['mathx_test.py']], 'blockers': [],
                             'agents': {}})
    return repo


class RenderTest(unittest.TestCase):
    def test_nested_edits_are_kept(self):
        src = b'print x, 10L\n'
        outer = Edit('A', 0, 12, ['print(', [6, 12], ')'])
        inner = Edit('B', 11, 12, [''])
        self.assertEqual(render(src, [outer, inner]), b'print(x, 10)\n')

    def test_lost_and_overlapping_edits_conflict(self):
        src = b'abcdef'
        with self.assertRaises(Conflict):             # B is inside A but outside A's slices
            render(src, [Edit('A', 0, 6, ['X']), Edit('B', 2, 3, ['y'])])
        with self.assertRaises(Conflict):
            render(src, [Edit('A', 0, 4, ['X']), Edit('B', 2, 6, ['y'])])


@needs_py2
class FinderTest(unittest.TestCase):
    def test_every_rule_found(self):
        sites = sites_of(os.path.join(FIXTURE, 'mathx.py'))
        rules = {s['rule'] for s in sites}
        self.assertEqual(rules, {'R%d' % i for i in range(1, 14)} | {'OUT'})
        by_kind = {}
        for s in sites:
            by_kind.setdefault(s['kind'], []).append(s)
        texts = {}
        for s in sites:
            if s['options']:
                texts.setdefault(s['kind'], s['options'][0]['text'])
        self.assertEqual(texts['print'], "print('show:', end=' ', file=sys.stderr)")
        self.assertEqual(texts['long-in-tuple'], 'int')
        self.assertEqual(texts['raise-comma'], "raise ValueError('bad')")
        self.assertEqual(texts['exec-statement'], 'exec(code, ns)')
        self.assertEqual([o['text'] for s in by_kind['has_key'] for o in s['options']],
                         ["'x' in d", "'y' not in d"])
        self.assertTrue(all(s['judgement'] and s['default'] is None for s in by_kind['division']))
        self.assertFalse(any(s['judgement'] for s in by_kind['xrange'] + by_kind['has_key']))

    def test_residual_scan(self):
        res = run_py2_json(PY2, 'py2_sites.py', [os.path.join(FIXTURE, 'mathx.py'), '--residual'])['residual']
        self.assertTrue({'R1', 'R2', 'R3', 'R4', 'R8', 'R9', 'R10', 'R11', 'R12'} <= {r['rule'] for r in res})


@needs_py2
class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tools(self, unit=1, files=('mathx.py',)):
        return CodeMigrationTools(self.repo, PY2, PY3, unit, list(files), 'test-model')

    @staticmethod
    def site(tools, kind, line=None, path='mathx.py'):
        return next(s for s in tools.plans[path].sites.values()
                    if s['kind'] == kind and (line is None or s['line'] == line))

    def test_finish_refuses_until_every_site_is_chosen(self):
        t = self.tools()
        r = t.tool_finish_unit('done')
        self.assertFalse(r['ok'])
        self.assertIn('choose for sites', r['missing'][0])
        with open(os.path.join(self.repo, 'mathx.py'), 'rb') as f:
            self.assertIn(b'xrange', f.read(), 'nothing may be written before finish_unit succeeds')

    def test_choose_validates(self):
        t = self.tools()
        sid = self.site(t, 'division', 24)['id']
        r = t.tool_choose('mathx.py', [{'site_id': sid, 'option': 'nope', 'reason': 'x'},
                                       {'site_id': sid, 'option': 'floor', 'reason': ''},
                                       {'site_id': 'S999', 'option': 'keep', 'reason': 'x'}])
        self.assertFalse(r['ok'])
        self.assertEqual(len(r['errors']), 3)
        self.assertIn('error', json.loads(t.call('list_sites', {'path': 'other.py'})))

    def test_probe_division_evidence(self):
        t = self.tools()
        r = t.tool_probe_division('mathx.py')
        by_line = {s['line']: s for s in r['sites']}
        self.assertEqual(by_line[24]['suggested'], 'floor')      # half(): int / int, truncated
        self.assertEqual(by_line[28]['suggested'], 'true')       # mean(): int / float
        self.assertIn('truncated', by_line[24]['evidence'])

    def migrate_correctly(self, t):
        t.tool_probe_division('mathx.py')
        picks = {('renamed-from-import', 5): 'text', ('octal-literal', 9): 'octal',
                 ('py2-dunder', 16): 'rename', ('div-dunder', 19): 'true', ('division', 24): 'floor',
                 ('division', 28): 'true', ('basestring', 43): 'str', ('range-list', 53): 'list',
                 ('exec-statement', 72): 'call', ('raise-comma', 81): 'call', ('module-attr', 97): 'py3',
                 ('unicode', 105): 'str'}
        choices = [{'site_id': self.site(t, k, l)['id'], 'option': o, 'reason': 'test'} for (k, l), o in picks.items()]
        r = t.tool_choose('mathx.py', choices)
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['still_need_choice'], [])
        pv = t.tool_preview('mathx.py')
        self.assertTrue(pv['py3_compile']['ok'], pv)
        self.assertEqual(pv['py2_constructs_left'], [])
        return t.tool_finish_unit('Fixture migrated with the right choices.')

    def test_correct_choices_pass_the_fixture_tests_under_py3(self):
        r = self.migrate_correctly(self.tools())
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['statuses'], {'mathx.py': 'in_progress'})
        t2 = self.tools(2, ['mathx_test.py'])
        ev = t2.tool_probe_division('mathx_test.py')['sites'][0]
        self.assertIn('Ratio/Ratio', ev['evidence'])
        self.assertIsNone(ev['suggested'])          # not numbers: the model has to judge
        t2.tool_choose('mathx_test.py', [{'site_id': ev['site_id'], 'option': 'true',
                                          'reason': 'Ratio defines __div__, migrated to __truediv__'}])
        self.assertTrue(t2.tool_finish_unit('test file: one division on Ratio objects')['ok'])
        p = subprocess.run([PY3, 'mathx_test.py'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])

        st = {f['path']: f for f in state.load_state(self.repo)['files']}
        f = st['mathx.py']
        self.assertEqual(f['rules'], ['R%d' % i for i in range(1, 14)])
        self.assertTrue(f['migration']['baseline_copy'].startswith('.migration/baseline/'))
        self.assertTrue(os.path.exists(os.path.join(self.repo, f['migration']['baseline_copy'])))
        self.assertTrue(all(c['rule'] for c in f['changes']))
        dec = state.load_decisions(self.repo)
        self.assertEqual(dec['OUT:raise-comma']['option'], 'call')
        progress = state.read_text(os.path.join(self.repo, 'progress.md'))
        self.assertIn('Code Migration Agent (unit 1)', progress)

    def test_pattern_decisions_are_reused_in_the_next_file(self):
        self.assertTrue(self.migrate_correctly(self.tools())['ok'])
        with open(os.path.join(self.repo, 'mathx_test.py'), 'a') as f:
            f.write('\n\ndef boom():\n    raise KeyError, "k"\n')
        t2 = self.tools(2, ['mathx_test.py'])
        s = self.site(t2, 'raise-comma', path='mathx_test.py')
        c = t2.plans['mathx_test.py'].choice[s['id']]
        self.assertEqual((c['how'], c['option']), ('reused', 'call'))
        self.assertEqual(t2.plans['mathx_test.py'].unresolved(), [self.site(t2, 'division', path='mathx_test.py')['id']])
        self.assertEqual(len([k for k in state.load_decisions(self.repo) if k == 'OUT:raise-comma']), 1)

    def test_block_leaves_the_file_untouched(self):
        t = self.tools()
        with open(os.path.join(self.repo, 'mathx.py'), 'rb') as f:
            before = f.read()
        choices = [{'site_id': sid, 'option': 'block' if s['kind'] == 'division' else 'keep',
                    'reason': 'operand types unknown' if s['kind'] == 'division' else 'test'}
                   for sid, s in t.plans['mathx.py'].sites.items() if sid in t.plans['mathx.py'].unresolved()]
        t.tool_choose('mathx.py', choices)
        r = t.tool_finish_unit('blocked on division')
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['statuses'], {'mathx.py': 'blocked'})
        with open(os.path.join(self.repo, 'mathx.py'), 'rb') as f:
            self.assertEqual(f.read(), before)
        f = {x['path']: x for x in state.load_state(self.repo)['files']}['mathx.py']
        self.assertTrue(any('division' in b['description'] for b in f['blockers']))
        self.assertEqual((f['rules'], f['changes']), ([], []))      # nothing applied, so nothing credited
        self.assertTrue(f['planned_changes'])

    def test_edit_lines_requires_a_citation(self):
        t = self.tools()
        self.assertIn('error', t.tool_edit_lines('mathx.py', 9, 9, 'PERM = 493', 'because', 'x'))
        r = t.tool_edit_lines('mathx.py', 9, 9, 'PERM = 0o755', 'R8', 'octal is outside the catalogue')
        self.assertTrue(r['ok'])
        self.assertEqual(len(r['covers_sites']), 1)


class PickUnitsTest(unittest.TestCase):
    def test_dependents_wait_for_blocked_files_and_retry(self):
        from migagent.stages import migrate as rcm
        st = {'migration_order': [['a.py'], ['b.py'], ['c.py']], 'files': [
            {'path': 'a.py', 'status': 'blocked', 'depends_on': [],
             'blockers': [{'agent': 'code_migration', 'subject': 'x'}]},
            {'path': 'b.py', 'status': 'pending', 'depends_on': ['a.py']},
            {'path': 'c.py', 'status': 'pending', 'depends_on': []}]}
        todo, notes = rcm.pick_units(json.loads(json.dumps(st)))
        self.assertEqual(todo, [(3, ['c.py'])])
        self.assertTrue(any('waits for a.py' in n for n in notes))
        todo, _ = rcm.pick_units(json.loads(json.dumps(st)), retry_blocked=True)
        self.assertEqual(todo, [(1, ['a.py']), (2, ['b.py']), (3, ['c.py'])])
        st['files'][0]['blockers'].append({'agent': 'dependency_mapper'})   # a human's blocker stays
        todo, _ = rcm.pick_units(json.loads(json.dumps(st)), retry_blocked=True)
        self.assertEqual(todo, [(3, ['c.py'])])


@needs_py2
class EndToEndRulesBackendTest(unittest.TestCase):
    def test_rules_backend_runs_both_units(self):
        tmp = tempfile.mkdtemp()
        try:
            repo = make_repo(tmp)
            log = os.path.join(tmp, 'trajectory.jsonl')
            for unit, files in ((1, ['mathx.py']), (2, ['mathx_test.py'])):
                tools = CodeMigrationTools(repo, PY2, PY3, unit, files, 'rules', run_log=log)
                user = prompts.USER.format(repo=repo, unit=unit, position=unit, total=2, files=files[0],
                                           file_lines='- %s (x, 1 lines; verification: v)' % files[0],
                                           catalogue='R1-R13', n_decisions=0, first=files[0])
                res = loop.run(RulesBackend(), tools, prompts.SYSTEM, user, 40, log, on_step=lambda s: None)
                self.assertTrue(res['finished'], res)
            st = {f['path']: f['status'] for f in state.load_state(repo)['files']}
            self.assertEqual(st, {'mathx.py': 'in_progress', 'mathx_test.py': 'in_progress'})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
