"""Compare Python 2 and Python 3 recordings (runs on the agent host).

Equivalence rules, and nothing else:
  - a Py2 `str` equals Py3 `str` or `bytes` with the same bytes (Py2 str is
    both text and bytes; R6), and Py2 `unicode` equals Py3 `str`;
  - long and int are one type (already one "int" in the recording; R8);
  - containers compare element by element (unordered ones are pre-sorted);
  - an exception equals an exception of the same type;
  - printed output must match exactly.
A Py2 list against a Py3 iterator or view is a difference: the caller could
index it in Py2.
"""
from __future__ import annotations

from typing import Optional, Tuple

STRISH = ('text', 'str2', 'bytes')


def same(a, b, path: str = '') -> Tuple[bool, Optional[str]]:
    if isinstance(a, dict) and isinstance(b, dict) and a.get('t') in STRISH and b.get('t') in STRISH:
        if a['t'] == 'text' and b['t'] == 'bytes':
            return False, '%s: Py2 text, Py3 bytes' % (path or 'value')
        if a['hex'] != b['hex']:
            return False, '%s: string differs' % (path or 'value')
        return True, None
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return (a == b, None if a == b else '%s: %r != %r' % (path or 'value', a, b))
    ta, tb = a.get('t'), b.get('t')
    if ta != tb:
        return False, '%s: Py2 %s, Py3 %s' % (path or 'value', _kind(a), _kind(b))
    if ta == 'exc':
        return (a['type'] == b['type'],
                None if a['type'] == b['type'] else '%s: raised %s vs %s' % (path or 'value', a['type'], b['type']))
    if ta in ('list', 'tuple', 'set', 'view', 'iter', 'dict'):
        va, vb = a['v'], b['v']
        if len(va) != len(vb):
            return False, '%s: %d items vs %d' % (path or 'value', len(va), len(vb))
        for i, (x, y) in enumerate(zip(va, vb)):
            ok, why = same(x, y, '%s[%d]' % (path, i))
            if not ok:
                return ok, why
        return True, None
    if ta == 'obj':
        for k in ('type', 'repr', 'str'):
            if a.get(k) != b.get(k):
                return False, '%s: %s differs' % (path or 'value', k)
        return True, None
    return (a == b, None if a == b else '%s: differs' % (path or 'value'))


def same_case(r2: dict, r3: dict) -> Tuple[bool, Optional[str]]:
    ok, why = same(r2.get('value'), r3.get('value'))
    if ok and r2.get('stdout', '') != r3.get('stdout', ''):
        return False, 'printed output differs'
    return ok, why


def show(v) -> str:
    """Short human-readable form of a recorded value."""
    if not isinstance(v, dict):
        return repr(v)
    t = v.get('t')
    if t in STRISH:
        raw = bytes.fromhex(v['hex'])
        try:
            txt = raw.decode('utf-8')
            return ('b%r' if t == 'bytes' else '%r') % (txt if t != 'bytes' else raw,)
        except UnicodeDecodeError:
            return 'bytes:%s' % v['hex'][:60]
    if t in ('int', 'float'):
        return v['v']
    if t == 'exc':
        return 'raises %s(%s)' % (v['type'], v.get('msg', '')[:80])
    if t == 'obj':
        return v.get('repr', '?')[:120]
    if t in ('list', 'tuple', 'set', 'view', 'iter', 'dict'):
        inner = ', '.join(show(x) if t != 'dict' else '%s: %s' % (show(x[0]), show(x[1])) for x in v['v'][:6])
        more = ', ...' if len(v['v']) > 6 else ''
        return '%s[%s%s]' % (t, inner, more)
    if t in ('type', 'callable'):
        return '<%s %s>' % (t, v.get('name'))
    return str(v)[:120]


def _kind(v) -> str:
    t = v.get('t')
    return {'exc': 'raised %s' % v.get('type'), 'obj': v.get('type', 'obj')}.get(t, t)
