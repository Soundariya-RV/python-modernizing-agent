"""Build the dependency facts for a Python 2 repository.

Everything in here is decided by parsing (under Python 2.7) or by asking the
real interpreters where a module lives. The LLM never edits these facts; it
reads them and resolves the issues they raise.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .graph import migration_units

HERE = os.path.dirname(os.path.abspath(__file__))

# rule_catalogue.md R11 — the only module renames the catalogue accepts.
R11_RENAMES = {
    'urllib2': 'urllib.request (+ urllib.error)',
    'ConfigParser': 'configparser',
    'Queue': 'queue',
    'cStringIO': 'io.StringIO (io.BytesIO for bytes)',
    'StringIO': 'io.StringIO (io.BytesIO for bytes)',
}

# Python-2-only stdlib modules that the catalogue does NOT cover. Shown to the
# model as hints only; using one is a decision, never a silent rewrite.
PY2_ONLY_HINTS = {
    'urlparse': 'urllib.parse', 'urllib': 'urllib.parse / urllib.request (py2 urllib API differs)',
    'httplib': 'http.client', 'cookielib': 'http.cookiejar', 'Cookie': 'http.cookies',
    'HTMLParser': 'html.parser', 'htmlentitydefs': 'html.entities',
    'BaseHTTPServer': 'http.server', 'SimpleHTTPServer': 'http.server',
    'CGIHTTPServer': 'http.server', 'SocketServer': 'socketserver',
    'cPickle': 'pickle', 'copy_reg': 'copyreg', '__builtin__': 'builtins',
    'xmlrpclib': 'xmlrpc.client', 'SimpleXMLRPCServer': 'xmlrpc.server',
    'thread': '_thread', 'dummy_thread': '_dummy_thread (removed 3.9)',
    'UserDict': 'collections.UserDict', 'UserList': 'collections.UserList',
    'UserString': 'collections.UserString', 'anydbm': 'dbm', 'dbhash': 'dbm.bsd',
    'whichdb': 'dbm', 'gdbm': 'dbm.gnu', 'dumbdbm': 'dbm.dumb',
    'robotparser': 'urllib.robotparser', 'commands': 'subprocess',
    'md5': 'hashlib', 'sha': 'hashlib', 'sets': 'builtin set',
    'Tkinter': 'tkinter', 'tkMessageBox': 'tkinter.messagebox',
    'markupbase': '_markupbase', 'repr': 'reprlib', 'exceptions': 'builtins',
    'new': 'types', 'mimetools': 'email', 'rfc822': 'email', 'HTMLParser.HTMLParser': 'html.parser',
}

# Stdlib modules that exist in 3.11 but are deprecated and removed later.
REMOVED_AFTER_311 = {
    'distutils': '3.12', 'imp': '3.12', 'asynchat': '3.12', 'asyncore': '3.12',
    'smtpd': '3.12', 'aifc': '3.13', 'audioop': '3.13', 'cgi': '3.13',
    'cgitb': '3.13', 'chunk': '3.13', 'crypt': '3.13', 'imghdr': '3.13',
    'mailcap': '3.13', 'msilib': '3.13', 'nis': '3.13', 'nntplib': '3.13',
    'ossaudiodev': '3.13', 'pipes': '3.13', 'sndhdr': '3.13', 'spwd': '3.13',
    'sunau': '3.13', 'telnetlib': '3.13', 'uu': '3.13', 'xdrlib': '3.13',
}

TEST_NAME_HINTS = ('test_', 'tests_')


@dataclass
class Issue:
    key: str
    kind: str
    severity: str            # needs_decision | warning | info
    subject: str
    detail: str
    hint: Optional[str] = None
    where: List[Tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {'key': self.key, 'kind': self.kind, 'severity': self.severity,
             'subject': self.subject, 'detail': self.detail,
             'where': ['%s:%d' % w for w in sorted(set(self.where))]}
        if self.hint:
            d['hint'] = self.hint
        return d


def run_json(cmd: List[str], cwd: Optional[str] = None, timeout: int = 300) -> dict:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError('command failed (%d): %s\n%s' % (p.returncode, ' '.join(cmd), p.stderr[-2000:]))
    return json.loads(p.stdout)


def _py2_flags() -> List[str]:
    return ['-E', '-s']       # Python 2.7 has no -I


def scan_repo(py2: str, repo: str, excludes: List[str]) -> dict:
    cmd = [py2] + _py2_flags() + [os.path.join(HERE, 'py2_scan.py'), repo]
    for e in excludes:
        cmd += ['--exclude', e]
    return run_json(cmd, cwd=HERE)


def probe(python: str, names: List[str], is_py2: bool) -> Dict[str, dict]:
    if not names:
        return {}
    flags = _py2_flags() if is_py2 else ['-I']
    # cwd=HERE keeps the target repo off sys.path; the probe drops its own dir.
    out: Dict[str, dict] = {}
    for i in range(0, len(names), 200):
        res = run_json([python] + flags + [os.path.join(HERE, 'py2_probe.py')] + names[i:i + 200], cwd=HERE)
        out.update(res['modules'])
    return out


def interpreter_version(python: str) -> str:
    p = subprocess.run([python, '-c', 'import sys; print(sys.version.split()[0])'],
                       capture_output=True, text=True, timeout=60)
    return (p.stdout or p.stderr).strip()


def _file_kind(rec: dict, imported_by: Set[str]) -> str:
    base = os.path.basename(rec['path'])
    if base == 'setup.py':
        return 'packaging'
    parts = rec['path'].split('/')
    if (rec.get('testcase_classes') or base.startswith(TEST_NAME_HINTS)
            or base.endswith('_test.py') or 'tests' in parts[:-1] or 'test' in parts[:-1]):
        return 'test'
    if rec.get('has_main') and not imported_by:
        return 'script'
    return 'library'


def analyze(repo: str, py2: str, py3: str, excludes: List[str]) -> dict:
    repo = os.path.abspath(repo)
    scan = scan_repo(py2, repo, excludes)
    files = {f['path']: f for f in scan['files']}
    issues: Dict[str, Issue] = {}

    def issue(kind, subject, severity, detail, where, hint=None):
        key = '%s:%s' % (kind, subject)
        it = issues.get(key)
        if it is None:
            it = issues[key] = Issue(key, kind, severity, subject, detail, hint)
        it.where.append(where)

    # dotted module -> path (a module name can exist under two import roots)
    index: Dict[str, str] = {}
    for path, rec in sorted(files.items()):
        if rec['module'] in index and index[rec['module']] != path:
            issue('module_name_collision', rec['module'], 'warning',
                  'two files define module %r: %s and %s; imports resolve to the first'
                  % (rec['module'], index[rec['module']], path), (path, 1))
            continue
        index[rec['module']] = path

    def package_of(rec: dict) -> str:
        m = rec['module']
        return m if rec['is_package_init'] else m.rpartition('.')[0]

    def internal(name: str) -> Optional[str]:
        return index.get(name)

    edges: Dict[str, Set[str]] = {p: set() for p in files}
    edge_info: Dict[str, List[dict]] = {p: [] for p in files}
    external: Dict[str, List[Tuple[str, int, str]]] = {}   # name -> [(file, line, context)]

    module_of = {p: r['module'] for p, r in files.items()}

    def add_edge(src, dst, line, how):
        if not dst or dst == src:
            return
        edges[src].add(dst)
        edge_info[src].append({'to': dst, 'line': line, 'how': how})
        # Importing pkg.mod runs pkg/__init__.py first. Only for importers
        # outside pkg: inside it, __init__ is already running.
        parts = module_of[dst].split('.')
        me = module_of[src]
        for k in range(1, len(parts)):
            anc = '.'.join(parts[:k])
            anc_path = internal(anc)
            if anc_path and anc_path not in (src, dst) and not (me == anc or me.startswith(anc + '.')):
                if anc_path not in edges[src]:
                    edges[src].add(anc_path)
                    edge_info[src].append({'to': anc_path, 'line': line, 'how': 'parent-package'})

    for path, rec in sorted(files.items()):
        if 'parse_error' in rec:
            issue('parse_error', path, 'needs_decision',
                  'does not parse as Python 2.7: %s. It may already be Python 3, be '
                  'generated, or be broken; its imports are unknown.' % rec['parse_error'],
                  (path, 1))
            continue
        pkg = package_of(rec)
        for imp in rec['imports']:
            line, ctx, mod = imp['line'], imp['context'], imp['module']
            if mod == '__future__':
                continue
            if imp['level']:
                base_parts = pkg.split('.') if pkg else []
                up = imp['level'] - 1
                if up > len(base_parts):
                    issue('unresolved_import', '.' * imp['level'] + mod, 'needs_decision',
                          'relative import goes above the top-level package', (path, line))
                    continue
                base = '.'.join(base_parts[:len(base_parts) - up] if up else base_parts)
                target = '.'.join(x for x in (base, mod) if x)
                hit = internal(target)
                add_edge(path, hit, line, 'explicit-relative')
                for n in imp['names']:
                    add_edge(path, internal('%s.%s' % (target, n) if target else n), line, 'explicit-relative')
                if not hit and not any(internal('%s.%s' % (target, n)) for n in imp['names']):
                    issue('unresolved_import', '.' * imp['level'] + mod, 'needs_decision',
                          'explicit relative import does not resolve to a file in the repo', (path, line))
                continue
            # Python 2 tries the importing file's own package first (implicit relative).
            hit, how = None, 'absolute'
            if pkg:
                cand = '%s.%s' % (pkg, mod)
                for k in range(len(cand.split('.')), len(pkg.split('.')), -1):
                    prefix = '.'.join(cand.split('.')[:k])
                    if internal(prefix):
                        hit, how = internal(prefix), 'implicit-relative'
                        break
            if not hit:
                parts = mod.split('.')
                for k in range(len(parts), 0, -1):
                    if internal('.'.join(parts[:k])):
                        hit = internal('.'.join(parts[:k]))
                        break
            if hit:
                add_edge(path, hit, line, how)
                if imp['kind'] == 'from':
                    base = '%s.%s' % (pkg, mod) if how == 'implicit-relative' else mod
                    for n in imp['names']:
                        add_edge(path, internal('%s.%s' % (base, n)), line, how)
                if how == 'implicit-relative':
                    issue('implicit_relative_import', '%s -> %s' % (path, hit), 'needs_decision',
                          'Python 2 implicit relative import of %r; Python 3 resolves it as '
                          'absolute and will not find it. Outside the catalogue.' % mod,
                          (path, line), hint='from . import %s' % mod.split('.')[0])
                continue
            external.setdefault(mod, []).append((path, line, ctx))

        for d in rec['dynamic']:
            arg = d.get('arg')
            target = internal(arg) if arg else None
            if target:
                add_edge(path, target, d['line'], 'dynamic:%s' % d['call'])
                issue('dynamic_import', '%s:%s' % (path, arg), 'info',
                      '%s(%r) resolves to %s; edge added' % (d['call'], arg, target), (path, d['line']))
            else:
                issue('dynamic_code', '%s:%s' % (path, d['call']), 'needs_decision',
                      '%s(...) with a non-literal or non-repo argument: the dependency '
                      'cannot be determined statically' % d['call'], (path, d['line']))

    # ---- external modules: ask both interpreters --------------------------------
    names = sorted(external)
    p2 = probe(py2, names, True)
    p3 = probe(py3, names, False)
    externals = []
    for name in names:
        top = name.split('.')[0]
        a, b = p2.get(name, {}), p3.get(name, {})
        uses = external[name]
        guarded = all(ctx in ('try', 'if', 'function') for _, _, ctx in uses)
        where = [(f, l) for f, l, _ in uses]
        if top in R11_RENAMES:
            cls, rule = 'renamed_stdlib', 'R11'
        elif a.get('stdlib') and b.get('available') and b.get('stdlib'):
            cls, rule = 'stdlib', None
        elif a.get('stdlib') and not b.get('available'):
            cls, rule = 'py2_only_stdlib', None
        elif a.get('available') and not a.get('stdlib'):
            cls, rule = ('third_party' if b.get('available') else 'third_party_missing_py3'), None
        elif not a.get('available') and b.get('stdlib'):
            cls, rule = 'py3_only_stdlib', None      # usually a try/except py3 branch
        elif not a.get('available'):
            cls, rule = 'unavailable', None
        else:
            cls, rule = 'stdlib', None
        entry = {'module': name, 'class': cls, 'rule': rule, 'guarded_import': guarded,
                 'py2': a, 'py3': b, 'used_by': sorted(set('%s:%d' % w for w in where))}
        externals.append(entry)

        if cls == 'renamed_stdlib':
            for w in where:
                issue('renamed_stdlib', top, 'info',
                      'R11 rename to %s. R16: must be migrated in every file in the same step.'
                      % R11_RENAMES[top], w)
        elif cls == 'stdlib' and top in REMOVED_AFTER_311:
            for w in where:
                issue('stdlib_removed_later', top, 'needs_decision',
                      'available in Python 3.11 but deprecated and removed in %s'
                      % REMOVED_AFTER_311[top], w)
        elif cls == 'py2_only_stdlib':
            for w in where:
                issue('py2_only_stdlib', top, 'info' if guarded else 'needs_decision',
                      'Python 2 stdlib module with no same-name Python 3 module; not covered '
                      'by the rule catalogue (only R11 renames are).' +
                      (' Import is guarded (try/if/function).' if guarded else ''),
                      w, hint=PY2_ONLY_HINTS.get(top) or PY2_ONLY_HINTS.get(name))
        elif cls == 'third_party_missing_py3':
            for w in where:
                issue('third_party_missing_py3', top, 'info' if guarded else 'needs_decision',
                      'third-party package installed for Python 2 (%s) but not importable by '
                      'the Python 3 interpreter' % a.get('origin'), w)
        elif cls == 'third_party':
            for w in where:
                issue('third_party', top, 'info', 'third-party package available in both '
                      'interpreters; check it is a Python-3-compatible release', w)
        elif cls == 'unavailable':
            for w in where:
                issue('unavailable_import', name, 'info' if guarded else 'needs_decision',
                      'not importable by either interpreter (missing dependency, optional '
                      'feature, or a module generated at build time)', w)

    # ---- non-code file dependencies ------------------------------------------
    file_deps: Dict[str, List[dict]] = {p: [] for p in files}
    for path, rec in files.items():
        for ref in rec.get('file_refs', []):
            target = ref.get('path')
            if not target:
                continue
            base = os.path.dirname(os.path.join(repo, path))
            exists = (os.path.exists(os.path.join(base, target))
                      or os.path.exists(os.path.join(repo, target)))
            file_deps[path].append({'path': target, 'line': ref['line'], 'call': ref['call'],
                                    'exists': exists})
            if not exists:
                issue('missing_file_ref', '%s:%s' % (path, target), 'warning',
                      '%s(%r) refers to a file that is not in the repository' % (ref['call'], target),
                      (path, ref['line']))

    # ---- order -------------------------------------------------------------------
    imported_by: Dict[str, Set[str]] = {p: set() for p in files}
    for a, bs in edges.items():
        for b in bs:
            imported_by[b].add(a)
    kinds = {p: _file_kind(files[p], imported_by[p]) for p in files}
    rank = {'library': 0, 'script': 1, 'test': 2, 'packaging': 3}

    def tie_break(unit):
        return (min(rank[kinds[p]] for p in unit), min(p.count('/') for p in unit), unit)

    units = migration_units(files.keys(), edges, tie_break)
    for u in units:
        if len(u) > 1:
            issue('import_cycle', ' <-> '.join(u), 'info',
                  'R29: these files import each other and must be migrated and verified as one unit',
                  (u[0], 1))

    # transitive importers, for "which tests cover this file"
    def importers_closure(p):
        seen, todo = set(), [p]
        while todo:
            for q in imported_by[todo.pop()]:
                if q not in seen:
                    seen.add(q)
                    todo.append(q)
        return seen

    file_entries = []
    for order, unit in enumerate(units, 1):
        for p in unit:
            rec = files[p]
            vcmd, basis, needs_char = default_verification(p, rec, kinds, importers_closure(p), files)
            file_entries.append({
                'path': p, 'module': rec['module'], 'kind': kinds[p], 'unit': order,
                'cycle': len(unit) > 1, 'lines': rec.get('lines'),
                'depends_on': sorted(edges[p]), 'imported_by': sorted(imported_by[p]),
                'import_edges': sorted(edge_info[p], key=lambda e: (e['line'], e['to'])),
                'external_imports': sorted(set(m for m, us in external.items()
                                               for f, _, _ in us if f == p)),
                'file_dependencies': file_deps[p],
                'verification_command': vcmd, 'verification_basis': basis,
                'needs_characterization_tests': needs_char,
                'parse_error': rec.get('parse_error'),
            })

    return {
        'repo': repo,
        'py2_scan_python': scan['python'],
        'excludes': excludes,
        'package_dirs': scan['package_dirs'],
        'files': file_entries,
        'units': [{'unit': i, 'files': u, 'cycle': len(u) > 1} for i, u in enumerate(units, 1)],
        'external_modules': externals,
        'issues': [i.as_dict() for i in sorted(issues.values(), key=lambda i: (
            {'needs_decision': 0, 'warning': 1, 'info': 2}[i.severity], i.key))],
    }


def default_verification(path, rec, kinds, importers, files):
    """Pick the command that proves this file under Python 3.

    Placeholders {PY2}, {PY3}, {TMP} are filled in by the Verification Gate.
    """
    kind = kinds[path]
    if kind == 'packaging':
        return ('{PY3} setup.py build --build-base {TMP}/build', 'packaging build', False)
    if kind == 'test':
        if rec.get('has_main'):
            return ('{PY3} %s' % path, 'test file runs itself', False)
        return ('{PY3} -m unittest %s' % rec['module'], 'unittest discovery of this module', False)
    tests = sorted(q for q in importers if kinds[q] == 'test')
    if tests:
        cmds = [('{PY3} %s' % q) if files[q].get('has_main') else ('{PY3} -m unittest %s' % files[q]['module'])
                for q in tests]
        return (' && '.join(cmds), 'tests that import it: %s' % ', '.join(tests), False)
    return ('{PY3} -m py_compile %s' % path,
            'no test imports this file: compile check only, characterization tests required', True)
