"""Tools the Dependency Mapping Agent (Qwen 32B) can call.

Split of responsibility:
  facts     parsing, interpreter probes, graph, order, command exit codes:
            computed here, never by the model.
  judgement which Agent.md failures are expected, how to resolve each issue
            that needs a decision, verification-command overrides, and the
            summary: made by the model, validated and recorded here.

`finalize` refuses to write anything until the run is complete, so a model
that skips a step gets told what is missing instead of producing a partial
migration_state.json.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Dict, List, Optional

from .. import agent_md, state, trees
from ..loop import Toolbox
from .analyze import analyze, interpreter_version

VERDICTS = ('verified', 'failed', 'expected_failure', 'skipped')
MAX_RESULT_CHARS = 11000     # the loop truncates tool results at 12000
ACTIONS = ('decide', 'block')


def _fn(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': desc,
        'parameters': {'type': 'object', 'properties': props or {},
                       'required': required or []}}}


class DependencyMapperTools(Toolbox):
    AGENT = 'dependency_mapper'

    def __init__(self, repo: str, py2: str, py3: str, excludes: List[str], model: str,
                 shell: Optional[str] = None, command_timeout: int = 600,
                 agent_md_name: str = 'Agent.md', run_log: str = ''):
        self.repo = os.path.abspath(repo)
        self.py2, self.py3 = py2, py3
        self.excludes = list(excludes)
        self.model = model
        self.shell = shell or agent_md.host_shell()
        self.command_timeout = command_timeout
        self.run_log = run_log
        self.agent_md_path = self._find_agent_md(agent_md_name)
        self.agent_md_text = state.read_text(self.agent_md_path) if self.agent_md_path else ''
        self.blocks = {b.id: b for b in agent_md.parse(self.agent_md_text, self.shell)}
        self.interpreters: Optional[dict] = None
        self.analysis: Optional[dict] = None
        self.prior_decisions = state.load_decisions(self.repo)
        # Agent.md describes the Python 2 baseline. Once files are migrated, its commands and the
        # import scan run on a copy of the repo with those files put back to their baseline.
        old = state.load_state(self.repo)
        self.migrated = sorted(f['path'] for f in (old or {}).get('files', [])
                               if f.get('migration') and f.get('status', 'pending') != 'pending')
        self._tmp = None
        self.run_root = self.repo
        if self.migrated:
            self._tmp = tempfile.mkdtemp(prefix='depmap-')
            self.run_root = trees.baseline_tree(self.repo, old, os.path.join(self._tmp, 'py2-baseline'))
        self.resolutions: Dict[str, dict] = {}       # issue key -> resolution
        self.extra_blockers: List[dict] = []
        self.verification_overrides: Dict[str, dict] = {}
        self.finished = False
        self.outputs: Dict[str, str] = {}

    def _find_agent_md(self, name):
        for cand in (name, 'agent.md', 'AGENT.md', 'Agent.md', 'agents.md', 'AGENTS.md'):
            p = os.path.join(self.repo, cand)
            if os.path.exists(p):
                return p
        return None

    # ------------------------------------------------------------- schemas
    def schemas(self):
        return [
            _fn('read_agent_md', 'Return the text of the repository\'s Agent.md (project description, '
                'environment, build and test commands, pointers to the rule catalogue).',
                {'offset': {'type': 'integer', 'description': 'character offset, default 0'}}),
            _fn('list_agent_md_commands', 'List the fenced shell blocks in Agent.md in document order: '
                'which lines will run on this host, which are dropped and why, and any result so far.'),
            _fn('run_agent_md_command', 'Run one Agent.md block (by id) in the repository root with '
                'PY2_BIN/PY3_BIN injected. Returns the exit code and the output tail. Blocks can depend '
                'on files written by earlier blocks, so run them in document order.',
                {'block_id': {'type': 'string'}}, ['block_id']),
            _fn('record_command_verdict', 'Record your verdict for a block after reading its output. '
                'verified = did what Agent.md says; failed = did not, and Agent.md does not expect that; '
                'expected_failure = non-zero exit or failure that Agent.md itself documents as expected '
                '(quote where in the note); skipped = not run here (other platform, setup-only, '
                'placeholder) - say why.',
                {'block_id': {'type': 'string'},
                 'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                 'note': {'type': 'string', 'description': 'one or two sentences of evidence'}},
                ['block_id', 'verdict', 'note']),
            _fn('check_interpreters', 'Report the versions of the Python 2 and Python 3 interpreters '
                'the agent will use (PY2_BIN, PY3_BIN).'),
            _fn('build_dependency_graph', 'Parse every .py file under Python 2.7, resolve imports, '
                'classify external modules under both interpreters, group import cycles and compute '
                'the migration order. Returns a summary plus the issues you must resolve.',
                {'exclude_dirs': {'type': 'array', 'items': {'type': 'string'},
                                  'description': 'repo-relative directories that are not part of the '
                                  'code to migrate (for example agent helper tools named in Agent.md). '
                                  'Each must exist. Optional.'}}),
            _fn('get_file_details', 'Full dependency record for one file: import edges with line '
                'numbers, external imports, file references, default verification command.',
                {'path': {'type': 'string'}}, ['path']),
            _fn('resolve_issue', 'Resolve one issue whose severity is needs_decision. action=decide '
                'records a decision in decisions.md (reused automatically if the same key appears '
                'again); action=block marks it as a blocker in migration_state.json and the affected '
                'files become status=blocked. Never pick a module replacement that is not in the rule '
                'catalogue as a silent rewrite: decide only what the later agents should do.',
                {'issue_key': {'type': 'string'},
                 'action': {'type': 'string', 'enum': list(ACTIONS)},
                 'decision': {'type': 'string', 'description': 'what will be done, one sentence'},
                 'reason': {'type': 'string', 'description': 'why, citing evidence'}},
                ['issue_key', 'action', 'decision', 'reason']),
            _fn('flag_blocker', 'Record a blocker that is not one of the graph issues, for example an '
                'Agent.md build or test command that fails on the Python 2 baseline, or a Python 3 '
                'build blocker documented in Agent.md.',
                {'subject': {'type': 'string'},
                 'path': {'type': 'string', 'description': 'affected repo file, or empty for repo-wide'},
                 'description': {'type': 'string'},
                 'source': {'type': 'string', 'description': 'evidence: block id, Agent.md section, issue key'}},
                ['subject', 'description', 'source']),
            _fn('set_verification_command', 'Override the default verification command for a file. '
                'Use {PY2}, {PY3}, {TMP} placeholders. Only when Agent.md or the graph gives a reason.',
                {'path': {'type': 'string'}, 'command': {'type': 'string'},
                 'reason': {'type': 'string'}}, ['path', 'command', 'reason']),
            _fn('finalize', 'Write migration_state.json, append to progress.md and decisions.md. '
                'Fails with a list of what is missing if the run is incomplete.',
                {'summary': {'type': 'string', 'description': '3-8 sentences for progress.md: what was '
                             'verified, the migration order, blockers and decisions.'}},
                ['summary']),
        ]

    # ------------------------------------------------------------- tools
    def tool_read_agent_md(self, offset: int = 0):
        if not self.agent_md_path:
            return {'error': 'no Agent.md in %s' % self.repo}
        chunk = 10000
        text = self.agent_md_text[offset: offset + chunk]
        rest = len(self.agent_md_text) - offset - len(text)
        return {'path': os.path.basename(self.agent_md_path), 'offset': offset,
                'remaining_chars': max(rest, 0), 'text': text}

    def tool_list_agent_md_commands(self):
        return {'host_shell': self.shell, 'blocks': [b.summary() for b in self.blocks.values()]}

    def tool_run_agent_md_command(self, block_id: str):
        b = self.blocks.get(block_id)
        if not b:
            return {'error': 'no block %r; call list_agent_md_commands' % block_id}
        if not b.runnable_here:
            return {'block_id': block_id, 'ran': False,
                    'reason': b.platform_note or 'every line is dropped (setup / placeholder)',
                    'dropped': [{'line': l, 'reason': r} for l, r in b.dropped],
                    'next': 'record_command_verdict with verdict=skipped'}
        agent_md.run_block(b, self.run_root, {'PY2_BIN': self.py2, 'PY3_BIN': self.py3},
                           self.command_timeout)
        where = ('a Python 2 baseline copy of the repo (%d migrated files restored)' % len(self.migrated)
                 if self.migrated else 'the repository')
        return {'block_id': block_id, 'section': b.section, 'ran': True, 'ran_in': where, 'exit_code': b.exit_code,
                'seconds': b.seconds, 'line_exits': b.line_exits,
                'dropped': [{'line': l, 'reason': r} for l, r in b.dropped],
                'output_tail': b.output_tail}

    def tool_record_command_verdict(self, block_id: str, verdict: str, note: str):
        b = self.blocks.get(block_id)
        if not b:
            return {'error': 'no block %r' % block_id}
        if verdict not in VERDICTS:
            return {'error': 'verdict must be one of %s' % (VERDICTS,)}
        if verdict != 'skipped' and b.exit_code is None:
            return {'error': 'block %s has not been run; run it first or use verdict=skipped' % block_id}
        failing = [x['line'] for x in b.line_exits if x.get('exit') not in (0, None)]
        if verdict == 'expected_failure':
            unmarked = [l for l in failing if 'EXPECTED' not in l.upper()]
            if unmarked:
                return {'error': 'these lines failed but Agent.md does not mark them EXPECTED, so the failure is '
                                 'not documented as expected: %s. Use verdict=failed.' % unmarked}
        if verdict == 'verified' and b.exit_code != 0:
            return {'error': 'block %s exited %s; a non-zero exit cannot be "verified". Use failed or '
                             'expected_failure (citing where Agent.md expects it).' % (block_id, b.exit_code)}
        if verdict == 'skipped' and b.runnable_here and b.exit_code is None and len(note.strip()) < 20:
            return {'error': 'skipping a block that can run here needs a real reason in note'}
        b.verdict, b.note = verdict, note.strip()
        return {'ok': True, 'block_id': block_id, 'verdict': verdict}

    def tool_check_interpreters(self):
        v2, v3 = interpreter_version(self.py2), interpreter_version(self.py3)
        ok2, ok3 = v2.startswith('2.7'), v3.startswith('3.')
        self.interpreters = {'py2': {'bin': self.py2, 'version': v2, 'ok': ok2},
                             'py3': {'bin': self.py3, 'version': v3, 'ok': ok3}}
        return self.interpreters

    def tool_build_dependency_graph(self, exclude_dirs: Optional[List[str]] = None):
        if exclude_dirs:
            bad = [d for d in exclude_dirs if not os.path.isdir(os.path.join(self.repo, d))]
            if bad:
                return {'error': 'not directories in the repo: %s' % bad}
            self.excludes = sorted(set(self.excludes) | {d.strip('/\\').replace('\\', '/') for d in exclude_dirs})
        self.analysis = analyze(self.run_root, self.py2, self.py3, self.excludes)
        a = self.analysis
        need = [dict(i, already_decided=self._prior(i['key'])) for i in a['issues'] if i['severity'] == 'needs_decision']
        other: Dict[str, int] = {}
        for i in a['issues']:
            if i['severity'] != 'needs_decision':
                other['%s (%s)' % (i['kind'], i['severity'])] = other.get('%s (%s)' % (i['kind'], i['severity']), 0) + 1
        out = {   # what the model must act on comes first: long results are truncated at the end
            'to_resolve': self._unresolved(),
            'issues_needing_decision': need,
            'other_issues': other,
            'excluded_dirs': self.excludes,
            'counts': {'files': len(a['files']), 'units': len(a['units']),
                       'cycles': sum(1 for u in a['units'] if u['cycle'])},
            'migration_order': [u['files'] for u in a['units']],
            'cycles': [u['files'] for u in a['units'] if u['cycle']],
            'files': [{'path': f['path'], 'kind': f['kind'], 'unit': f['unit'], 'depends_on': f['depends_on'],
                       'external': f['external_imports'], 'verification_command': f['verification_command'],
                       'needs_characterization_tests': f['needs_characterization_tests']} for f in a['files']],
            'external_modules': [{'module': e['module'], 'class': e['class'], 'rule': e['rule'],
                                  'guarded': e['guarded_import'], 'used_by': e['used_by']}
                                 for e in a['external_modules']],
        }
        budget = MAX_RESULT_CHARS
        if len(json.dumps(out)) > budget:
            out['external_modules'] = ['%s (%s)' % (e['module'], e['class']) for e in a['external_modules']]
        if len(json.dumps(out)) > budget:
            out['files'] = ['%s (%s, unit %d%s)' % (f['path'], f['kind'], f['unit'],
                                                    ', deps: ' + ','.join(f['depends_on']) if f['depends_on'] else '')
                            for f in a['files']]
        if len(json.dumps(out)) > budget:
            out['files'] = 'omitted (%d files): use get_file_details(path)' % len(a['files'])
            out['migration_order'] = '%d units: see migration_state.json after finalize' % len(a['units'])
        for k in ('issues_needing_decision',):
            while len(json.dumps(out)) > budget and len(out[k]) > 1:
                out[k] = out[k][:-1]
                out['issues_needing_decision_omitted'] = len(need) - len(out[k])
        return out

    def tool_get_file_details(self, path: str):
        if not self.analysis:
            return {'error': 'call build_dependency_graph first'}
        for f in self.analysis['files']:
            if f['path'] == path:
                return f
        return {'error': 'no file %r in the graph' % path}

    def tool_resolve_issue(self, issue_key: str, action: str, decision: str, reason: str):
        if not self.analysis:
            return {'error': 'call build_dependency_graph first'}
        issue = next((i for i in self.analysis['issues'] if i['key'] == issue_key), None)
        if not issue:
            return {'error': 'no issue with key %r' % issue_key}
        if action not in ACTIONS:
            return {'error': 'action must be decide or block'}
        if not decision.strip() or not reason.strip():
            return {'error': 'decision and reason are both required'}
        prior = self._prior(issue_key)
        if prior and action == 'decide':
            return {'ok': True, 'reused': prior,
                    'note': 'decisions.md already has %s for this key; it is reused, not duplicated. '
                            'Edit decisions.md by hand to change it.' % prior['id']}
        self.resolutions[issue_key] = {'issue': issue, 'action': action,
                                       'decision': decision.strip(), 'reason': reason.strip()}
        return {'ok': True, 'issue_key': issue_key, 'action': action,
                'remaining': self._unresolved()}

    def tool_flag_blocker(self, subject: str, description: str, source: str, path: str = ''):
        if path and self.analysis and path not in {f['path'] for f in self.analysis['files']}:
            return {'error': 'path %r is not a file in the graph (leave empty for repo-wide)' % path}
        self.extra_blockers.append({'subject': subject, 'path': path or None,
                                    'description': description, 'source': source,
                                    'agent': self.AGENT, 'raised_at': state.now()})
        return {'ok': True, 'blockers': len(self.extra_blockers)}

    def tool_set_verification_command(self, path: str, command: str, reason: str):
        if not self.analysis:
            return {'error': 'call build_dependency_graph first'}
        if path not in {f['path'] for f in self.analysis['files']}:
            return {'error': 'no file %r' % path}
        if '{PY3}' not in command:
            return {'error': 'the command must run under Python 3: use the {PY3} placeholder'}
        self.verification_overrides[path] = {'command': command, 'reason': reason}
        return {'ok': True}

    def tool_finalize(self, summary: str):
        missing = self._missing()
        if not summary.strip():
            missing.append('summary is empty')
        if missing:
            return {'ok': False, 'missing': missing}
        self._write(summary.strip())
        self.finished = True
        return {'ok': True, 'written': self.outputs}

    # ------------------------------------------------------------- helpers
    def _prior(self, key):
        d = self.prior_decisions.get(key)
        return {'id': d['id'], 'decision': d.get('decision'), 'action': d.get('action')} if d else None

    def _unresolved(self) -> List[str]:
        if not self.analysis:
            return []
        return [i['key'] for i in self.analysis['issues']
                if i['severity'] == 'needs_decision' and i['key'] not in self.resolutions
                and not self._prior(i['key'])]

    def _missing(self) -> List[str]:
        m = []
        if not self.agent_md_path:
            m.append('Agent.md not found (cannot verify commands)')
        if not self.interpreters:
            m.append('call check_interpreters')
        elif not (self.interpreters['py2']['ok'] and self.interpreters['py3']['ok']):
            m.append('interpreters are wrong: %s' % json.dumps(self.interpreters))
        if not self.analysis:
            m.append('call build_dependency_graph')
        no_verdict = [b.id for b in self.blocks.values() if b.verdict is None]
        if no_verdict:
            m.append('record_command_verdict for blocks: %s' % ', '.join(no_verdict))
        if self.analysis and self._unresolved():
            m.append('resolve_issue for: %s' % ', '.join(self._unresolved()))
        return m

    def _write(self, summary: str):
        a = self.analysis
        old = state.load_state(self.repo)
        today = state.now()
        blockers_by_file: Dict[str, List[dict]] = {}
        repo_blockers: List[dict] = []

        # decisions.md entries (new only; reused ones are referenced)
        next_id = state.next_decision_id(self.repo)
        new_dec = []
        decision_refs: Dict[str, str] = {}
        for key, r in sorted(self.resolutions.items()):
            did = 'D-%03d' % next_id
            next_id += 1
            decision_refs[key] = did
            i = r['issue']
            new_dec.append({'id': did, 'title': '%s: %s' % (i['kind'], i['subject']), 'key': key,
                            'kind': i['kind'], 'action': r['action'], 'where': ', '.join(i['where']),
                            'context': i['detail'] + (' Hint: %s.' % i['hint'] if i.get('hint') else ''),
                            'decision': r['decision'], 'reason': r['reason'], 'agent': self.AGENT,
                            'model': self.model, 'date': today})
            if r['action'] == 'block':
                b = {'subject': key, 'description': r['decision'], 'reason': r['reason'],
                     'decision_id': did, 'agent': self.AGENT, 'raised_at': today}
                files = sorted({w.rsplit(':', 1)[0] for w in i['where']})
                for fpath in files:
                    blockers_by_file.setdefault(fpath, []).append(b)
                if not files:
                    repo_blockers.append(b)
        for key in [i['key'] for i in a['issues']]:
            if self._prior(key) and key not in decision_refs:
                decision_refs[key] = self._prior(key)['id']
                if (self._prior(key).get('action') or '') == 'block':
                    i = next(x for x in a['issues'] if x['key'] == key)
                    for fpath in sorted({w.rsplit(':', 1)[0] for w in i['where']}):
                        blockers_by_file.setdefault(fpath, []).append(
                            {'subject': key, 'description': self._prior(key)['decision'],
                             'decision_id': self._prior(key)['id'], 'agent': self.AGENT, 'raised_at': today})
        for b in self.extra_blockers:
            (blockers_by_file.setdefault(b['path'], []) if b['path'] else repo_blockers).append(b)

        files = []
        for f in a['files']:
            entry = {
                'path': f['path'], 'status': 'pending', 'unit': f['unit'], 'kind': f['kind'],
                'module': f['module'], 'cycle': f['cycle'], 'lines': f['lines'],
                'depends_on': f['depends_on'], 'imported_by': f['imported_by'],
                'external_imports': f['external_imports'], 'file_dependencies': f['file_dependencies'],
                'verification_command': f['verification_command'],
                'verification_basis': f['verification_basis'],
                'needs_characterization_tests': f['needs_characterization_tests'],
                'rules': [],                    # filled by the Code Migration agent
                'blockers': blockers_by_file.get(f['path'], []),
                'decisions': sorted({decision_refs[i['key']] for i in a['issues']
                                     if i['key'] in decision_refs
                                     and any(w.rsplit(':', 1)[0] == f['path'] for w in i['where'])}),
            }
            ov = self.verification_overrides.get(f['path'])
            if ov:
                entry['verification_command'] = ov['command']
                entry['verification_basis'] = 'set by %s: %s' % (self.AGENT, ov['reason'])
            files.append(entry)
        files = state.merge_files(old, files, self.excludes)
        for f in files:
            if f.get('blockers') and f.get('status', 'pending') == 'pending':
                f['status'] = 'blocked'

        new_state = {
            'schema_version': state.SCHEMA_VERSION,
            'repo': os.path.basename(self.repo),
            'target_python': self.interpreters['py3']['version'],
            'baseline_python': self.interpreters['py2']['version'],
            'updated_at': today,
            'statuses': list(state.STATUSES),
            'environment': {
                'py2_bin': self.py2, 'py3_bin': self.py3, 'host_shell': self.shell,
                'agent_md': os.path.basename(self.agent_md_path),
                'agent_md_commands': [{'block': b.id, 'section': b.section, 'lang': b.lang,
                                       'exit_code': b.exit_code, 'line_exits': b.line_exits,
                                       'verdict': b.verdict, 'note': b.note,
                                       'dropped_lines': len(b.dropped)}
                                      for b in self.blocks.values()],
            },
            'excluded_dirs': self.excludes,
            'migration_order': [u['files'] for u in a['units']],
            'files': files,
            'external_modules': [{k: e[k] for k in ('module', 'class', 'rule', 'guarded_import', 'used_by')}
                                 for e in a['external_modules']],
            'blockers': repo_blockers,
            'agents': dict((old or {}).get('agents', {}), **{self.AGENT: {
                'last_run': today, 'model': self.model, 'run_log': self.run_log}}),
        }
        for k in ('history',):
            if old and k in old:
                new_state[k] = old[k]
        self.outputs['migration_state'] = state.write_state(self.repo, new_state)
        dp = state.append_decisions(self.repo, new_dec)
        if dp:
            self.outputs['decisions'] = dp
        self.outputs['progress'] = state.append_progress(self.repo, self._progress_md(new_state, new_dec, summary))
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _progress_md(self, st: dict, new_dec: List[dict], summary: str) -> str:
        cmds = st['environment']['agent_md_commands']
        counts: Dict[str, int] = {}
        for c in cmds:
            counts[c['verdict']] = counts.get(c['verdict'], 0) + 1
        lines = ['## %s - Dependency Mapping Agent' % st['updated_at'][:16].replace('T', ' '), '',
                 '- **Model:** %s' % self.model,
                 '- **Interpreters:** Python %s -> Python %s' % (st['baseline_python'], st['target_python']),
                 '- **Agent.md commands:** %s' % ', '.join('%d %s' % (n, v) for v, n in sorted(counts.items())),
                 '- **Files:** %d in %d migration units (%d with cycles)' % (
                     len(st['files']), len(st['migration_order']),
                     sum(1 for u in st['migration_order'] if len(u) > 1)),
                 '- **Order:** ' + ' -> '.join('[%s]' % ', '.join(u) if len(u) > 1 else u[0]
                                             for u in st['migration_order']),
                 '- **Decisions:** %d new%s' % (len(new_dec), (' (%s)' % ', '.join(d['id'] for d in new_dec)) if new_dec else ''),
                 '- **Blockers:** %d file-level, %d repo-wide' % (
                     sum(len(f.get('blockers', [])) for f in st['files']), len(st['blockers'])),
                 ]
        if self.run_log:
            lines.append('- **Run log:** `%s`' % self.run_log)
        lines += ['', '| Block | Section | Exit | Verdict | Note |', '|---|---|---|---|---|']
        for c in cmds:
            lines.append('| %s | %s | %s | %s | %s |' % (c['block'], c['section'], '' if c['exit_code'] is None else c['exit_code'],
                                                       c['verdict'], (c['note'] or '').replace('|', '\\|')))
        lines += ['', summary]
        return '\n'.join(lines)
