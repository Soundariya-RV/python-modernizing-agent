"""Tools the Cross-File Consistency agent (Qwen 32B) calls, once per repository.

Facts (checks.py): R16 rename consistency, R29 cycle units, Python 3 imports
of every module, and R31 API differences with their references.
Judgement (model): whether each R31 API change is safe (accept: recorded in
decisions.md and reused) or breaks callers (violation), and the summary.

`finish` writes the verdict to migration_state.json (`consistency`), marks
each file with a violation `needs_repair` (a `passing` file goes back to
`in_progress`), and appends to progress.md and decisions.md.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Dict, Optional

from .. import state, trees
from ..loop import Toolbox
from . import checks


def _fn(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': desc,
        'parameters': {'type': 'object', 'properties': props or {}, 'required': required or []}}}


class ConsistencyTools(Toolbox):
    AGENT = 'cross_file_consistency'

    def __init__(self, repo: str, py2: str, py3: str, model: str, run_log: str = ''):
        self.repo = os.path.abspath(repo)
        self.py2, self.py3, self.model, self.run_log = py2, py3, model, run_log
        self.state = state.load_state(self.repo)
        if not self.state:
            raise RuntimeError('no migration_state.json in %s' % self.repo)
        self.prior = state.load_decisions(self.repo)
        self.work = tempfile.mkdtemp(prefix='xfile-')
        self.findings: Optional[list] = None
        self.summary_by_check: Dict[str, dict] = {}
        self.resolution: Dict[str, dict] = {}
        self.finished = False
        self.outputs: Dict[str, str] = {}

    def schemas(self):
        return [
            _fn('run_checks', 'Run the repository-wide checks: R16 renames all-or-nothing, R29 cycles '
                'together, every module imports under Python 3, R31 public API unchanged. Returns every '
                'finding: violation (fails the run), needs_judgement (decide with resolve), info.'),
            _fn('show_finding', 'Full evidence for one finding: baseline and migrated signatures, references '
                'in other files, implicit uses, tracebacks.', {'finding_id': {'type': 'string'}}, ['finding_id']),
            _fn('resolve', 'Decide a needs_judgement finding (an R31 API change). accept = no caller depends on '
                'it, or every caller was updated (say which evidence shows it); violation = callers or '
                'implicit uses can break.',
                {'finding_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['accept', 'violation']},
                 'reason': {'type': 'string'}}, ['finding_id', 'action', 'reason']),
            _fn('finish', 'Record the verdict in migration_state.json, decisions.md and progress.md. Fails '
                'while a needs_judgement finding is unresolved.',
                {'summary': {'type': 'string', 'description': '2-5 sentences for progress.md'}}, ['summary']),
        ]

    def tool_run_checks(self):
        fd = checks.Finder()
        base = trees.baseline_tree(self.repo, self.state, os.path.join(self.work, 'py2-baseline'))
        mig = trees.copy_repo(self.repo, os.path.join(self.work, 'py3-migrated'))
        self.summary_by_check = {
            'R16': checks.r16_renames(fd, self.state, mig, self.py2),
            'R29': checks.r29_cycles(fd, self.state),
            'imports': checks.imports(fd, self.state, mig, self.py3),
            'R31': checks.r31_api(fd, self.state, base, mig, self.py2, self.py3, self.prior),
        }
        self.findings = fd.findings
        return {'checks': self.summary_by_check,
                'findings': [{k: f[k] for k in ('id', 'check', 'severity', 'path', 'subject', 'detail')}
                             for f in self.findings],
                'needs_judgement': [f['id'] for f in self.findings if f['severity'] == 'needs_judgement']}

    def _finding(self, fid):
        if self.findings is None:
            return None
        return next((f for f in self.findings if f['id'] == fid), None)

    def tool_show_finding(self, finding_id: str):
        f = self._finding(finding_id)
        if not f:
            return {'error': 'no finding %r (run_checks first)' % finding_id}
        return f

    def tool_resolve(self, finding_id: str, action: str, reason: str):
        f = self._finding(finding_id)
        if not f:
            return {'error': 'no finding %r (run_checks first)' % finding_id}
        if f['severity'] != 'needs_judgement':
            return {'error': '%s is a %s: only needs_judgement findings can be resolved' % (finding_id, f['severity'])}
        if action not in ('accept', 'violation') or not reason.strip():
            return {'error': 'action must be accept or violation, with a reason'}
        self.resolution[finding_id] = {'action': action, 'reason': reason.strip()}
        return {'ok': True, 'unresolved': self._unresolved()}

    def _unresolved(self):
        return [f['id'] for f in self.findings or [] if f['severity'] == 'needs_judgement' and f['id'] not in self.resolution]

    def tool_finish(self, summary: str):
        missing = []
        if self.findings is None:
            missing.append('run_checks')
        elif self._unresolved():
            missing.append('resolve: %s' % ', '.join(self._unresolved()))
        if not summary.strip():
            missing.append('summary is empty')
        if missing:
            return {'ok': False, 'missing': missing}
        self._write(summary.strip())
        self.finished = True
        return {'ok': True, 'verdict': self.verdict, 'written': self.outputs}

    def _write(self, summary: str):
        today = state.now()
        new_dec = []
        next_id = state.next_decision_id(self.repo)
        for f in self.findings:
            r = self.resolution.get(f['id'])
            if r:
                f['severity'] = 'info' if r['action'] == 'accept' else 'violation'
                f['resolution'] = dict(r, decision='D-%03d' % next_id)
                new_dec.append({'id': 'D-%03d' % next_id, 'title': 'R31 API change: %s' % f['subject'],
                                'key': 'api:%s:%s' % (f['path'], f['subject']), 'kind': 'R31 api-change',
                                'action': r['action'], 'option': r['action'], 'where': f['path'],
                                'context': f['detail'], 'decision': 'API change %s' % (
                                    'accepted' if r['action'] == 'accept' else 'breaks callers: violation'),
                                'reason': r['reason'], 'agent': self.AGENT, 'model': self.model, 'date': today})
                next_id += 1
        violations = [f for f in self.findings if f['severity'] == 'violation']
        self.verdict = 'consistent' if not violations else 'inconsistent'
        st = state.load_state(self.repo)
        by = {e['path']: e for e in st['files']}
        touched = set()
        for f in violations:
            for p in [x.strip() for x in f['path'].split(',')]:
                e = by.get(p)
                if not e or e.get('status') == 'pending':
                    continue
                touched.add(p)
                c = e.setdefault('consistency', {'violations': []})
                c['violations'].append({'check': f['check'], 'subject': f['subject'], 'detail': f['detail']})
        for p, e in by.items():
            if p in touched:
                e['consistency']['at'] = today
                e['needs_repair'] = True
                if e.get('status') == 'passing':
                    e['status'] = 'in_progress'
                e.setdefault('history', []).append({'at': today, 'agent': self.AGENT, 'event': 'violation',
                                                    'status': e['status']})
            elif e.get('status') != 'pending':
                e['consistency'] = {'at': today, 'violations': []}
        st['consistency'] = {'agent': self.AGENT, 'at': today, 'verdict': self.verdict, 'model': self.model,
                             'checks': self.summary_by_check, 'findings': self.findings}
        st['updated_at'] = today
        st['agents'] = dict(st.get('agents', {}), **{self.AGENT: {'last_run': today, 'model': self.model,
                                                                 'run_log': self.run_log}})
        self.outputs['migration_state'] = state.write_state(self.repo, st)
        dp = state.append_decisions(self.repo, new_dec)
        if dp:
            self.outputs['decisions'] = dp
        self.outputs['progress'] = state.append_progress(self.repo, self._progress_md(today, new_dec, summary))
        shutil.rmtree(self.work, ignore_errors=True)

    def _progress_md(self, today, new_dec, summary) -> str:
        counts: Dict[str, int] = {}
        for f in self.findings:
            counts[f['severity']] = counts.get(f['severity'], 0) + 1
        lines = ['## %s - Cross-File Consistency Agent -> %s' % (today[:16].replace('T', ' '), self.verdict), '',
                 '- **Model:** %s' % self.model,
                 '- **Findings:** %s' % (', '.join('%d %s' % (n, s) for s, n in sorted(counts.items())) or 'none'),
                 '- **Decisions:** %d new%s' % (len(new_dec), ' (%s)' % ', '.join(d['id'] for d in new_dec) if new_dec else '')]
        if self.run_log:
            lines.append('- **Run log:** `%s`' % self.run_log)
        r16 = self.summary_by_check.get('R16', {}).get('modules', {})
        lines += ['', '| Check | Result |', '|---|---|',
                  '| R16 renames | %s |' % (', '.join('%s: %s' % kv for kv in r16.items()) or 'no renamed modules in the repo'),
                  '| R29 cycles | %d cycle units |' % self.summary_by_check.get('R29', {}).get('cycles', 0),
                  '| Python 3 imports | %(ok)d ok, %(failed)d failed |' % self.summary_by_check.get('imports', {'ok': 0, 'failed': 0}),
                  '| R31 public API | %d modules compared |' % self.summary_by_check.get('R31', {}).get('modules_compared', 0)]
        if self.findings:
            lines += ['', '| Finding | Check | Severity | Detail |', '|---|---|---|---|']
            for f in self.findings:
                res = f.get('resolution')
                sev = f['severity'] + (' (%s %s)' % (res['action'], res['decision']) if res else '')
                lines.append('| %s | %s | %s | %s |' % (f['id'], f['check'], sev, f['detail'].replace('|', '\\|')[:200]))
        lines += ['', '**Model summary** (not checked by the tools; the facts are above): ' + summary]
        return '\n'.join(lines)
