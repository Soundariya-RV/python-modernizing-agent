"""Tests for the init stage and for running on any repository (git URL or plain folder)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from migagent import cli, state  # noqa: E402
from migagent.stages import init  # noqa: E402
from test_code_migration import PY2, PY3, needs_py2  # noqa: E402

FIXTURE = os.path.join(HERE, 'fixtures', 'py2mig')


def args(repo):
    return types.SimpleNamespace(repo=repo, py2=PY2, py3=PY3, exclude=[], command_timeout=120, quiet=True,
                                 reinit=False)


@needs_py2
class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, 'mathlib')
        shutil.copytree(FIXTURE, self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_agent_md_from_commands_it_ran(self):
        a = args(self.repo)
        self.assertTrue(init.needed(a))
        res = init.run(a, '')
        self.assertTrue(res['finished'])
        md = state.read_text(os.path.join(self.repo, 'Agent.md'))
        self.assertIn('"$PY2_BIN" mathx_test.py\n', md)                     # passes on the baseline
        self.assertIn('"$PY3_BIN" mathx_test.py   # EXPECTED TO FAIL', md)    # fails under Python 3
        self.assertTrue(os.path.exists(os.path.join(self.repo, 'rule_catalogue.md')))
        self.assertFalse(init.needed(a))

    def test_baseline_failure_is_not_marked_expected(self):
        p = os.path.join(self.repo, 'mathx_test.py')
        src = state.read_text(p).replace("if __name__ == '__main__':",
                                         'class Broken(unittest.TestCase):\n    def test_no(self):\n'
                                         '        self.fail("x")\n\n\nif __name__ == \'__main__\':')
        with open(p, 'w') as f:
            f.write(src)
        init.run(args(self.repo), '')
        md = state.read_text(os.path.join(self.repo, 'Agent.md'))
        line = next(l for l in md.splitlines() if l.startswith('"$PY2_BIN" mathx_test.py'))
        self.assertIn('FAILS on the Python 2 baseline', line)
        self.assertNotIn('EXPECTED', line)

    def test_git_url_is_cloned_at_a_ref(self):
        src = os.path.join(self.tmp, 'src')
        shutil.copytree(FIXTURE, src)
        g = lambda *a: subprocess.run(['git', '-C', src] + list(a), check=True, capture_output=True)  # noqa: E731
        g('init', '-q')
        g('-c', 'user.email=t@t', '-c', 'user.name=t', 'add', '.')
        g('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'py2')
        g('tag', 'py2-baseline')
        with open(os.path.join(src, 'later.py'), 'w') as f:
            f.write('x = 1\n')
        g('add', 'later.py')
        g('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'later')
        repo = cli.resolve_repo('file://' + src, 'py2-baseline', os.path.join(self.tmp, 'repos'))
        self.assertEqual(repo, os.path.join(self.tmp, 'repos', 'src'))
        self.assertTrue(os.path.exists(os.path.join(repo, 'mathx.py')))
        self.assertFalse(os.path.exists(os.path.join(repo, 'later.py')))       # checked out at the tag
        self.assertEqual(cli.resolve_repo('file://' + src, None, os.path.join(self.tmp, 'repos')), repo)  # reused


if __name__ == '__main__':
    unittest.main()
