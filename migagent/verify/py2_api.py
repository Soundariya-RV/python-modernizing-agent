# -*- coding: utf-8 -*-
"""List the public API of one Python 2 module: what characterization cases can call.

    python2.7 -E -s py2_api.py <file> [--dunders]

Also runs under Python 3 (for the migrated file). --dunders adds the special
methods of each class (__hex__, __nonzero__ ...) and the classes whose names
start with _ (public classes inherit from them), for the R31 API comparison.

Prints JSON {"api": [{"name", "kind", "line", "params", "required", "doc"}]}:
top-level functions and classes, and the public methods and properties of
each class. Parsed with the Python 2 AST, so it works on unmigrated code.
"""
from __future__ import print_function

import ast
import json
import sys


def _argname(x):
    if isinstance(x, str):                   # Python 2 vararg/kwarg
        return x
    if hasattr(x, 'arg'):                    # Python 3 ast.arg
        return x.arg
    return x.id if isinstance(x, ast.Name) else '(tuple)'


def params(fn, skip_self):
    a = fn.args
    names = [_argname(x) for x in getattr(a, 'posonlyargs', []) + a.args]
    if skip_self and names:
        names = names[1:]
    required = len(names) - len(a.defaults)
    if a.vararg:
        names.append('*' + _argname(a.vararg))
    for k in getattr(a, 'kwonlyargs', []):
        names.append(_argname(k) + '=')
    if a.kwarg:
        names.append('**' + _argname(a.kwarg))
    return names, max(required, 0)


def public(name, with_dunders):
    if name.startswith('__') and name.endswith('__'):
        return with_dunders
    return not name.startswith('_')


def doc(node):
    d = ast.get_docstring(node)
    return d.strip().splitlines()[0][:100] if d else ''


def main(argv):
    dunders = '--dunders' in argv
    with open(argv[1], 'rb') as f:
        tree = ast.parse(f.read())
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
            p, r = params(node, False)
            out.append({'name': node.name, 'kind': 'function', 'line': node.lineno, 'params': p,
                        'required': r, 'doc': doc(node)})
        elif isinstance(node, ast.ClassDef) and (dunders or not node.name.startswith('_')):
            init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == '__init__'), None)
            p, r = params(init, True) if init else ([], 0)
            out.append({'name': node.name, 'kind': 'class', 'line': node.lineno, 'params': p, 'required': r,
                        'doc': doc(node), 'bases': [getattr(b, 'id', getattr(b, 'attr', '?')) for b in node.bases]})
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and public(n.name, dunders and n.name != '__init__'):
                    deco = [getattr(d, 'id', getattr(d, 'attr', '')) for d in n.decorator_list]
                    kind = 'property' if 'property' in deco else 'method'
                    p, r = params(n, 'staticmethod' not in deco)
                    out.append({'name': '%s.%s' % (node.name, n.name), 'kind': kind, 'line': n.lineno,
                                'params': p, 'required': r, 'doc': doc(n)})
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if not name.startswith('_'):
                out.append({'name': name, 'kind': 'constant', 'line': node.lineno, 'params': [], 'required': 0,
                            'doc': ''})
    print(json.dumps({'api': out}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
