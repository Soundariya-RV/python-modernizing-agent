"""Tests for the Cross-File Consistency agent. Needs PY2_BIN and PY3_BIN; no model."""
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
from migagent.consistency import prompts  # noqa: E402
from migagent.consistency.rules_backend import RulesBackend  # noqa: E402
from migagent.consistency.tools import ConsistencyTools  # noqa: E402
from test_code_migration import PY2, PY3, needs_py2  # noqa: E402

# path: (Python 2 baseline, migrated Python 3 or None if still pending, kind, status)
FILES = {
    'lib.py': ('def area(w, h):\n    return w * h\n\n\ndef old():\n    return 1\n',
               'def area(w, h, unit=None):\n    return w * h\n', 'library', 'passing'),
    'cfg_a.py': ('import ConfigParser\nP = ConfigParser.RawConfigParser\n',
                 'import configparser\nP = configparser.RawConfigParser\n', 'library', 'passing'),
    'cfg_b.py': ('import ConfigParser\nimport lib\nQ = ConfigParser.RawConfigParser\nX = lib.old()\n',
                 None, 'library', 'pending'),
    'ring_a.py': ('import ring_b\n', 'import ring_b\n', 'library', 'passing'),
    'ring_b.py': ('import ring_a\n', None, 'library', 'pending'),
    'broken.py': ('x = 1\n', 'x = (\n', 'library', 'in_progress'),
}


def make_repo(tmp):
    repo = os.path.join(tmp, 'repo')
    os.makedirs(os.path.join(repo, '.migration', 'baseline'))
    files, changes = [], {'cfg_a.py': [{'rule': 'R11', 'before': 'ConfigParser', 'after': 'configparser'}]}
    for path, (py2, py3, kind, status) in FILES.items():
        with open(os.path.join(repo, path), 'w') as f:
            f.write(py3 if py3 is not None else py2)
        e = {'path': path, 'kind': kind, 'status': status, 'module': path[:-3], 'rules': [], 'decisions': [],
             'changes': changes.get(path, [])}
        if py3 is not None:
            with open(os.path.join(repo, '.migration', 'baseline', path), 'w') as f:
                f.write(py2)
            e['migration'] = {'baseline_copy': '.migration/baseline/' + path, 'unit': 1 if 'ring' not in path else 4}
        files.append(e)
    order = [['lib.py'], ['cfg_a.py'], ['cfg_b.py'], ['ring_a.py', 'ring_b.py'], ['broken.py']]
    state.write_state(repo, {'schema_version': 1, 'repo': 'repo', 'files': files, 'migration_order': order,
                             'blockers': [], 'agents': {}})
    return repo


@needs_py2
class ConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_check_finds_its_problem(self):
        t = ConsistencyTools(self.repo, PY2, PY3, 'test')
        r = t.tool_run_checks()
        by = {(f['check'], f['severity']): f for f in t.findings}
        self.assertIn('cfg_b.py', by[('R16', 'violation')]['detail'])              # mixed rename
        self.assertIn('ring_a.py', by[('R29', 'violation')]['subject'])           # cycle split
        self.assertEqual(by[('imports', 'violation')]['path'], 'broken.py')       # migrated, does not import
        r31 = [f for f in t.findings if f['check'] == 'R31' and f['severity'] == 'needs_judgement']
        self.assertEqual(sorted(f['subject'] for f in r31), ['area', 'old'])
        old = next(f for f in r31 if f['subject'] == 'old')
        self.assertTrue(any('cfg_b.py' in x for x in old['evidence']['references_in_other_files']))
        self.assertEqual(sorted(r['needs_judgement']), sorted(f['id'] for f in r31))

    def test_finish_requires_judgement_and_marks_files(self):
        t = ConsistencyTools(self.repo, PY2, PY3, 'test')
        t.tool_run_checks()
        self.assertFalse(t.tool_finish('x')['ok'])
        r31 = {f['subject']: f['id'] for f in t.findings if f['severity'] == 'needs_judgement'}
        self.assertIn('error', t.tool_resolve(t.findings[0]['id'] if t.findings[0]['severity'] != 'needs_judgement'
                                              else 'F999', 'accept', 'x'))
        t.tool_resolve(r31['area'], 'accept', 'new parameter has a default: old calls still work')
        t.tool_resolve(r31['old'], 'violation', 'cfg_b.py calls lib.old()')
        res = t.tool_finish('checked')
        self.assertEqual(res['verdict'], 'inconsistent')
        st = state.load_state(self.repo)
        by = {f['path']: f for f in st['files']}
        self.assertEqual((by['lib.py']['status'], by['lib.py']['needs_repair']), ('in_progress', True))
        self.assertEqual(by['cfg_a.py']['status'], 'passing')       # R16: the file on the old name is flagged, not this one
        self.assertEqual(by['cfg_b.py']['status'], 'pending')                      # pending files are left alone
        self.assertEqual(st['consistency']['verdict'], 'inconsistent')
        dec = state.load_decisions(self.repo)
        self.assertEqual(dec['api:lib.py:area']['option'], 'accept')
        t2 = ConsistencyTools(self.repo, PY2, PY3, 'test')                         # decisions are reused
        t2.tool_run_checks()
        self.assertEqual([f for f in t2.findings if f['severity'] == 'needs_judgement'], [])

    def test_rules_backend_end_to_end(self):
        t = ConsistencyTools(self.repo, PY2, PY3, 'rules')
        res = loop.run(RulesBackend(), t, prompts.SYSTEM, 'check', 20, os.path.join(self.tmp, 't.jsonl'),
                       on_step=lambda s: None)
        self.assertTrue(res['finished'], res)
        self.assertIn('Cross-File Consistency Agent', state.read_text(os.path.join(self.repo, 'progress.md')))


if __name__ == '__main__':
    unittest.main()
