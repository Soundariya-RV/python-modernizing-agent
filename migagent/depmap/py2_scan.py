# -*- coding: utf-8 -*-
"""Static import/file-reference scanner. Runs under Python 2.7 (and 3.x).

The target repository is Python 2 source, which Python 3's `ast` cannot parse
(print statements, `except X, e`, backticks ...). So this script is executed
with $PY2_BIN and prints one JSON document on stdout.

    python2.7 py2_scan.py <repo-root> [--exclude DIR ...]

Facts only: nothing here guesses what an import means. Resolution and
classification happen in analyze.py.
"""
from __future__ import print_function

import ast
import json
import os
import sys

DEFAULT_EXCLUDES = set(['.git', '.hg', '.svn', '.tox', '.venv', 'venv', 'env',
                        'envs', 'build', 'dist', '__pycache__', '.eggs',
                        'node_modules'])
OPEN_FUNCS = set(['open', 'file', 'execfile'])
OPEN_ATTRS = set([('codecs', 'open'), ('io', 'open'), ('os.path', 'exists'),
                  ('os.path', 'isfile')])
DYNAMIC_FUNCS = set(['__import__'])
DYNAMIC_ATTRS = set([('importlib', 'import_module'), ('imp', 'load_source'),
                     ('imp', 'load_module'), ('imp', 'find_module')])


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return None


def _str_value(node):
    s = getattr(ast, 'Str', None)
    if s is not None and isinstance(node, s):
        return node.s
    c = getattr(ast, 'Constant', None)
    if c is not None and isinstance(node, c) and isinstance(node.value, str):
        return node.value
    return None


class Scanner(ast.NodeVisitor):
    def __init__(self):
        self.imports = []
        self.dynamic = []
        self.file_refs = []
        self.has_main = False
        self.testcase_classes = []
        self._ctx = []          # stack of 'function' | 'try' | 'if' | 'class'

    def _context(self):
        # The innermost interesting context decides how an import is labelled.
        for c in reversed(self._ctx):
            if c in ('function', 'try', 'if'):
                return c
        return 'top'

    def _push(self, kind, node):
        self._ctx.append(kind)
        self.generic_visit(node)
        self._ctx.pop()

    def visit_FunctionDef(self, node):
        self._push('function', node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_ClassDef(self, node):
        for b in node.bases:
            name = _dotted(b) or ''
            if name.endswith('TestCase'):
                self.testcase_classes.append(node.name)
        self._push('class', node)

    def visit_If(self, node):
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == '__name__'):
            self.has_main = True
        self._push('if', node)

    def visit_TryExcept(self, node):          # Python 2
        self._push('try', node)

    visit_Try = visit_TryExcept               # Python 3

    def visit_Import(self, node):
        for a in node.names:
            self.imports.append({
                'kind': 'import', 'module': a.name, 'names': [],
                'alias': a.asname, 'level': 0, 'line': node.lineno,
                'context': self._context()})

    def visit_ImportFrom(self, node):
        self.imports.append({
            'kind': 'from', 'module': node.module or '',
            'names': [a.name for a in node.names], 'alias': None,
            'level': node.level or 0, 'line': node.lineno,
            'context': self._context()})

    def visit_Exec(self, node):               # Python 2 `exec` statement
        self.dynamic.append({'call': 'exec', 'arg': None, 'line': node.lineno})
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _dotted(node.func)
        arg = _str_value(node.args[0]) if node.args else None
        if name in DYNAMIC_FUNCS or (name and tuple(name.rsplit('.', 1)) in DYNAMIC_ATTRS):
            self.dynamic.append({'call': name, 'arg': arg, 'line': node.lineno})
        if name in ('exec', 'eval'):
            self.dynamic.append({'call': name, 'arg': None, 'line': node.lineno})
        if name in OPEN_FUNCS or (name and tuple(name.rsplit('.', 1)) in OPEN_ATTRS):
            self.file_refs.append({'call': name, 'path': arg, 'line': node.lineno})
        self.generic_visit(node)


def module_name(rel_path, package_dirs):
    """Dotted module name for a repo-relative path, honouring __init__.py."""
    parts = rel_path[:-3].split('/')
    dirs, leaf = parts[:-1], parts[-1]
    # Walk up from the file while each directory is a package; the import
    # root is the first directory that is not one.
    k = len(dirs)
    while k > 0 and '/'.join(dirs[:k]) in package_dirs:
        k -= 1
    pkg = dirs[k:]
    mod = pkg if leaf == '__init__' else pkg + [leaf]
    return '.'.join(mod), '/'.join(dirs[:k])


def scan(root, excludes):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, '/')
        rel_dir = '' if rel_dir == '.' else rel_dir
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in DEFAULT_EXCLUDES and not d.startswith('.')
            and (rel_dir + '/' + d).lstrip('/') not in excludes)
        for f in sorted(filenames):
            if f.endswith('.py'):
                files.append((rel_dir + '/' + f).lstrip('/'))
    package_dirs = set(p[:-len('/__init__.py')] for p in files
                       if p.endswith('/__init__.py'))
    out = []
    for rel in files:
        with open(os.path.join(root, rel), 'rb') as fh:
            src = fh.read()
        mod, import_root = module_name(rel, package_dirs)
        rec = {'path': rel, 'module': mod, 'import_root': import_root,
               'is_package_init': rel.endswith('__init__.py'),
               'lines': src.count(b'\n') + 1}
        try:
            tree = ast.parse(src, rel)
        except SyntaxError as e:
            rec['parse_error'] = '%s (line %s)' % (e.msg, e.lineno)
            out.append(rec)
            continue
        s = Scanner()
        s.visit(tree)
        rec.update(imports=s.imports, dynamic=s.dynamic, file_refs=s.file_refs,
                   has_main=s.has_main, testcase_classes=s.testcase_classes)
        out.append(rec)
    return {'root': os.path.abspath(root), 'python': sys.version.split()[0],
            'package_dirs': sorted(package_dirs), 'files': out}


def main(argv):
    root = argv[1]
    excludes = set()
    i = 2
    while i < len(argv):
        if argv[i] == '--exclude':
            excludes.add(argv[i + 1].strip('/').replace('\\', '/'))
            i += 2
        else:
            i += 1
    json.dump(scan(root, excludes), sys.stdout, indent=1, sort_keys=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
