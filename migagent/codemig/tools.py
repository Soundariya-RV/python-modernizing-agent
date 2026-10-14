"""Tools the Code Migration Agent (Qwen 32B) calls for one migration unit.

Split of responsibility:
  facts     where every catalogue construct is (py2_sites.py, under Python 2.7),
            the candidate rewrites, the runtime types behind each `/`
            (py2_divprobe.py), rendering, the Python 3 compile check and the
            residual scan: computed here.
  judgement which option fits a site whose answer depends on what the code
            means (the catalogue's "Watch for" cases), what to do with Python 2
            constructs outside the catalogue (choose, keep or block), and the
            summary: made by the model, validated and recorded here.

Nothing is written to the repository until `finish_unit`, and it refuses
while a site lacks a choice or a file does not compile under Python 3. A file
with a blocked site is left untouched and marked `blocked`; the others in the
unit become `in_progress` (migrated, waiting for the Verification Gate).
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from .. import state, trees
from ..loop import Toolbox
from .rewrite import Conflict, Edit, py3_compile, render, unified_diff

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = tuple('R%d' % i for i in range(1, 14))
SPECIAL = ('keep', 'block')
MAX_LISTED_CHOICES = 8
MAX_LISTED_AUTO = 60
MAX_DIFF_CHARS = 5000


def _fn(name, desc, props=None, required=None):
    return {'type': 'function', 'function': {
        'name': name, 'description': desc,
        'parameters': {'type': 'object', 'properties': props or {}, 'required': required or []}}}


def run_py2_json(py2: str, script: str, args: List[str], timeout: int = 300) -> dict:
    p = subprocess.run([py2, '-E', '-s', os.path.join(HERE, script)] + args, cwd=HERE,
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError('%s failed (%d): %s' % (script, p.returncode, p.stderr[-2000:]))
    return json.loads(p.stdout)


def git_baseline(repo: str, path: str) -> dict:
    """{'ref': sha} when the file is tracked and unchanged since HEAD, else why not."""
    def git(*a):
        return subprocess.run(['git', '-C', repo] + list(a), capture_output=True, text=True)
    head = git('rev-parse', 'HEAD')
    if head.returncode != 0:
        return {'ref': None, 'why': 'not a git repository'}
    tracked = git('ls-files', '--error-unmatch', '--', path).returncode == 0
    if not tracked:
        return {'ref': None, 'why': 'untracked in git'}
    if git('diff', '--quiet', 'HEAD', '--', path).returncode != 0:
        return {'ref': None, 'why': 'modified since HEAD', 'dirty': True}
    return {'ref': head.stdout.strip()}


class FilePlan:
    def __init__(self, repo: str, entry: dict, py2: str):
        self.path = entry['path']
        self.entry = entry
        self.abspath = os.path.join(repo, self.path)
        with open(self.abspath, 'rb') as f:
            self.src = f.read()
        scan = run_py2_json(py2, 'py2_sites.py', [self.abspath])
        self.parse_error: Optional[str] = scan.get('parse_error')
        self.enc = scan.get('encoding', 'utf-8')
        self.sites: Dict[str, dict] = {s['id']: s for s in scan['sites']}
        self.choice: Dict[str, dict] = {}       # site id -> {option, reason, how, decision}
        self.custom: List[dict] = []            # edit_lines edits
        self.preview: Optional[dict] = None
        self.lines = self.src.decode(self.enc, 'replace').splitlines()
        self.line_starts = [0]
        for line in self.src.splitlines(True):
            self.line_starts.append(self.line_starts[-1] + len(line))

    def key(self, s: dict) -> str:
        return s['key'] or '%s:%s:%s:%d' % (s['rule'], s['kind'], self.path, s['line'])

    def option(self, s: dict, oid: str) -> Optional[dict]:
        return next((o for o in s['options'] if o['id'] == oid), None)

    def unresolved(self) -> List[str]:
        return [sid for sid in self.sites if sid not in self.choice]

    def context(self, line: int, before: int = 2, after: int = 2) -> str:
        lo, hi = max(1, line - before), min(len(self.lines), line + after)
        return '\n'.join('%d: %s' % (n, self.lines[n - 1]) for n in range(lo, hi + 1))

    def edits(self) -> List[Edit]:
        out = []
        for sid, c in self.choice.items():
            s = self.sites[sid]
            if c['option'] in SPECIAL or c['option'].startswith('custom'):
                continue
            o = self.option(s, c['option'])
            out.append(Edit(sid, s['start'], s['end'], o['parts']))
        for n, e in enumerate(self.custom):
            out.append(Edit('custom-%d' % (n + 1), e['start'], e['end'], [e['text']]))
        return out

    def blocked(self) -> List[str]:
        return [sid for sid, c in self.choice.items() if c['option'] == 'block']


class CodeMigrationTools(Toolbox):
    AGENT = 'code_migration'

    def __init__(self, repo: str, py2: str, py3: str, unit: int, files: List[str], model: str,
                 run_log: str = '', command_timeout: int = 600):
        self.repo = os.path.abspath(repo)
        self.py2, self.py3 = py2, py3
        self.unit = unit
        self.model = model
        self.run_log = run_log
        self.command_timeout = command_timeout
        self.state = state.load_state(self.repo)
        if not self.state:
            raise RuntimeError('no migration_state.json in %s: run the Dependency Mapper first' % self.repo)
        by_path = {f['path']: f for f in self.state['files']}
        self.order = list(files)
        self.prior = state.load_decisions(self.repo)
        self.plans: Dict[str, FilePlan] = {}
        for p in self.order:
            plan = FilePlan(self.repo, by_path[p], py2)
            self._apply_prior(plan)
            self.plans[p] = plan
        self.tmp = tempfile.mkdtemp(prefix='codemig-')
        self.finished = False
        self.outputs: Dict[str, str] = {}

    def _apply_prior(self, plan: FilePlan):
        for sid, s in plan.sites.items():
            d = self.prior.get(plan.key(s))
            opt = (d or {}).get('option', '').strip('`')
            if d and (opt in SPECIAL or plan.option(s, opt)):
                plan.choice[sid] = {'option': opt, 'reason': d.get('reason', ''), 'how': 'reused',
                                    'decision': d['id']}
            elif not s['judgement'] and s['default']:
                plan.choice[sid] = {'option': s['default'], 'reason': '', 'how': 'auto', 'decision': None}

    # ------------------------------------------------------------- schemas
    def schemas(self):
        return [
            _fn('list_sites', 'Identify every rule_catalogue.md construct in one file of this unit (demo '
                'step 3). Returns counts per rule, the sites applied automatically, the sites reused from '
                'decisions.md, and the sites that still need your choice (with options and context). '
                'Call it again after choose to see the next sites that need a choice.',
                {'path': {'type': 'string'}}, ['path']),
            _fn('show_lines', 'Show source lines of a file (original Python 2 version), at most 60.',
                {'path': {'type': 'string'}, 'start': {'type': 'integer'}, 'end': {'type': 'integer'}},
                ['path', 'start', 'end']),
            _fn('probe_division', 'R13 evidence: run the file\'s verification command under Python 2 with '
                'every / instrumented, and report the operand types seen on each division line.',
                {'path': {'type': 'string'},
                 'runner': {'type': 'string', 'description': 'optional: what to run instead, e.g. '
                            '"tests/test_x.py" or "-m unittest discover"'}}, ['path']),
            _fn('choose', 'Record your choice for one or more sites of a file. option is one of the '
                'site\'s option ids, or "keep" (leave the Python 2 code unchanged; say why) or "block" '
                '(the file cannot be migrated safely without a human; say what is missing). reason is '
                'required for every site that needs a choice.',
                {'path': {'type': 'string'},
                 'choices': {'type': 'array', 'items': {'type': 'object', 'properties': {
                     'site_id': {'type': 'string'}, 'option': {'type': 'string'},
                     'reason': {'type': 'string'}}, 'required': ['site_id', 'option', 'reason']}}},
                ['path', 'choices']),
            _fn('edit_lines', 'Replace whole lines with your own text, only when no offered option fits. '
                'cite must be a catalogue rule (R1-R13) or a decision id from decisions.md (D-001). '
                'Sites inside the range are covered by this edit.',
                {'path': {'type': 'string'}, 'start_line': {'type': 'integer'},
                 'end_line': {'type': 'integer'}, 'new_text': {'type': 'string'},
                 'cite': {'type': 'string'}, 'reason': {'type': 'string'}},
                ['path', 'start_line', 'end_line', 'new_text', 'cite', 'reason']),
            _fn('preview', 'Render the migrated file from the current choices (nothing is written to the '
                'repo): Python 3 compile result, Python 2 constructs still present, and the diff.',
                {'path': {'type': 'string'}}, ['path']),
            _fn('finish_unit', 'Write the migrated files, update migration_state.json, decisions.md and '
                'progress.md. Fails with a list of what is missing if a site has no choice or a file does '
                'not compile under Python 3.',
                {'summary': {'type': 'string', 'description': '2-6 sentences for progress.md: rules '
                             'applied, choices made, blockers'}}, ['summary']),
        ]

    # ------------------------------------------------------------- tools
    def _plan(self, path):
        plan = self.plans.get(path)
        if plan is None:
            raise KeyError('%r is not in this unit; files: %s' % (path, ', '.join(self.order)))
        return plan

    def call(self, name, args):
        try:
            return super().call(name, args)
        except KeyError as e:   # surfaced as a tool error, not a crash
            return json.dumps({'error': str(e).strip('"')})

    def tool_list_sites(self, path: str):
        plan = self._plan(path)
        if plan.parse_error:
            return {'path': path, 'parse_error': plan.parse_error,
                    'next': 'this file is not valid Python 2: block it with finish_unit (it is marked blocked)'}
        counts: Dict[str, int] = {}
        for s in plan.sites.values():
            counts[s['rule']] = counts.get(s['rule'], 0) + 1
        auto, reused, todo = [], [], []
        for sid, s in sorted(plan.sites.items()):
            c = plan.choice.get(sid)
            if c and c['how'] == 'auto':
                auto.append('%s %s L%d %s: %s -> %s' % (sid, s['rule'], s['line'], s['kind'], _clip(s['before']),
                                                         _clip(plan.option(s, c['option'])['text'])))
            elif c and c['how'] == 'reused':
                reused.append('%s %s L%d %s: %s (%s)' % (sid, s['rule'], s['line'], s['kind'], c['option'],
                                                          c['decision']))
            elif not c:
                todo.append(s)
        out = {'path': path, 'lines': len(plan.lines), 'rules_found': dict(sorted(counts.items())),
               'catalogue_rules_applicable': sorted(r for r in counts if r != 'OUT'),
               'auto': auto[:MAX_LISTED_AUTO]}
        if len(auto) > MAX_LISTED_AUTO:
            out['auto_more'] = len(auto) - MAX_LISTED_AUTO
        if reused:
            out['reused_from_decisions'] = reused
        out['needs_choice'] = [self._site_view(plan, s) for s in todo[:MAX_LISTED_CHOICES]]
        out['needs_choice_remaining'] = len(todo)
        decided = self.state_decisions(plan)
        if decided:
            out['file_decisions'] = decided
        out['next'] = 'choose for each needs_choice site' if todo else 'preview %s' % path
        return out

    def _site_view(self, plan: FilePlan, s: dict) -> dict:
        v = {'site_id': s['id'], 'rule': s['rule'], 'kind': s['kind'], 'line': s['line'],
             'before': _clip(s['before'], 160),
             'options': {o['id']: _clip(o['text'], 160) + (' (%s)' % o['note'] if o['note'] else '')
                         for o in s['options']},
             'suggested': s.get('suggested', s['default']), 'why': s['why'],
             'context': plan.context(s['line'])}
        if s.get('evidence'):
            v['evidence'] = s['evidence']
        if not s['options']:
            v['options'] = {'(none)': 'no rewrite exists in the catalogue: keep (with reason) or block, or '
                                      'edit_lines citing a decision'}
        return v

    def state_decisions(self, plan: FilePlan) -> List[str]:
        out = []
        ids = {d['id']: d for d in self.prior.values()}
        for did in plan.entry.get('decisions', []):
            d = ids.get(did)
            if d:
                out.append('%s (%s): %s' % (did, d.get('key', ''), d.get('decision', '')))
        return out

    def tool_show_lines(self, path: str, start: int, end: int):
        plan = self._plan(path)
        start, end = max(1, int(start)), min(len(plan.lines), int(end))
        if end - start > 59:
            end = start + 59
        return {'path': path, 'text': '\n'.join('%d: %s' % (n, plan.lines[n - 1]) for n in range(start, end + 1))}

    def tool_probe_division(self, path: str, runner: str = ''):
        plan = self._plan(path)
        r13 = [s for s in plan.sites.values() if s['rule'] == 'R13' and s['kind'] == 'division']
        if not r13:
            return {'path': path, 'note': 'no / in this file: nothing to probe'}
        if runner:
            args = shlex.split(runner)
        else:
            cmd = plan.entry.get('verification_command') or ''
            args = shlex.split(cmd.replace('{TMP}', self.tmp))
            if not args or args[0] not in ('{PY3}', '{PY2}'):
                return {'error': 'cannot derive a runner from verification_command %r; pass runner' % cmd}
            args = args[1:]
        if 'py_compile' in args or not args:
            return {'path': path, 'note': 'the verification command only compiles the file, so it executes '
                    'no division. Pass a runner that exercises the code, or decide each site without '
                    'evidence (block if the operand types cannot be known).'}
        out = os.path.join(self.tmp, 'div-%s.json' % hashlib.sha1(path.encode()).hexdigest()[:8])
        try:
            tree = self._baseline_tree()
            subprocess.run([self.py2, '-E', '-s', os.path.join(HERE, 'py2_divprobe.py'), tree,
                            os.path.join(tree, path), out] + args, cwd=HERE, capture_output=True, text=True,
                           timeout=self.command_timeout)
            with open(out, encoding='utf-8') as f:
                res = json.load(f)
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            return {'error': 'probe failed: %s' % e}
        lines = res.get('lines', {})
        report = []
        for s in sorted(r13, key=lambda x: x['start']):
            ev = lines.get(str(s['line']))
            s['evidence'], s['suggested'] = _division_evidence(ev, ' '.join(args))
            report.append({'site_id': s['id'], 'line': s['line'], 'evidence': s['evidence'],
                           'suggested': s['suggested']})
        return {'path': path, 'runner': ' '.join(args), 'runner_exit': res.get('exit'), 'sites': report,
                'note': 'evidence is per line, from the Python 2 baseline; runner_exit != 0 means the '
                        'Python 2 run itself failed'}

    def _baseline_tree(self) -> str:
        """A copy of the repo where files already migrated are back to their Python 2 baseline."""
        tree = os.path.join(self.tmp, 'py2-baseline')
        if not os.path.isdir(tree):
            trees.baseline_tree(self.repo, self.state, tree)
        return tree

    def tool_choose(self, path: str, choices: List[dict]):
        plan = self._plan(path)
        if isinstance(choices, dict):
            choices = [choices]
        done, errors = [], []
        for c in choices or []:
            if not isinstance(c, dict):
                errors.append('each choice must be an object {site_id, option, reason}')
                continue
            sid, opt, reason = c.get('site_id', ''), (c.get('option') or '').strip(), (c.get('reason') or '').strip()
            s = plan.sites.get(sid)
            if not s:
                errors.append('%s: no such site in %s' % (sid, path))
                continue
            if opt not in SPECIAL and not plan.option(s, opt):
                errors.append('%s: option must be one of %s' % (sid, [o['id'] for o in s['options']] + list(SPECIAL)))
                continue
            if not reason and (s['judgement'] or opt in SPECIAL or opt != s['default']):
                errors.append('%s: reason is required' % sid)
                continue
            if any(e['start'] <= s['start'] and s['end'] <= e['end'] for e in plan.custom):
                errors.append('%s: covered by an edit_lines edit' % sid)
                continue
            plan.choice[sid] = {'option': opt, 'reason': reason, 'how': 'model', 'decision': None}
            done.append(sid)
        plan.preview = None
        return {'ok': not errors, 'recorded': done, 'errors': errors, 'still_need_choice': plan.unresolved()}

    def tool_edit_lines(self, path: str, start_line: int, end_line: int, new_text: str, cite: str, reason: str):
        plan = self._plan(path)
        start_line, end_line = int(start_line), int(end_line)
        if not (1 <= start_line <= end_line <= len(plan.lines)):
            return {'error': 'lines must be within 1..%d' % len(plan.lines)}
        cite = cite.strip()
        known = {d['id'] for d in self.prior.values()}
        if cite not in RULES and cite not in known:
            return {'error': 'cite must be R1-R13 or an existing decision id (%s)' % (', '.join(sorted(known)) or 'none')}
        if not reason.strip():
            return {'error': 'reason is required'}
        a, b = plan.line_starts[start_line - 1], plan.line_starts[end_line]
        partial = [sid for sid, s in plan.sites.items() if s['start'] < b and s['end'] > a
                   and not (a <= s['start'] and s['end'] <= b)]
        if partial:
            return {'error': 'sites %s cross the edited range; widen it to whole statements' % partial}
        if any(e['start'] < b and e['end'] > a for e in plan.custom):
            return {'error': 'overlaps an earlier edit_lines edit'}
        text = new_text if new_text.endswith('\n') or b == len(plan.src) else new_text + '\n'
        if plan.src[a:b].endswith(b'\r\n') and not text.endswith('\r\n'):
            text = text.replace('\r\n', '\n').replace('\n', '\r\n')
        absorbed = [sid for sid, s in plan.sites.items() if a <= s['start'] and s['end'] <= b]
        n = len(plan.custom) + 1
        plan.custom.append({'start': a, 'end': b, 'text': text, 'cite': cite, 'reason': reason.strip(),
                            'lines': [start_line, end_line],
                            'before': plan.src[a:b].decode(plan.enc, 'replace')})
        for sid in absorbed:
            plan.choice[sid] = {'option': 'custom-%d' % n, 'reason': reason.strip(), 'how': 'model',
                                'decision': None}
        plan.preview = None
        return {'ok': True, 'edit': 'custom-%d' % n, 'covers_sites': absorbed}

    def tool_preview(self, path: str):
        plan = self._plan(path)
        return self._preview(plan, with_diff=True)

    def _preview(self, plan: FilePlan, with_diff: bool = False) -> dict:
        if plan.parse_error:
            plan.preview = {'compile': {'ok': False, 'error': plan.parse_error}, 'ok': False}
            return dict(plan.preview, path=plan.path)
        try:
            out = render(plan.src, plan.edits(), plan.enc)
        except Conflict as e:
            plan.preview = {'ok': False, 'conflict': str(e)}
            return {'path': plan.path, 'ok': False, 'conflict': str(e)}
        tmp = os.path.join(self.tmp, plan.path.replace('/', '__'))
        with open(tmp, 'wb') as f:
            f.write(out)
        comp = py3_compile(self.py3, tmp)
        res = run_py2_json(self.py2, 'py2_sites.py', [tmp, '--residual'])
        kept = {plan.sites[sid]['line'] for sid, c in plan.choice.items() if c['option'] in SPECIAL}
        residual = res.get('residual', [])
        unexpected = [r for r in residual if r['line'] not in kept]
        changed = sum(1 for l in unified_diff(plan.path, plan.src, out, plan.enc, 0).splitlines()
                      if l.startswith('-') and not l.startswith('---'))
        plan.preview = {'ok': comp['ok'] and not unexpected, 'compile': comp, 'output': out,
                        'sha256': hashlib.sha256(out).hexdigest(), 'changed_lines': changed,
                        'residual': residual, 'unexpected_residual': unexpected}
        view = {'path': plan.path, 'py3_compile': comp, 'changed_lines': changed,
                'py2_constructs_left': residual, 'still_need_choice': plan.unresolved()}
        if unexpected:
            view['problem'] = ('Python 2 constructs left that no choice kept: %s' %
                               ', '.join('%s L%d %s' % (r['rule'], r['line'], r['what']) for r in unexpected))
        if with_diff:
            d = unified_diff(plan.path, plan.src, out, plan.enc, 0)
            view['diff'] = d[:MAX_DIFF_CHARS] + ('\n...[diff truncated]' if len(d) > MAX_DIFF_CHARS else '')
        return view

    def tool_finish_unit(self, summary: str):
        missing = []
        for p in self.order:
            plan = self.plans[p]
            if plan.parse_error:
                continue
            if plan.unresolved():
                missing.append('%s: choose for sites %s' % (p, ', '.join(plan.unresolved())))
                continue
            if plan.blocked():
                continue
            if plan.preview is None or 'output' not in plan.preview:
                self._preview(plan)
            pv = plan.preview
            if pv.get('conflict'):
                missing.append('%s: %s' % (p, pv['conflict']))
            elif not pv['compile']['ok']:
                missing.append('%s: does not compile under Python 3: %s (fix with choose or edit_lines, '
                               'or block the site that causes it)' % (p, pv['compile']['error']))
            elif pv['unexpected_residual']:
                missing.append('%s: Python 2 constructs left that no choice kept: %s' % (
                    p, ', '.join('%s L%d' % (r['rule'], r['line']) for r in pv['unexpected_residual'])))
        if not summary.strip():
            missing.append('summary is empty')
        if missing:
            return {'ok': False, 'missing': missing}
        self._write(summary.strip())
        self.finished = True
        return {'ok': True, 'written': self.outputs,
                'statuses': {p: self._status(self.plans[p]) for p in self.order}}

    # ------------------------------------------------------------- writing
    def _status(self, plan: FilePlan) -> str:
        return 'blocked' if (plan.parse_error or plan.blocked()) else 'in_progress'

    def _write(self, summary: str):
        today = state.now()
        st = state.load_state(self.repo)
        by_path = {f['path']: f for f in st['files']}
        next_id = state.next_decision_id(self.repo)
        new_dec: List[dict] = []
        dec_by_key: Dict[str, dict] = {}
        report = []
        for p in self.order:
            plan = self.plans[p]
            entry = by_path[p]
            status = self._status(plan)
            changes, rules = [], set()
            file_dec = list(entry.get('decisions', []))
            for sid in sorted(plan.sites):
                s, c = plan.sites[sid], plan.choice.get(sid)
                if not c:
                    continue
                if c['how'] == 'model' and (s['judgement'] or c['option'] != s['default']):
                    key = plan.key(s)
                    d = dec_by_key.get(key)
                    if d is None:
                        d = {'id': 'D-%03d' % next_id, 'key': key, 'kind': '%s %s' % (s['rule'], s['kind']),
                             'title': '%s %s: %s' % (s['rule'], s['kind'], key if s['key'] else '%s:%d' % (p, s['line'])),
                             'action': c['option'] if c['option'] in SPECIAL else 'choose',
                             'option': c['option'], 'where': [], 'context': '%s Before: `%s`' % (
                                 s['why'] or ('outside the catalogue' if s['rule'] == 'OUT' else ''),
                                 _clip(s['before'], 120)),
                             'decision': self._decision_text(plan, s, c), 'reason': c['reason'],
                             'agent': self.AGENT, 'model': self.model, 'date': today}
                        next_id += 1
                        dec_by_key[key] = d
                        new_dec.append(d)
                    d['where'].append('%s:%d' % (p, s['line']))
                    c['decision'] = d['id']
                if c.get('decision') and c['decision'] not in file_dec:
                    file_dec.append(c['decision'])
                if c['option'] in SPECIAL or c['option'].startswith('custom'):
                    continue
                o = plan.option(s, c['option'])
                if o['text'] == s['before']:
                    continue               # an explicit "no change" option (e.g. R13 true division)
                if s['rule'] in RULES:
                    rules.add(s['rule'])
                changes.append({'site': sid, 'rule': s['rule'], 'kind': s['kind'], 'line': s['line'],
                                'before': _clip(s['before'], 200), 'after': _clip(o['text'], 200),
                                'how': c['how'], 'decision': c.get('decision')})
            for n, e in enumerate(plan.custom, 1):
                did = e['cite'] if e['cite'].startswith('D-') else None
                if e['cite'] in RULES:
                    rules.add(e['cite'])
                changes.append({'site': 'custom-%d' % n, 'rule': e['cite'] if e['cite'] in RULES else 'OUT',
                                'kind': 'edit_lines', 'line': e['lines'][0], 'before': _clip(e['before'], 200),
                                'after': _clip(e['text'], 200), 'how': 'model', 'decision': did,
                                'reason': e['reason']})
                if did and did not in file_dec:
                    file_dec.append(did)
            counts: Dict[str, int] = {}
            for s in plan.sites.values():
                counts[s['rule']] = counts.get(s['rule'], 0) + 1
            entry['applicable_rules'] = dict(sorted(counts.items()))
            if status == 'blocked':            # nothing is written: record the plan, not changes
                entry['rules'], entry['changes'], entry['planned_changes'] = [], [], changes
            else:
                entry['rules'] = sorted(rules, key=lambda r: int(r[1:]))
                entry['changes'] = changes
                entry.pop('planned_changes', None)
            entry['decisions'] = file_dec
            entry['status'] = status
            hist = entry.setdefault('history', [])
            if status == 'blocked':
                blockers = [b for b in entry.get('blockers', []) if b.get('agent') != self.AGENT]
                for sid in plan.blocked():
                    s, c = plan.sites[sid], plan.choice[sid]
                    blockers.append({'subject': plan.key(s), 'description': '%s %s at line %d: %s' % (
                        s['rule'], s['kind'], s['line'], _clip(s['before'], 120)), 'reason': c['reason'],
                        'decision_id': c.get('decision'), 'agent': self.AGENT, 'raised_at': today})
                if plan.parse_error:
                    blockers.append({'subject': 'parse_error:%s' % p, 'description': plan.parse_error,
                                     'agent': self.AGENT, 'raised_at': today})
                entry['blockers'] = blockers
                hist.append({'at': today, 'agent': self.AGENT, 'event': 'blocked', 'unit': self.unit})
            else:
                base = git_baseline(self.repo, p)
                info = {'agent': self.AGENT, 'migrated_at': today, 'unit': self.unit,
                        'baseline_sha256': hashlib.sha256(plan.src).hexdigest(),
                        'migrated_sha256': plan.preview['sha256'], 'baseline_ref': base.get('ref'),
                        'changed_lines': plan.preview['changed_lines'],
                        'py3_compile': 'ok', 'model': self.model}
                if not base.get('ref'):
                    copy = os.path.join(self.repo, '.migration', 'baseline', p)
                    os.makedirs(os.path.dirname(copy), exist_ok=True)
                    with open(copy, 'wb') as f:
                        f.write(plan.src)
                    info['baseline_copy'] = os.path.relpath(copy, self.repo).replace(os.sep, '/')
                _write_bytes(plan.abspath, plan.preview['output'])
                entry['migration'] = info
                hist.append({'at': today, 'agent': self.AGENT, 'event': 'migrated', 'unit': self.unit,
                             'rules': entry['rules'], 'changes': len(changes)})
            report.append((plan, entry))
        for d in new_dec:
            d['where'] = ', '.join(d['where'])
        st['updated_at'] = today
        st['agents'] = dict(st.get('agents', {}), **{self.AGENT: {
            'last_run': today, 'model': self.model, 'run_log': self.run_log}})
        self.outputs['migration_state'] = state.write_state(self.repo, st)
        dp = state.append_decisions(self.repo, new_dec)
        if dp:
            self.outputs['decisions'] = dp
        self.outputs['progress'] = state.append_progress(self.repo, self._progress_md(today, report, new_dec, summary))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decision_text(self, plan: FilePlan, s: dict, c: dict) -> str:
        if c['option'] == 'keep':
            return 'keep the Python 2 code unchanged'
        if c['option'] == 'block':
            return 'block: needs a human before this file can be migrated'
        o = plan.option(s, c['option'])
        return 'option `%s`: `%s`' % (c['option'], _clip(o['text'], 120)) if o else c['option']

    def _progress_md(self, today: str, report, new_dec: List[dict], summary: str) -> str:
        lines = ['## %s - Code Migration Agent (unit %d)' % (today[:16].replace('T', ' '), self.unit), '',
                 '- **Model:** %s' % self.model]
        if self.run_log:
            lines.append('- **Run log:** `%s`' % self.run_log)
        lines.append('- **Decisions:** %d new%s' % (len(new_dec), (' (%s)' % ', '.join(d['id'] for d in new_dec))
                                                    if new_dec else ''))
        for plan, entry in report:
            found = ', '.join('%s x%d' % (r, n) for r, n in entry['applicable_rules'].items()) or 'none'
            lines += ['', '### `%s` -> %s' % (plan.path, entry['status']), '',
                      '- **Constructs found:** %s' % found,
                      '- **Rules applied:** %s' % (', '.join(entry['rules']) or 'none')]
            if entry['status'] == 'blocked':
                lines.append('- **Blocked by:** %s' % '; '.join(b['description'] for b in entry['blockers']
                                                              if b.get('agent') == self.AGENT))
                continue
            lines.append('- **Lines changed:** %d' % entry['migration']['changed_lines'])
            kept = [(sid, plan.choice[sid]) for sid in sorted(plan.choice) if plan.choice[sid]['option'] == 'keep']
            if kept:
                lines.append('- **Kept unchanged:** %s' % '; '.join('%s L%d (%s)' % (
                    plan.sites[sid]['kind'], plan.sites[sid]['line'], c.get('decision') or c['reason'])
                    for sid, c in kept))
            if entry['changes']:
                lines += ['', '| Line | Rule | Change | How |', '|---|---|---|---|']
                for ch in entry['changes']:
                    how = ch['how'] + (' %s' % ch['decision'] if ch.get('decision') else '')
                    lines.append('| %d | %s | `%s` -> `%s` | %s |' % (
                        ch['line'], ch['rule'], _md(ch['before']), _md(ch['after']), how))
        lines += ['', '**Model summary** (not checked by the tools; the facts are above): ' + summary]
        return '\n'.join(lines)


def _division_evidence(ev: Optional[dict], runner: str):
    if not ev:
        return 'never executed by `%s`' % runner, None
    types = ev['types']
    desc = ', '.join('%s x%d' % (k, v) for k, v in sorted(types.items()))
    txt = 'executed %d times: %s' % (ev['count'], desc)
    if ev.get('inexact'):
        txt += '; %d integer divisions truncated' % ev['inexact']
    if ev.get('negative'):
        txt += ' (%d with operands of opposite sign: Py2 floors toward -inf, like //)' % ev['negative']
    if ev.get('errors'):
        txt += '; errors: %s' % ev['errors']
    ints = ('int', 'long', 'bool')
    pairs = [k.split('/') for k in types]
    if all(a in ints and b in ints for a, b in pairs):
        return txt, 'floor'
    if all(a == 'float' or b == 'float' for a, b in pairs):
        return txt, 'true'
    return txt + '; mixed operand types: neither / nor // alone preserves Py2 behaviour', None


def _write_bytes(path: str, data: bytes):
    tmp = path + '.codemig.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _clip(s: str, n: int = 70) -> str:
    s = s.replace('\n', '\\n')
    return s if len(s) <= n else s[:n - 3] + '...'


def _md(s: str) -> str:
    return s.replace('|', '\\|').replace('`', "'")
