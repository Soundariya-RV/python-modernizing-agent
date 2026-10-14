# -*- coding: utf-8 -*-
"""Evaluate characterization cases and record what each returns. Python 2.7 and 3.x.

    python charrun.py <tree> <cases.json> <out.json>

cases.json: {"module": "pkg.mod", "setup": ["net = mod.X(1)"], "cases": [{"id", "expr"}]}
Each case is one expression, evaluated after `import <module>` and the setup
lines. The result (or the exception type), plus anything printed, is
recorded in an interpreter-neutral form. Only the catalogue's
normalisations are applied: long and int are one type (R8), byte strings are
compared as hex (R6), and unordered containers are sorted.
"""
from __future__ import print_function

import binascii
import itertools
import json
import os
import re
import sys

PY2 = sys.version_info[0] == 2
if PY2:
    from StringIO import StringIO
    TEXT, INTS = unicode, (int, long)  # noqa: F821 (Python 2 names)
else:
    from io import StringIO
    TEXT, INTS = str, (int,)
ADDR = re.compile(r' at 0x[0-9a-fA-F]+')
LONG = re.compile(r'\b(\d+)L\b')
UPREFIX = re.compile(r'(?<![\w\'"])u(?=[\'"])')
VIEWS = ('dict_keys', 'dict_values', 'dict_items')


def clean(s):
    if not isinstance(s, TEXT):
        s = s.decode('utf-8', 'replace')
    return UPREFIX.sub('', LONG.sub(r'\1', ADDR.sub(' at 0x?', s)))


def neutral(v):
    """Sort key that is the same on both interpreters."""
    return json.dumps(v, sort_keys=True)


def hexs(b):
    return binascii.hexlify(b).decode('ascii')


def norm(v, depth=0):
    if depth > 8:
        return {'t': 'deep'}
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, INTS):
        return {'t': 'int', 'v': str(v)}
    if isinstance(v, float):
        return {'t': 'float', 'v': repr(v)}
    if isinstance(v, TEXT):
        return {'t': 'text', 'hex': hexs(v.encode('utf-8'))}
    if isinstance(v, bytes):                     # Python 2 str, Python 3 bytes
        return {'t': 'str2' if PY2 else 'bytes', 'hex': hexs(bytes(v))}
    if isinstance(v, (list, tuple)):
        return {'t': type(v).__name__ if type(v) in (list, tuple) else 'list',
                'v': [norm(x, depth + 1) for x in v[:200]]}
    if isinstance(v, dict):
        items = [[norm(k, depth + 1), norm(x, depth + 1)] for k, x in list(v.items())[:200]]
        return {'t': 'dict', 'v': sorted(items, key=neutral)}
    if isinstance(v, (set, frozenset)):
        return {'t': 'set', 'v': sorted([norm(x, depth + 1) for x in list(v)[:200]], key=neutral)}
    tname = type(v).__name__
    if tname in VIEWS:
        return {'t': 'view', 'v': sorted([norm(x, depth + 1) for x in list(v)[:200]], key=neutral)}
    if isinstance(v, type) or tname in ('classobj',):
        return {'t': 'type', 'name': v.__name__}
    if iter_like(v):
        return {'t': 'iter', 'v': [norm(x, depth + 1) for x in itertools.islice(v, 200)]}
    if callable(v):
        return {'t': 'callable', 'name': getattr(v, '__name__', tname)}
    rec = {'t': 'obj', 'type': tname}
    for k, f in (('repr', repr), ('str', str)):
        try:
            rec[k] = clean(f(v))[:500]
        except Exception as e:
            rec[k] = '<%s failed: %s>' % (k, type(e).__name__)
    return rec


def iter_like(v):
    try:
        return iter(v) is v
    except TypeError:
        return False


def main(argv):
    tree, cases_path, out_path = os.path.abspath(argv[1]), argv[2], argv[3]
    with open(cases_path) as f:
        spec = json.load(f)
    os.chdir(tree)
    sys.path.insert(0, tree)
    ns = {}
    res = {'python': sys.version.split()[0], 'module': spec['module'], 'setup_error': None, 'results': {}}
    try:
        exec(compile('import %s' % spec['module'], '<import>', 'exec'), ns)
        for line in spec.get('setup', []):
            exec(compile(line, '<setup>', 'exec'), ns)
    except BaseException as e:
        res['setup_error'] = '%s: %s' % (type(e).__name__, clean(str(e))[:300])
    if res['setup_error'] is None:
        for case in spec['cases']:
            buf = StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                rec = {'value': norm(eval(compile(case['expr'], '<case>', 'eval'), ns))}
            except BaseException as e:
                rec = {'value': {'t': 'exc', 'type': type(e).__name__, 'msg': clean(str(e))[:200]}}
            finally:
                sys.stdout = old
            printed = buf.getvalue()
            if printed:
                rec['stdout'] = clean(printed)[:1000]
            res['results'][case['id']] = rec
    with open(out_path, 'w') as f:
        json.dump(res, f)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
