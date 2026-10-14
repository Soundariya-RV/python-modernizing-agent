"""Tests for the Dependency Mapping Agent.

Needs PY2_BIN and PY3_BIN (real CPython 2.7 and 3.x); tests that need them are
skipped otherwise. No model is needed: the agent loop is exercised with the
rules backend and with a local fake OpenAI-compatible server.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from migagent import agent_md, loop, state  # noqa: E402
from migagent.depmap import prompts  # noqa: E402
from migagent.depmap.analyze import analyze  # noqa: E402
from migagent.depmap.graph import migration_units, strongly_connected  # noqa: E402
from migagent.depmap.rules_backend import RulesBackend  # noqa: E402
from migagent.depmap.tools import DependencyMapperTools  # noqa: E402
from migagent.llm import OpenAICompatBackend  # noqa: E402

FIXTURE = os.path.join(HERE, 'fixtures', 'py2repo')
PY2, PY3 = os.environ.get('PY2_BIN'), os.environ.get('PY3_BIN') or sys.executable
needs_py2 = unittest.skipUnless(PY2, 'set PY2_BIN to a CPython 2.7 interpreter')


class GraphTest(unittest.TestCase):
    def test_scc_groups_cycles(self):
        e = {'a': {'b'}, 'b': {'c'}, 'c': {'a'}, 'd': {'a'}}
        comps = sorted(strongly_connected('abcd', e))
        self.assertEqual(comps, [['a', 'b', 'c'], ['d']])

    def test_order_dependencies_first_with_tie_break(self):
        e = {'app': {'lib'}, 'test': {'app'}, 'lib': set(), 'other': set()}
        units = migration_units(e, e, lambda u: (u[0] == 'test', u))
        flat = [u[0] for u in units]
        self.assertLess(flat.index('lib'), flat.index('app'))
        self.assertLess(flat.index('app'), flat.index('test'))

    def test_deep_chain_no_recursion_error(self):
        n = 5000
        e = {str(i): {str(i + 1)} for i in range(n)}
        e[str(n)] = set()
        units = migration_units(e, e, lambda u: u)
        self.assertEqual(units[0], [str(n)])


class AgentMdTest(unittest.TestCase):
    def setUp(self):
        self.text = state.read_text(os.path.join(FIXTURE, 'Agent.md'))

    def test_bash_host(self):
        blocks = agent_md.parse(self.text, 'bash')
        self.assertEqual([b.lang for b in blocks], ['bash', 'powershell', 'bash'])
        setup, ps, test = blocks
        self.assertEqual(setup.run_lines, ['"$PY2_BIN" -V'])
        self.assertEqual({r for _, r in setup.dropped},
                         {'environment setup', 'sets PY2_BIN/PY3_BIN; the tool injects these'})
        self.assertFalse(ps.runnable_here)
        self.assertEqual(len(test.run_lines), 2)
        reasons = [r for _, r in test.dropped]
        self.assertEqual(reasons, ['contains a <placeholder>', 'follows a dropped <placeholder> line'])

    def test_powershell_host(self):
        blocks = agent_md.parse(self.text, 'powershell')
        self.assertEqual([b.runnable_here for b in blocks], [False, True, False])

    @needs_py2
    def test_run_reports_every_line(self):
        shell = agent_md.host_shell()
        if shell == 'powershell':
            code = '& $env:PY2_BIN -c "import sys; sys.exit(3)"\n& $env:PY3_BIN -c "print(42)"'
        else:
            code = '"$PY2_BIN" -c "import sys; sys.exit(3)"\n"$PY3_BIN" -c "print(42)"'
        b = agent_md._classify(agent_md.Block('B1', 's', shell, code), shell)
        agent_md.run_block(b, FIXTURE, {'PY2_BIN': PY2, 'PY3_BIN': PY3}, 120)
        self.assertEqual(b.exit_code, 3)
        self.assertEqual([x['exit'] for x in b.line_exits], [3, 0])
        self.assertIn('42', b.output_tail)


@needs_py2
class AnalyzeFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = analyze(FIXTURE, PY2, PY3, ['tools'])
        cls.files = {f['path']: f for f in cls.a['files']}
        cls.issues = {i['key']: i for i in cls.a['issues']}

    def test_excluded_dir(self):
        self.assertNotIn('tools/helper.py', self.files)

    def test_cycle_is_one_unit(self):
        cycles = [u['files'] for u in self.a['units'] if u['cycle']]
        self.assertEqual(cycles, [['shop/__init__.py', 'shop/models.py', 'shop/pricing.py']])
        self.assertIn('import_cycle:shop/__init__.py <-> shop/models.py <-> shop/pricing.py', self.issues)

    def test_order_respects_dependencies(self):
        pos = {p: i for i, u in enumerate(self.a['units']) for p in u['files']}
        for p, f in self.files.items():
            for d in f['depends_on']:
                self.assertLessEqual(pos[d], pos[p], '%s must come after %s' % (p, d))
        self.assertLess(pos['shop/util.py'], pos['shop/models.py'])
        self.assertLess(pos['shop/models.py'], pos['tests/test_shop.py'])

    def test_outside_importer_depends_on_package_init(self):
        self.assertIn('shop/__init__.py', self.files['tests/test_shop.py']['depends_on'])
        self.assertNotIn('shop/__init__.py', self.files['shop/util.py']['depends_on'])

    def test_implicit_relative_imports(self):
        self.assertIn('implicit_relative_import:shop/models.py -> shop/pricing.py', self.issues)
        self.assertIn('implicit_relative_import:shop/__init__.py -> shop/models.py', self.issues)

    def test_external_classification(self):
        cls = {e['module']: e['class'] for e in self.a['external_modules']}
        self.assertEqual(cls['urllib2'], 'renamed_stdlib')
        self.assertEqual(cls['cStringIO'], 'renamed_stdlib')
        self.assertEqual(cls['urlparse'], 'py2_only_stdlib')
        self.assertEqual(cls['re'], 'stdlib')
        self.assertEqual(cls['yaml_is_not_installed_anywhere'], 'unavailable')
        self.assertEqual(self.issues['py2_only_stdlib:urlparse']['severity'], 'needs_decision')
        self.assertEqual(self.issues['py2_only_stdlib:urlparse']['hint'], 'urllib.parse')
        self.assertEqual(self.issues['renamed_stdlib:urllib2']['severity'], 'info')

    def test_needs_decision_set(self):
        nd = sorted(k for k, i in self.issues.items() if i['severity'] == 'needs_decision')
        self.assertEqual(nd, [
            'dynamic_code:shop/plugins.py:__import__',
            'implicit_relative_import:shop/__init__.py -> shop/models.py',
            'implicit_relative_import:shop/models.py -> shop/pricing.py',
            'parse_error:scripts/already_py3.py',
            'py2_only_stdlib:urlparse',
            'stdlib_removed_later:distutils',
            'unavailable_import:yaml_is_not_installed_anywhere',
        ])

    def test_dynamic_literal_becomes_edge(self):
        self.assertIn('shop/util.py', self.files['shop/plugins.py']['depends_on'])

    def test_file_refs(self):
        refs = {r['path']: r['exists'] for r in self.files['scripts/report.py']['file_dependencies']}
        self.assertEqual(refs, {'README.txt': True, 'does_not_exist.cfg': False})

    def test_kinds_and_verification(self):
        self.assertEqual(self.files['tests/test_shop.py']['kind'], 'test')
        self.assertEqual(self.files['setup.py']['kind'], 'packaging')
        self.assertEqual(self.files['scripts/report.py']['kind'], 'script')
        self.assertEqual(self.files['shop/models.py']['verification_command'], '{PY3} tests/test_shop.py')
        self.assertTrue(self.files['shop/plugins.py']['needs_characterization_tests'])


@needs_py2
class EndToEndRulesBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, 'py2repo')
        shutil.copytree(FIXTURE, self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, backend=None):
        backend = backend or RulesBackend()
        log = os.path.join(self.tmp, 'trajectory.jsonl')
        tools = DependencyMapperTools(self.repo, PY2, PY3, ['tools'], backend.model, run_log=log)
        res = loop.run(backend, tools, prompts.SYSTEM, 'map it', 80, log, on_step=lambda s: None)
        return res, tools

    def test_full_run_and_resume(self):
        res, _ = self._run()
        self.assertTrue(res['finished'], res)
        st = state.load_state(self.repo)
        self.assertEqual(len(st['files']), 9)
        by = {f['path']: f for f in st['files']}
        self.assertEqual(by['shop/util.py']['status'], 'blocked')        # urlparse blocked by stub
        self.assertEqual(by['tests/test_shop.py']['status'], 'pending')
        decisions = state.load_decisions(self.repo)
        self.assertEqual(len(decisions), 7)

        # A later agent marks a file passing; a re-run must keep it and reuse decisions.
        by['tests/test_shop.py']['status'] = 'passing'
        by['tests/test_shop.py']['rules'] = ['R1']
        state.write_state(self.repo, st)
        res2, tools2 = self._run()
        self.assertTrue(res2['finished'], res2)
        st2 = state.load_state(self.repo)
        by2 = {f['path']: f for f in st2['files']}
        self.assertEqual(by2['tests/test_shop.py']['status'], 'passing')
        self.assertEqual(by2['tests/test_shop.py']['rules'], ['R1'])
        self.assertEqual(len(state.load_decisions(self.repo)), 7, 'decisions duplicated')
        self.assertEqual(by2['shop/util.py']['status'], 'blocked', 'reused block decision lost')
        progress = state.read_text(os.path.join(self.repo, 'progress.md'))
        self.assertEqual(progress.count('Dependency Mapping Agent'), 2)

    def test_finalize_refuses_incomplete_run(self):
        tools = DependencyMapperTools(self.repo, PY2, PY3, ['tools'], 'm')
        r = tools.tool_finalize('done')
        self.assertFalse(r['ok'])
        self.assertTrue(any('check_interpreters' in m for m in r['missing']))
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'migration_state.json')))

    def test_verdict_guard(self):
        tools = DependencyMapperTools(self.repo, PY2, PY3, ['tools'], 'm')
        bid = next(b.id for b in tools.blocks.values() if b.runnable_here)
        self.assertIn('error', tools.tool_record_command_verdict(bid, 'verified', 'trust me'))

    def test_expected_failure_needs_the_failing_line_marked(self):
        tools = DependencyMapperTools(self.repo, PY2, PY3, ['tools'], 'm')
        b = next(b for b in tools.blocks.values() if b.runnable_here)
        b.exit_code = 1
        b.line_exits = [{'line': 'python run.py   # must pass', 'exit': 1}]
        self.assertIn('error', tools.tool_record_command_verdict(b.id, 'expected_failure', 'documented?'))
        b.line_exits = [{'line': 'python run.py   # EXPECTED TO FAIL on Py3', 'exit': 1}]
        self.assertTrue(tools.tool_record_command_verdict(b.id, 'expected_failure', 'marked EXPECTED')['ok'])


class _FakeQwen(http.server.BaseHTTPRequestHandler):
    """Replays Qwen-style replies: one structured tool call, one in <tool_call> text."""
    replies = []
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        _FakeQwen.seen.append(body)
        msg = _FakeQwen.replies.pop(0)
        out = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}],
                          'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class OpenAICompatBackendTest(unittest.TestCase):
    def test_http_round_trip_and_hermes_fallback(self):
        _FakeQwen.replies = [
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'c1', 'type': 'function',
                'function': {'name': 'echo', 'arguments': '{"x": 1}'}}]},
            {'role': 'assistant', 'content': '<think>plan</think>\n<tool_call>\n'
                                             '{"name": "echo", "arguments": {"x": 2}}\n</tool_call>'},
            {'role': 'assistant', 'content': '<tools>{"name": "echo", "arguments": {"x": 3}}</tools>'},
            {'role': 'assistant', 'content': '```json\n{"name": "echo", "arguments": {"x": 4}}\n```'},
            {'role': 'assistant', 'content': '{"name": "echo", "arguments": {"x": 5}}\n\n\n{"name": "echo"}'},
            {'role': 'assistant', 'content': '{"name": "echo", "arguments": {}}\nI will now wait.'},
            {'role': 'assistant', 'content': '{"name": "echo", "note": "not a call"}'},
            {'role': 'assistant', 'content': 'done'},
        ]
        _FakeQwen.seen = []
        srv = http.server.HTTPServer(('127.0.0.1', 0), _FakeQwen)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            be = OpenAICompatBackend('http://127.0.0.1:%d/v1' % srv.server_port, 'Qwen/test')
            r1 = be.chat([{'role': 'user', 'content': 'hi'}], [])
            self.assertEqual((r1.tool_calls[0].name, r1.tool_calls[0].arguments), ('echo', {'x': 1}))
            r2 = be.chat([], [])
            self.assertEqual(r2.tool_calls[0].arguments, {'x': 2})
            self.assertEqual(r2.content, '')
            self.assertEqual(be.chat([], []).tool_calls[0].arguments, {'x': 3})   # <tools> tag
            r4 = be.chat([], [])                                                  # bare JSON (Ollama)
            self.assertEqual((r4.tool_calls[0].name, r4.tool_calls[0].arguments, r4.content),
                             ('echo', {'x': 4}, ''))
            r5 = be.chat([], [])                                                  # two bare calls
            self.assertEqual([(c.name, c.arguments) for c in r5.tool_calls], [('echo', {'x': 5}), ('echo', {})])
            self.assertEqual(be.chat([], []).tool_calls, [])                      # trailing prose: not a call
            self.assertEqual(be.chat([], []).tool_calls, [])                      # extra keys: not a call
            self.assertEqual(be.chat([], []).tool_calls, [])
            self.assertEqual(_FakeQwen.seen[0]['temperature'], 0.0)
            self.assertEqual(_FakeQwen.seen[0]['model'], 'Qwen/test')
        finally:
            srv.shutdown()
            srv.server_close()


class CheckLlmTest(unittest.TestCase):
    def _serve(self, replies):
        _FakeQwen.replies, _FakeQwen.seen = replies, []
        srv = http.server.HTTPServer(('127.0.0.1', 0), _FakeQwen)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return OpenAICompatBackend('http://127.0.0.1:%d/v1' % srv.server_port, 'Qwen/test')

    def test_preflight(self):
        import contextlib
        import io
        from migagent.stages import mapper as r
        be = self._serve([
            {'role': 'assistant', 'content': '<tool_call>{"name": "ping", "arguments": {"msg": "ok"}}</tool_call>'},
            {'role': 'assistant', 'content': 'I would call ping.'}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.check_llm(be), 0)
            self.assertEqual(r.check_llm(be), 1)


if __name__ == '__main__':
    unittest.main()


class LoopRepeatTest(unittest.TestCase):
    def test_repeated_calls_in_one_reply_run_once(self):
        from migagent.llm import Reply, ToolCall

        class Box(loop.Toolbox):
            n = 0

            def schemas(self):
                return [{'type': 'function', 'function': {'name': 'tick', 'parameters': {}}},
                        {'type': 'function', 'function': {'name': 'done', 'parameters': {}}}]

            def tool_tick(self):
                Box.n += 1
                return {'n': Box.n}

            def tool_done(self):
                self.finished = True
                return {'ok': True}

        class B(loop.Backend):
            name, model, i = 'b', 'b', 0

            def chat(self, messages, tools):
                B.i += 1
                if B.i == 1:
                    return Reply('', [ToolCall('c%d' % k, 'tick', {}) for k in range(20)])
                return Reply('', [ToolCall('d', 'done', {})])
        log = os.path.join(tempfile.mkdtemp(), 't.jsonl')
        res = loop.run(B(), Box(), 's', 'u', 5, log, on_step=lambda s: None)
        self.assertTrue(res['finished'])
        self.assertEqual(Box.n, 1)



class EntryPointTest(unittest.TestCase):
    """modernize.py end to end with the rules backend on a copy of the fixture repo."""

    @needs_py2
    def test_all_stages_then_status(self):
        import subprocess
        tmp = tempfile.mkdtemp()
        try:
            repo = os.path.join(tmp, 'py2repo')
            shutil.copytree(FIXTURE, repo)
            entry = os.path.join(os.path.dirname(HERE), 'modernize.py')
            env = dict(os.environ, PY2_BIN=PY2, PY3_BIN=PY3)
            p = subprocess.run([sys.executable, entry, '-repo', repo, '--backend', 'rules', '--exclude', 'tools',
                                '--results', os.path.join(tmp, 'results'), '--runs-dir', os.path.join(tmp, 'runs'),
                                '--quiet'], capture_output=True, text=True, env=env, timeout=600)
            self.assertIn('== map (', p.stdout, p.stdout + p.stderr)
            for stage in ('migrate', 'verify', 'consistency'):
                self.assertIn('== %s: finished' % stage, p.stdout, p.stdout[-3000:] + p.stderr[-2000:])
            res = os.listdir(os.path.join(tmp, 'results'))
            self.assertEqual(len(res), 1)
            folder = os.path.join(tmp, 'results', res[0])
            for name in ('console.log', 'status.txt', 'EXIT', 'before', 'after', 'runs'):
                self.assertTrue(os.path.exists(os.path.join(folder, name)), name)
            p2 = subprocess.run([sys.executable, entry, '-repo', repo, '--status'], capture_output=True, text=True,
                                env=env)
            self.assertIn('success criterion', p2.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TruncatedReplyTest(unittest.TestCase):
    def test_complete_calls_survive_a_cut_off_reply(self):
        content = '{"name": "a", "arguments": {"x": 1}}\n\n{"name": "b", "arguments": {}}\n\n{"name": "b",'
        cut = OpenAICompatBackend._parse({'choices': [{'message': {'content': content}, 'finish_reason': 'length'}]})
        self.assertEqual([c.name for c in cut.tool_calls], ['a', 'b'])
        whole = OpenAICompatBackend._parse({'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
        self.assertEqual(whole.tool_calls, [])          # not cut off: malformed text is not a call


class MergeExcludedTest(unittest.TestCase):
    def test_excluded_untouched_files_are_dropped_migrated_kept(self):
        old = {'files': [{'path': 'a.py', 'status': 'passing'},
                         {'path': 'tools/x.py', 'status': 'pending'},
                         {'path': 'tools/y.py', 'status': 'in_progress', 'migration': {'unit': 2}},
                         {'path': 'gone.py', 'status': 'pending'}]}
        out = {f['path']: f for f in state.merge_files(old, [{'path': 'a.py', 'status': 'pending'}], ['tools'])}
        self.assertEqual(out['a.py']['status'], 'passing')
        self.assertNotIn('tools/x.py', out)
        self.assertTrue(out['tools/y.py']['excluded'])
        self.assertTrue(out['gone.py']['missing_from_repo'])

