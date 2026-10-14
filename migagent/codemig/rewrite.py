"""Apply chosen site options to the original bytes of a file (runs on the agent host).

An edit replaces the byte span [start, end) of the original with its parts:
literal text, or [a, b] slices of the original. Slices are rendered
recursively, so an edit nested inside a slice (an `L` literal inside a
print(...) wrapper) is applied too. Edits that overlap without nesting, or
that sit inside a span but outside every slice, are conflicts: the edit
would be lost, so rendering refuses instead.
"""
from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Sequence, Union

Part = Union[str, List[int]]


@dataclass
class Edit:
    site: str
    start: int
    end: int
    parts: Sequence[Part]


class Conflict(Exception):
    def __init__(self, sites: List[str], why: str):
        super().__init__('%s: %s' % (', '.join(sites), why))
        self.sites, self.why = sites, why


def render(src: bytes, edits: List[Edit], encoding: str = 'utf-8') -> bytes:
    used: Dict[str, bool] = {}
    out = _render(src, 0, len(src), sorted(edits, key=lambda e: (e.start, -e.end)), encoding, used)
    lost = [e.site for e in edits if e.site not in used]
    if lost:
        raise Conflict(lost, 'inside another rewrite but outside the text it keeps; '
                             'keep one of the two sites or pick another option')
    return out


def _render(src, lo, hi, edits, enc, used) -> bytes:
    inside = [e for e in edits if lo <= e.start and e.end <= hi]
    tops: List[Edit] = []
    for e in inside:
        if tops and e.start < tops[-1].end:
            if e.end <= tops[-1].end:
                continue                      # nested: rendered through the parent's slices
            raise Conflict([tops[-1].site, e.site], 'overlapping rewrites')
        tops.append(e)
    buf, pos = [], lo
    for e in tops:
        buf.append(src[pos:e.start])
        used[e.site] = True
        nested = [x for x in inside if x is not e and e.start <= x.start and x.end <= e.end]
        for p in e.parts:
            if isinstance(p, str):
                buf.append(p.encode(enc))
            else:
                buf.append(_render(src, p[0], p[1], nested, enc, used))
        pos = e.end
    buf.append(src[pos:hi])
    return b''.join(buf)


def unified_diff(path: str, before: bytes, after: bytes, encoding: str = 'utf-8', context: int = 1) -> str:
    a = before.decode(encoding, 'replace').splitlines(True)
    b = after.decode(encoding, 'replace').splitlines(True)
    return ''.join(difflib.unified_diff(a, b, 'a/' + path, 'b/' + path, n=context))


def py3_compile(py3: str, path: str, timeout: int = 120) -> dict:
    """Compile a file under Python 3 without writing .pyc files."""
    code = ('import sys\nsrc = open(sys.argv[1], "rb").read()\n'
            'try:\n    compile(src, sys.argv[1], "exec")\n'
            'except SyntaxError as e:\n'
            '    print("%s:%s: %s" % (type(e).__name__, e.lineno, e.msg)); sys.exit(1)\n')
    p = subprocess.run([py3, '-I', '-c', code, path], capture_output=True, text=True, timeout=timeout)
    return {'ok': p.returncode == 0, 'error': (p.stdout + p.stderr).strip()[-1500:] or None}
