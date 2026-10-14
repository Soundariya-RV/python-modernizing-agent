# -*- coding: utf-8 -*-
"""Record the runtime operand types of every `/` in one module. Runs under Python 2.7.

    python2.7 -E -s py2_divprobe.py <repo> <target.py> <out.json> <runner> [args...]

<runner> is what exercises the target: a script path (run as __main__) or
`-m module`. The target's `/` and `/=` are rewritten in the AST to call a
recorder that computes the same Python 2 result, so behaviour is unchanged.
If the target is the runner script itself, it is instrumented directly.

Writes {"lines": {line: {"count", "types": {"int/int": n}, "inexact", "negative",
"errors"}}, "runner", "exit"} to out.json. Evidence is per line: a line with
two divisions reports both together.
"""
from __future__ import print_function

import ast
import imp
import json
import os
import runpy
import sys

try:
    import __builtin__ as builtins
except ImportError:            # not expected: this file runs under Python 2
    import builtins

INTS = (int, long)  # noqa: F821 (Python 2)
RECORD = {}


def _rec(line, a, b, r=None, err=None):
    e = RECORD.setdefault(line, {'count': 0, 'types': {}, 'inexact': 0, 'negative': 0, 'errors': {}})
    e['count'] += 1
    k = '%s/%s' % (type(a).__name__, type(b).__name__)
    e['types'][k] = e['types'].get(k, 0) + 1
    if err is not None:
        e['errors'][err] = e['errors'].get(err, 0) + 1
    elif isinstance(a, INTS) and isinstance(b, INTS) and not isinstance(a, bool):
        if a % b:
            e['inexact'] += 1
        if (a < 0) != (b < 0):
            e['negative'] += 1


def divprobe(a, b, line):
    try:
        r = a / b                  # classic division: this module has no __future__ import
    except Exception as exc:
        _rec(line, a, b, err=type(exc).__name__)
        raise
    _rec(line, a, b, r)
    return r


class Instrument(ast.NodeTransformer):
    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Div):
            call = ast.Call(func=ast.Name(id='__divprobe__', ctx=ast.Load()),
                            args=[node.left, node.right, ast.Num(n=node.lineno)],
                            keywords=[], starargs=None, kwargs=None)
            return ast.copy_location(call, node)
        return node

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Div) and isinstance(node.target, ast.Name):
            load = ast.copy_location(ast.Name(id=node.target.id, ctx=ast.Load()), node)
            call = ast.Call(func=ast.Name(id='__divprobe__', ctx=ast.Load()),
                            args=[load, node.value, ast.Num(n=node.lineno)],
                            keywords=[], starargs=None, kwargs=None)
            new = ast.Assign(targets=[node.target], value=ast.copy_location(call, node))
            return ast.copy_location(new, node)
        return node


def instrumented_code(path):
    with open(path, 'rb') as f:
        src = f.read()
    tree = Instrument().visit(ast.parse(src, path))
    ast.fix_missing_locations(tree)
    return compile(tree, path, 'exec', dont_inherit=True)


def module_name(repo, target):
    rel = os.path.relpath(target, repo).replace(os.sep, '/')[:-3]
    if rel.endswith('/__init__'):
        rel = rel[:-len('/__init__')]
    return rel.replace('/', '.')


class Hook(object):
    def __init__(self, name, path):
        self.name, self.path = name, path

    def find_module(self, fullname, path=None):
        return self if fullname == self.name else None

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        mod = imp.new_module(fullname)
        mod.__file__ = self.path
        mod.__loader__ = self
        if os.path.basename(self.path) == '__init__.py':
            mod.__path__ = [os.path.dirname(self.path)]
            mod.__package__ = fullname
        else:
            mod.__package__ = fullname.rpartition('.')[0]
        sys.modules[fullname] = mod
        try:
            exec(instrumented_code(self.path), mod.__dict__)
        except BaseException:
            del sys.modules[fullname]
            raise
        return mod


def main(argv):
    repo, target, out = os.path.abspath(argv[1]), os.path.abspath(argv[2]), argv[3]
    runner = argv[4:]
    builtins.__divprobe__ = divprobe
    os.chdir(repo)
    code = 0
    try:
        if runner[:1] == ['-m']:
            sys.argv = [runner[1]] + runner[2:]
            sys.path.insert(0, repo)
            sys.meta_path.insert(0, Hook(module_name(repo, target), target))
            runpy.run_module(runner[1], run_name='__main__', alter_sys=True)
        else:
            script = os.path.abspath(runner[0])
            sys.argv = [script] + runner[1:]
            sys.path.insert(0, os.path.dirname(script))
            if script == target:
                mod = imp.new_module('__main__')
                mod.__file__ = script
                sys.modules['__main__'] = mod
                exec(instrumented_code(script), mod.__dict__)
            else:
                sys.meta_path.insert(0, Hook(module_name(repo, target), target))
                runpy.run_path(script, run_name='__main__')
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, (int, bool)) else (0 if e.code is None else 1)
    except BaseException as e:
        code = '%s: %s' % (type(e).__name__, e)
    finally:
        with open(out, 'w') as f:
            json.dump({'runner': runner, 'exit': code,
                       'lines': dict((str(k), v) for k, v in RECORD.items())}, f)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
