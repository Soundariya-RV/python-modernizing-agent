# -*- coding: utf-8 -*-
"""Find rule_catalogue.md sites in one Python 2 file. Runs under Python 2.7.

    python2.7 -E -s py2_sites.py <file>              every site, with candidate rewrites
    python2.7 -E -s py2_sites.py <file> --residual   Python-2-only constructs still present

Prints one JSON document on stdout. Facts and candidate rewrites only: which
option is applied is decided by the tools (mechanical sites) or by the model.

A site:
  id         S001, S002 ... (stable for one version of the file)
  rule       R1..R13, or OUT (a Python 2 construct outside the catalogue)
  kind       short name: xrange, has_key, long-in-tuple, raise-comma, ...
  line       1-based line of the construct
  start/end  byte offsets of the span in the file
  before     source text of the span
  options    [{id, parts, text, note}]: parts are literal strings and
             [start, end] byte slices of the original, so a wrapper such as
             print(...) or `k in d` keeps the rewrites nested inside it
  default    option the tools apply unless told otherwise, or null
  judgement  true when the right option depends on what the code means
  why        what to check (the catalogue's "Watch for")
  key        pattern-level decision key when the answer does not depend on the
             site (decisions.md reuses it in every file), else null
"""
from __future__ import print_function

import ast
import io
import json
import keyword
import re
import sys
import tokenize

NAME, OP, NUMBER, STRING = tokenize.NAME, tokenize.OP, tokenize.NUMBER, tokenize.STRING
NEWLINE, NL, COMMENT = tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT
INDENT, DEDENT, ENDMARKER = tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER
OPENERS = {'(': ')', '[': ']', '{': '}'}
CLOSERS = {')': '(', ']': '[', '}': '{'}
TIGHT_OPS = set(['+', '-', '*', '/', '//', '%', '**', '<<', '>>', '&', '|', '^', '~',
                 '==', '!=', '<>', '<', '>', '<=', '>='])
LOOSE_NAMES = set(['and', 'or', 'not', 'if', 'else', 'lambda', 'in', 'is'])
SAFE_FUNCS = set(['list', 'tuple', 'set', 'frozenset', 'sorted', 'len', 'sum', 'min', 'max',
                  'any', 'all', 'enumerate', 'reversed', 'zip', 'map', 'filter', 'dict', 'iter'])
SAFE_METHODS = set(['join', 'extend', 'update', 'union', 'intersection', 'difference',
                    'issubset', 'issuperset', 'fromkeys', 'writelines'])
RENAMED = {'ConfigParser': 'configparser', 'Queue': 'queue', 'cStringIO': 'io',
           'StringIO': 'io', 'urllib2': 'urllib.request'}
URLLIB2_ERROR = set(['URLError', 'HTTPError'])
URLLIB2_PARSE = set(['quote', 'unquote', 'urlparse', 'splittype', 'splithost', 'splituser',
                     'splitpasswd', 'splitport', 'splitattr', 'splitquery', 'splittag',
                     'splitvalue', 'unwrap', 'urljoin'])
URLLIB2_REQUEST = set([
    'urlopen', 'install_opener', 'build_opener', 'Request', 'OpenerDirector', 'BaseHandler',
    'HTTPDefaultErrorHandler', 'HTTPRedirectHandler', 'HTTPCookieProcessor', 'ProxyHandler',
    'HTTPPasswordMgr', 'HTTPPasswordMgrWithDefaultRealm', 'AbstractBasicAuthHandler',
    'HTTPBasicAuthHandler', 'ProxyBasicAuthHandler', 'AbstractDigestAuthHandler',
    'HTTPDigestAuthHandler', 'ProxyDigestAuthHandler', 'AbstractHTTPHandler', 'HTTPHandler',
    'HTTPSHandler', 'FileHandler', 'FTPHandler', 'CacheFTPHandler', 'UnknownHandler',
    'HTTPErrorProcessor', 'parse_http_list', 'parse_keqv_list', 'getproxies', 'url2pathname',
    'pathname2url', 'localhost'])
DUNDERS = {   # name: (py3 rename or None, py3 never calls it)
    '__nonzero__': ('__bool__', False), '__unicode__': ('__str__', False),
    '__hex__': (None, True), '__oct__': (None, True), '__long__': (None, True),
    '__getslice__': (None, True), '__setslice__': (None, True), '__delslice__': (None, True),
    '__coerce__': (None, True), '__cmp__': (None, True)}
DIV_DUNDERS = {'__div__': '__truediv__', '__idiv__': '__itruediv__', '__rdiv__': '__rtruediv__'}
REMOVED_BUILTINS = {'unichr': 'chr', 'file': 'open', 'intern': 'sys.intern', 'reduce': None,
                    'cmp': None, 'apply': None, 'buffer': None, 'execfile': None,
                    'reload': None, 'coerce': None}
LAZY_BUILTINS = set(['map', 'filter', 'zip'])
MODULE_ATTRS = {
    ('sys', 'maxint'): 'sys.maxsize', ('os', 'getcwdu'): 'os.getcwd',
    ('string', 'letters'): 'string.ascii_letters', ('string', 'lowercase'): 'string.ascii_lowercase',
    ('string', 'uppercase'): 'string.ascii_uppercase', ('string', 'maketrans'): None,
    ('string', 'join'): None, ('string', 'split'): None, ('string', 'atoi'): None,
    ('string', 'atof'): None, ('string', 'atol'): None, ('string', 'replace'): None,
    ('string', 'upper'): None, ('string', 'lower'): None, ('string', 'strip'): None,
    ('string', 'find'): None, ('string', 'joinfields'): None, ('string', 'zfill'): None,
    ('itertools', 'izip'): 'zip', ('itertools', 'imap'): 'map', ('itertools', 'ifilter'): 'filter',
    ('itertools', 'izip_longest'): 'itertools.zip_longest',
    ('itertools', 'ifilterfalse'): 'itertools.filterfalse',
    ('sys', 'exc_clear'): None, ('sys', 'exc_type'): None, ('sys', 'exc_value'): None,
    ('sys', 'exc_traceback'): None,
    ('types', 'IntType'): 'int', ('types', 'LongType'): 'int', ('types', 'FloatType'): 'float',
    ('types', 'ListType'): 'list', ('types', 'DictType'): 'dict', ('types', 'TupleType'): 'tuple',
    ('types', 'BooleanType'): 'bool', ('types', 'StringType'): None, ('types', 'UnicodeType'): None,
    ('types', 'StringTypes'): None, ('types', 'ClassType'): None, ('types', 'InstanceType'): None}
VIEW_METHODS = {'viewkeys': 'keys', 'viewitems': 'items', 'viewvalues': 'values'}


class Tok(object):
    __slots__ = ('type', 's', 'row', 'col', 'erow', 'ecol', 'start', 'end')


def coding_of(src):
    for line in src.splitlines()[:2]:
        m = re.search(br'coding[:=]\s*([-\w.]+)', line)
        if m:
            return m.group(1).decode('ascii')
    return 'utf-8'


class File(object):
    def __init__(self, src):
        self.src = src
        self.enc = coding_of(src)
        lines = io.BytesIO(src).readlines()
        self.starts = [0]
        for l in lines:
            self.starts.append(self.starts[-1] + len(l))
        self.toks = []
        for t in tokenize.generate_tokens(io.BytesIO(src).readline):
            k = Tok()
            k.type, k.s = t[0], t[1]
            (k.row, k.col), (k.erow, k.ecol) = t[2], t[3]
            k.start = self.starts[min(k.row, len(self.starts)) - 1] + k.col
            k.end = self.starts[min(k.erow, len(self.starts)) - 1] + k.ecol
            self.toks.append(k)
        self.sig = [t for t in self.toks if t.type not in (NL, COMMENT)]
        self.at = {}
        self.match = {}
        stack = []
        for i, t in enumerate(self.sig):
            if t.type in (NAME, OP):
                self.at.setdefault((t.row, t.col), i)
            if t.type == OP and t.s in OPENERS:
                stack.append(i)
            elif t.type == OP and t.s in CLOSERS and stack:
                j = stack.pop()
                self.match[i], self.match[j] = j, i

    def text(self, a, b):
        return self.src[a:b].decode(self.enc, 'replace')

    def is_(self, i, s, typ=None):
        if i < 0 or i >= len(self.sig):
            return False
        t = self.sig[i]
        return t.s == s and (typ is None or t.type == typ)

    def stmt_end(self, i):
        """Index of the last token of the simple statement that contains token i."""
        depth, j = 0, i
        while j + 1 < len(self.sig):
            t = self.sig[j + 1]
            if t.type in (NEWLINE, ENDMARKER) or (depth == 0 and t.type == OP and t.s == ';'):
                return j
            if t.type == OP and t.s in OPENERS:
                depth += 1
            elif t.type == OP and t.s in CLOSERS:
                depth -= 1
            j += 1
        return j

    def split_top(self, a, b, sep=',', sep_type=OP):
        """Split tokens a..b (inclusive) at top-level separators: [(first, last), ...]."""
        parts, depth, cur = [], 0, a
        for j in range(a, b + 1):
            t = self.sig[j]
            if t.type == OP and t.s in OPENERS:
                depth += 1
            elif t.type == OP and t.s in CLOSERS:
                depth -= 1
            elif depth == 0 and t.type == sep_type and t.s == sep:
                parts.append((cur, j - 1))
                cur = j + 1
        parts.append((cur, b))
        return parts

    def span(self, a, b):
        return [self.sig[a].start, self.sig[b].end]

    def primary_start(self, j):
        """First token of the primary expression that ends at token j (`a.b(c)[d]`)."""
        while j >= 0:
            t = self.sig[j]
            if t.type == OP and t.s in CLOSERS and j in self.match:
                j = self.match[j]
                p = j - 1
                if p >= 0 and ((self.sig[p].type == NAME and not keyword.iskeyword(self.sig[p].s))
                               or (self.sig[p].type == OP and self.sig[p].s in (')', ']'))
                               or self.sig[p].type == STRING):
                    j = p
                    continue
                return j
            if t.type in (NAME, NUMBER, STRING) and not (t.type == NAME and keyword.iskeyword(t.s)):
                p = j - 1
                if self.is_(p, '.', OP):
                    j = p - 1
                    continue
                if t.type == STRING and p >= 0 and self.sig[p].type == STRING:
                    j = p
                    continue
                return j
            return j + 1
        return 0

    def enclosing_open(self, i):
        depth, j = 0, i - 1
        while j >= 0:
            t = self.sig[j]
            if t.type in (NEWLINE, INDENT, DEDENT):
                return None
            if t.type == OP and t.s in CLOSERS:
                depth += 1
            elif t.type == OP and t.s in OPENERS:
                if depth == 0:
                    return j
                depth -= 1
            j -= 1
        return None

    def is_display(self, o):
        """Is the bracket at o a display/grouping, not a call or subscript?"""
        p = o - 1
        if p < 0:
            return True
        t = self.sig[p]
        if t.type == NAME and not keyword.iskeyword(t.s):
            return False
        if t.type == OP and t.s in (')', ']', '}'):
            return False
        return t.type != STRING


class Finder(object):
    def __init__(self, src):
        self.f = File(src)
        self.sites = []
        self.tree = ast.parse(src)
        self.parents = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.future = set()
        self.defined = set()
        self.plain_imports = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                self.future.update(a.name for a in node.names)
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                self.defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
                self.defined.add(node.id)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    self.defined.add(a.asname or a.name.split('.')[0])
                    if not a.asname:
                        self.plain_imports.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.defined.add(a.asname or a.name)
        self.names_at = {}
        self.attrs = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                self.names_at[(node.lineno, node.col_offset)] = node
            elif isinstance(node, ast.Attribute):
                self.attrs.setdefault(node.attr, []).append(node)
        self.import_spans = []

    # ------------------------------------------------------------ helpers
    def add(self, rule, kind, line, span, options, default, judgement=False, why='', key=None,
            extra=None):
        f = self.f
        opts = []
        for oid, parts, note in options:
            preview = ''.join(p if isinstance(p, type(u'')) or isinstance(p, str) else f.text(p[0], p[1])
                              for p in parts)
            opts.append({'id': oid, 'parts': [p if isinstance(p, list) else _u(p) for p in parts],
                         'text': _u(preview), 'note': note})
        site = {'id': 'S%03d' % (len(self.sites) + 1), 'rule': rule, 'kind': kind, 'line': line,
                'start': span[0], 'end': span[1], 'before': f.text(span[0], span[1]),
                'options': opts, 'default': default, 'judgement': bool(judgement), 'why': why,
                'key': key}
        if extra:
            site.update(extra)
        self.sites.append(site)
        return site

    def call_of(self, node):
        p = self.parents.get(node)
        return p if isinstance(p, ast.Call) and p.func is node else None

    def attr_node(self, attr, tok):
        best = None
        for n in self.attrs.get(attr, []):
            if (n.lineno, n.col_offset) <= (tok.row, tok.col):
                if best is None or (n.lineno, n.col_offset) > (best.lineno, best.col_offset):
                    best = n
        return best

    def iteration_context(self, node):
        p = self.parents.get(node)
        if isinstance(p, ast.For) and p.iter is node:
            return 'for-loop'
        if isinstance(p, ast.comprehension) and p.iter is node:
            return 'comprehension'
        if isinstance(p, ast.Call) and any(a is node for a in p.args):
            fn = p.func
            if isinstance(fn, ast.Name) and fn.id in SAFE_FUNCS:
                return 'argument of %s()' % fn.id
            if isinstance(fn, ast.Attribute) and fn.attr in SAFE_METHODS:
                return 'argument of .%s()' % fn.attr
        if isinstance(p, ast.Compare):
            for op, c in zip(p.ops, p.comparators):
                if c is node and isinstance(op, (ast.In, ast.NotIn)):
                    return 'membership test'
        return None

    def describe_use(self, node):
        p = self.parents.get(node)
        if isinstance(p, ast.Subscript):
            return 'indexed or sliced'
        if isinstance(p, ast.BinOp):
            return 'operand of %s' % type(p.op).__name__
        if isinstance(p, ast.Attribute):
            return 'method .%s() called on it' % p.attr
        if isinstance(p, (ast.Assign, ast.AugAssign)):
            return 'assigned to a variable (check how the variable is used later)'
        if isinstance(p, ast.Return):
            return 'returned to the caller'
        if isinstance(p, ast.Compare):
            return 'compared'
        if isinstance(p, ast.Call):
            fn = p.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', '?')
            return 'passed to %s()' % name
        return 'used as %s' % type(p).__name__ if p is not None else 'top level'

    def in_function(self, node):
        p = self.parents.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.Lambda)):
                return True
            p = self.parents.get(p)
        return False

    def tok_index(self, node, s=None):
        i = self.f.at.get((node.lineno, node.col_offset))
        if i is not None and (s is None or self.f.sig[i].s == s):
            return i
        if s is not None:      # decorated defs: lineno is the decorator's line
            for j in range(i or 0, len(self.f.sig)):
                t = self.f.sig[j]
                if (t.row, t.col) >= (node.lineno, node.col_offset) and t.s == s:
                    return j
        return i

    def in_import(self, tok):
        return any(a <= tok.start < b for a, b in self.import_spans)

    def wrap_if_loose(self, a, b):
        """Parts for tokens a..b, parenthesised if they contain a low-precedence operator."""
        f = self.f
        depth = 0
        for j in range(a, b + 1):
            t = f.sig[j]
            if t.type == OP and t.s in OPENERS:
                depth += 1
            elif t.type == OP and t.s in CLOSERS:
                depth -= 1
            elif depth == 0 and ((t.type == NAME and t.s in LOOSE_NAMES) or
                                 (t.type == OP and t.s in ('==', '!=', '<>', '<', '>', '<=', '>='))):
                return ['(', f.span(a, b), ')']
        return [f.span(a, b)]

    # ------------------------------------------------------------ run
    def run(self):
        self.find_imports()          # R11 first: it records import spans used by later scans
        self.find_statements()       # R1 R4 R12 OUT:raise
        self.find_tokens()           # R2 R3 R5 R6 R7 R8 R9 R10 R13 OUT
        self.find_defs()             # dunders, tuple params, next(), __metaclass__
        self.sites.sort(key=lambda s: (s['start'], -s['end']))
        for n, s in enumerate(self.sites):
            s['id'] = 'S%03d' % (n + 1)
        return self.sites

    # R11 --------------------------------------------------------------------
    def find_imports(self):
        f = self.f
        uses = {}               # module -> [(i_mod, i_attr)]
        for i, t in enumerate(f.sig):
            if (t.type == NAME and t.s in RENAMED and t.s in self.plain_imports
                    and not f.is_(i - 1, '.', OP) and f.is_(i + 1, '.', OP)
                    and i + 2 < len(f.sig) and f.sig[i + 2].type == NAME):
                uses.setdefault(t.s, []).append((i, i + 2))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                i = self.tok_index(node, 'import')
                if i is None:
                    continue
                e = f.stmt_end(i)
                self.import_spans.append((f.sig[i].start, f.sig[e].end))
                for a in node.names:
                    top = a.name
                    if top not in RENAMED:
                        continue
                    j = next((k for k in range(i + 1, e + 1) if f.sig[k].s == top), None)
                    if j is None:
                        continue
                    self.import_site(top, j, a.asname, uses.get(top, []))
            elif isinstance(node, ast.ImportFrom) and node.module in RENAMED and not node.level:
                i = self.tok_index(node, 'from')
                if i is None:
                    continue
                e = f.stmt_end(i)
                self.import_spans.append((f.sig[i].start, f.sig[e].end))
                self.from_import_site(node, i, e)
        for mod, lst in sorted(uses.items()):
            for im, ia in lst:
                if self.in_import(f.sig[im]):
                    continue
                attr = f.sig[ia].s
                span = [f.sig[im].start, f.sig[ia].end]
                line = f.sig[im].row
                if mod in ('ConfigParser', 'Queue'):
                    self.add('R11', 'renamed-module-use', line, [f.sig[im].start, f.sig[im].end],
                             [('py3', [RENAMED[mod]], '')], 'py3', key='R11:%s' % mod)
                elif mod in ('cStringIO', 'StringIO'):
                    if attr == 'StringIO':
                        self.add('R11', 'stringio-class', line, span,
                                 [('text', ['io.StringIO'], 'holds text (unicode)'),
                                  ('bytes', ['io.BytesIO'], 'holds bytes (R6)')], 'text', judgement=True,
                                 why='%s.StringIO held Py2 str. Text -> io.StringIO; bytes (packed data, '
                                     'socket or file "rb" data) -> io.BytesIO.' % mod)
                    else:
                        self.add('R11', 'stringio-attr', line, span, [], None, judgement=True,
                                 why='%s.%s has no io equivalent in the catalogue mapping' % (mod, attr))
                else:   # urllib2
                    target = self.urllib2_target(attr)
                    if target:
                        self.add('R11', 'urllib2-attr', line, span, [('py3', ['%s.%s' % (target, attr)], '')],
                                 'py3', key='R11:urllib2.%s' % attr)
                    else:
                        self.add('R11', 'urllib2-attr', line, span, [], None, judgement=True,
                                 why='urllib2.%s is not in the urllib.request/error/parse mapping' % attr)

    @staticmethod
    def urllib2_target(attr):
        if attr in URLLIB2_ERROR:
            return 'urllib.error'
        if attr in URLLIB2_PARSE:
            return 'urllib.parse'
        if attr in URLLIB2_REQUEST:
            return 'urllib.request'
        return None

    def import_site(self, mod, j, asname, uses):
        f = self.f
        t = f.sig[j]
        span = [t.start, t.end]
        if mod in ('ConfigParser', 'Queue'):
            self.add('R11', 'renamed-import', t.row, span, [('py3', [RENAMED[mod]], '')], 'py3',
                     key='R11:%s' % mod)
        elif mod in ('cStringIO', 'StringIO'):
            self.add('R11', 'renamed-import', t.row, span, [('py3', ['io'], '')], 'py3',
                     judgement=bool(asname),
                     why=('imported as %s: check whether it holds text (io.StringIO) or bytes '
                          '(io.BytesIO); an alias cannot express BytesIO' % asname) if asname else '',
                     key=None if asname else 'R11:%s' % mod)
        else:   # urllib2
            if asname:
                self.add('R11', 'renamed-import', t.row, span, [('py3', ['urllib.request'], '')], 'py3',
                         judgement=True, why='urllib2 imported as %s: urllib.request re-exports '
                         'URLError/HTTPError and quote/unquote, but check each attribute use' % asname)
                return
            need = ['urllib.request']
            for im, ia in uses:
                tgt = self.urllib2_target(f.sig[ia].s)
                if tgt and tgt not in need:
                    need.append(tgt)
            self.add('R11', 'renamed-import', t.row, span, [('py3', [', '.join(need)], '')], 'py3',
                     key='R11:urllib2')

    def from_import_site(self, node, i, e):
        f = self.f
        mod = node.module
        span = f.span(i, e)
        line = f.sig[i].row
        names = [(a.name, a.asname) for a in node.names]

        def alias(n, a):
            return '%s as %s' % (n, a) if a else n
        if mod in ('ConfigParser', 'Queue'):
            j = i + 1
            self.add('R11', 'renamed-from-import', line, [f.sig[j].start, f.sig[j].end],
                     [('py3', [RENAMED[mod]], '')], 'py3', key='R11:%s' % mod)
        elif mod in ('cStringIO', 'StringIO'):
            if any(n == '*' for n, _ in names):
                self.add('R11', 'renamed-from-import', line, span, [], None, judgement=True,
                         why='star import from %s' % mod)
                return
            text = 'from io import ' + ', '.join(alias(n, a) for n, a in names)
            byt = 'from io import ' + ', '.join(
                alias('BytesIO', a or n) if n == 'StringIO' else alias(n, a) for n, a in names)
            self.add('R11', 'renamed-from-import', line, span,
                     [('text', [text], 'holds text'), ('bytes', [byt], 'holds bytes (R6)')], 'text',
                     judgement=True, why='StringIO from %s held Py2 str: text -> io.StringIO, '
                     'bytes -> io.BytesIO' % mod)
        else:   # from urllib2 import ...
            groups, unknown = {}, []
            for n, a in names:
                tgt = self.urllib2_target(n)
                if tgt:
                    groups.setdefault(tgt, []).append(alias(n, a))
                else:
                    unknown.append(n)
            if unknown:
                self.add('R11', 'renamed-from-import', line, span, [], None, judgement=True,
                         why='from urllib2 import %s: not in the mapping' % ', '.join(unknown))
                return
            line_start = f.starts[f.sig[i].row - 1]
            indent = f.src[line_start:f.sig[i].start]
            sep = ('\n' + indent.decode(f.enc)) if not indent.strip() else '; '
            stmts = ['from %s import %s' % (m, ', '.join(groups[m])) for m in sorted(groups)]
            self.add('R11', 'renamed-from-import', line, span, [('py3', [sep.join(stmts)], '')], 'py3',
                     key='R11:urllib2')

    # R1 R4 R12 OUT:raise -------------------------------------------------------
    def find_statements(self):
        f = self.f
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Print) and 'print_function' not in self.future:
                self.print_site(node)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                self.except_site(node)
            elif isinstance(node, ast.Exec):
                self.exec_site(node)
            elif isinstance(node, ast.Raise) and node.inst is not None:
                self.raise_site(node)

    def print_site(self, node):
        f = self.f
        i = self.tok_index(node, 'print')
        if i is None or f.sig[i].s != 'print':
            return
        e = f.stmt_end(i)
        a = i + 1
        line = f.sig[i].row
        span = f.span(i, e)
        if a > e:
            self.add('R1', 'print', line, span, [('call', ['print()'], '')], 'call', key='R1:print')
            return
        dest = None
        if f.is_(a, '>>', OP):
            elems = f.split_top(a + 1, e)
            dest, vals = elems[0], elems[1:]
        else:
            vals = f.split_top(a, e)
        trailing = bool(vals) and vals[-1][0] > vals[-1][1]
        if trailing:
            vals = vals[:-1]
        if dest is None and not trailing and len(vals) == 1:
            x, y = vals[0]
            if f.is_(x, '(', OP) and f.match.get(x) == y:
                inner = f.split_top(x + 1, y - 1) if y > x + 1 else [(x + 1, x)]
                if len(inner) == 1:
                    return        # print (expr): already valid Python 3 with the same output
                self.add('R1', 'print-tuple', line, span, [('call', ['print(', f.span(x, y), ')'], '')],
                         'call', key='R1:print',
                         why='print (a, b) prints a tuple in Py2, so it becomes print((a, b))')
                return
        parts = ['print(']
        if vals:
            parts.append([f.sig[vals[0][0]].start, f.sig[vals[-1][1]].end])
        extras = []
        if trailing:
            extras.append(["end=' '"])
        if dest is not None:
            extras.append(['file=', f.span(dest[0], dest[1])])
        for n, x in enumerate(extras):
            if vals or n:
                parts.append(', ')
            parts.extend(x)
        parts.append(')')
        self.add('R1', 'print', line, span, [('call', parts, '')], 'call', key='R1:print',
                 why=("trailing comma: Py2 softspace never writes a trailing space at the end of the "
                      "output, end=' ' does; compare stdout byte for byte") if trailing else '')

    def except_site(self, node):
        f = self.f
        i = self.tok_index(node, 'except')
        if i is None:
            return
        depth, c = 0, None
        for j in range(i + 1, len(f.sig)):
            t = f.sig[j]
            if t.type == OP and t.s in OPENERS:
                depth += 1
            elif t.type == OP and t.s in CLOSERS:
                depth -= 1
            elif depth == 0 and t.type == OP and t.s == ',' and c is None:
                c = j
            elif depth == 0 and t.type == OP and t.s == ':':
                break
        if c is None:
            return      # already `except E as e`
        line = f.sig[i].row
        if not isinstance(node.name, ast.Name):
            self.add('OUT', 'except-unpack', line, [f.sig[c].start, f.sig[c + 1].start], [], None,
                     judgement=True, why='except E, (a, b): tuple unpacking of the exception has no '
                     'Python 3 form', key='OUT:except-unpack')
            return
        name = node.name.id
        scope = self.parents.get(node)
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.Module)):
            scope = self.parents.get(scope)
        inside = set(ast.walk(node))
        later = sorted(n.lineno for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == name
                       and n not in inside and n.lineno > node.lineno)
        why = ''
        if later:
            why = ('%s is used again at line %d, after the except block; Python 3 deletes it at the end '
                   'of the block' % (name, later[0]))
        self.add('R4', 'except-comma', line, [f.sig[c].start, f.sig[c + 1].start], [('as', [' as '], '')],
                 'as', judgement=bool(later), why=why, key=None if later else 'R4:except-comma')

    def exec_site(self, node):
        f = self.f
        i = self.tok_index(node, 'exec')
        if i is None:
            return
        e = f.stmt_end(i)
        a = i + 1
        if a > e or (f.is_(a, '(', OP) and f.match.get(a) == e):
            return        # exec(code) is already the function form
        parts_in = f.split_top(a, e, 'in', NAME)
        body = parts_in[0]
        parts = ['exec(', f.span(body[0], body[1])]
        if len(parts_in) > 1:
            rest = (parts_in[1][0], parts_in[-1][1])
            for g in f.split_top(rest[0], rest[1]):
                parts += [', ', f.span(g[0], g[1])]
        parts.append(')')
        infunc = self.in_function(node)
        self.add('R12', 'exec-statement', f.sig[i].row, f.span(i, e), [('call', parts, '')], 'call',
                 judgement=infunc,
                 why=('inside a function: Python 3 exec() cannot create or rebind the function\'s '
                      'locals; if later code relies on that, pass an explicit dict') if infunc else '',
                 key=None if infunc else 'R12:exec')

    def raise_site(self, node):
        f = self.f
        i = self.tok_index(node, 'raise')
        if i is None:
            return
        e = f.stmt_end(i)
        elems = f.split_top(i + 1, e)
        if len(elems) < 2:
            return
        typ, inst = elems[0], elems[1]
        parts = ['raise ', f.span(typ[0], typ[1])]
        if isinstance(node.inst, ast.Tuple) and f.is_(inst[0], '(', OP):
            parts.append(f.span(inst[0], inst[1]))
        else:
            parts += ['(', f.span(inst[0], inst[1]), ')']
        if len(elems) > 2:
            parts += ['.with_traceback(', f.span(elems[2][0], elems[2][1]), ')']
        self.add('OUT', 'raise-comma', f.sig[i].row, f.span(i, e), [('call', parts, '')], 'call',
                 judgement=True, why='raise E, V is a SyntaxError in Python 3; raise E(V) is the same '
                 'exception. Outside the catalogue, so the first decision is reused for every file.',
                 key='OUT:raise-comma')

    # token-level rules --------------------------------------------------------
    def find_tokens(self):
        f = self.f
        sig = f.sig
        backtick = None
        for i, t in enumerate(sig):
            prev_dot = f.is_(i - 1, '.', OP)
            nxt = sig[i + 1] if i + 1 < len(sig) else None
            if t.type == NAME and not prev_dot and not self.in_import(t) and not self.is_def_name(i):
                self.name_token(i, t, nxt)
            elif t.type == NAME and prev_dot:
                self.method_token(i, t, nxt)
            elif t.type == NUMBER:
                self.number_token(t)
            elif t.type == STRING:
                self.string_token(t)
            elif t.type == OP:
                if t.s == '`':
                    if backtick is None:
                        backtick = i
                    else:
                        a = sig[backtick]
                        self.add('R9', 'backtick', a.row, [a.start, t.end],
                                 [('repr', ['repr(', [a.end, t.start], ')'], '')], 'repr', key='R9:backtick')
                        backtick = None
                elif t.s == '<>':
                    self.add('R10', 'not-equal', t.row, [t.start, t.end], [('ne', ['!='], '')], 'ne',
                             key='R10:<>')
                elif t.s in ('/', '/=') and 'division' not in self.future:
                    self.add('R13', 'division', t.row, [t.start, t.end],
                             [('floor', [t.s.replace('/', '//')], 'integer operands, truncation intended'),
                              ('true', [t.s], 'float operands or true division intended (no change)')],
                             None, judgement=True,
                             why='depends on the runtime operand types: run probe_division for evidence')
            if t.type == NAME and not prev_dot and nxt is not None and nxt.s == '.' and i + 2 < len(sig):
                self.module_attr(i, t, sig[i + 2])

    def is_def_name(self, i):
        return self.f.is_(i - 1, 'def', NAME) or self.f.is_(i - 1, 'class', NAME)

    def name_node(self, t):
        return self.names_at.get((t.row, t.col))

    def name_token(self, i, t, nxt):
        f = self.f
        s = t.s
        assigned = nxt is not None and nxt.type == OP and nxt.s == '=' and not f.is_(i - 1, ',', OP)
        if s == 'xrange':
            self.add('R2', 'xrange', t.row, [t.start, t.end], [('range', ['range'], '')], 'range',
                     key='R2:xrange')
        elif s == 'range' and nxt is not None and nxt.s == '(' and 'range' not in self.defined:
            node = self.name_node(t)
            call = self.call_of(node) if node is not None else None
            if call is not None and not self.iteration_context(call):
                close = f.match.get(i + 1)
                self.add('R2', 'range-list', t.row, [t.start, f.sig[close].end],
                         [('list', ['list(', [t.start, f.sig[close].end], ')'], 'the result is used as a list'),
                          ('lazy', [[t.start, f.sig[close].end]], 'only iterated, indexed or len()-ed (no change)')],
                         None, judgement=True,
                         why='Py2 range() returns a list; here it is %s. Wrap in list() only if it is '
                             'mutated, concatenated, compared with a list or printed.' % self.describe_use(call))
        elif s == 'raw_input':
            self.add('R5', 'raw_input', t.row, [t.start, t.end], [('input', ['input'], '')], 'input',
                     key='R5:raw_input')
        elif s == 'input' and nxt is not None and nxt.s == '(' and 'input' not in self.defined:
            close = f.match.get(i + 1)
            self.add('R5', 'py2-input', t.row, [t.start, f.sig[close].end],
                     [('eval', ['eval(', [t.start, f.sig[close].end], ')'], 'behaviour-preserving'),
                      ('input', [[t.start, f.sig[close].end]], 'returns the typed text (behaviour change)')],
                     None, judgement=True, why='Py2 input() evaluates what the user typed. eval(input()) '
                     'keeps that (security-relevant); input() alone changes behaviour.', key='R5:py2-input')
        elif s == 'unicode' and not assigned:
            why = 'Py2 unicode is text. Check the values: anything that is really bytes must not become str.'
            if self.guarded_by(t, 'NameError'):
                why = ('inside a try/except NameError compatibility shim: str is right on Python 3, but '
                       'the shim itself may now be dead code. ') + why
            self.add('R6', 'unicode', t.row, [t.start, t.end], [('str', ['str'], '')], 'str',
                     judgement=True, why=why)
        elif s == 'basestring' and not assigned:
            self.add('R7', 'basestring', t.row, [t.start, t.end],
                     [('str', ['str'], 'only text was accepted'),
                      ('str_bytes', ['(str, bytes)'], 'Py2 byte strings were accepted too')], 'str',
                     judgement=True, why='did the code also accept Py2 byte strings? Decide from the callers (R6).')
        elif s == 'long' and not assigned:
            self.long_token(i, t, nxt)
        elif s in REMOVED_BUILTINS and nxt is not None and nxt.s == '(' and s not in self.defined:
            new = REMOVED_BUILTINS[s]
            if s == 'intern' and 'sys' not in self.plain_imports:
                new = None
            self.add('OUT', 'removed-builtin', t.row, [t.start, t.end],
                     [('py3', [new], '')] if new else [], new and 'py3', judgement=True,
                     why='%s() does not exist in Python 3%s' % (s, '; %s is the equivalent' % new if new else ''),
                     key='OUT:builtin:%s' % s)
        elif s in LAZY_BUILTINS and nxt is not None and nxt.s == '(' and s not in self.defined:
            node = self.name_node(t)
            call = self.call_of(node) if node is not None else None
            if call is not None and not self.iteration_context(call):
                close = f.match.get(i + 1)
                self.add('OUT', 'lazy-%s' % s, t.row, [t.start, f.sig[close].end],
                         [('list', ['list(', [t.start, f.sig[close].end], ')'], 'the result is used as a list'),
                          ('lazy', [[t.start, f.sig[close].end]], 'only iterated once (no change)')],
                         None, judgement=True,
                         why='Py2 %s() returns a list, Py3 a one-shot iterator; here it is %s'
                             % (s, self.describe_use(call)))
        elif s in ('async', 'await'):
            self.add('OUT', 'keyword-name', t.row, [t.start, t.end], [], None, judgement=True,
                     why='%s is a reserved keyword in Python 3.7+; it must be renamed everywhere' % s,
                     key='OUT:keyword-name:%s' % s)

    def guarded_by(self, t, exc):
        node = self.name_node(t)
        p = self.parents.get(node) if node is not None else None
        child = node
        while p is not None:
            if isinstance(p, ast.TryExcept) and any(child is b for b in p.body):
                for h in p.handlers:
                    names = [h.type] if not isinstance(h.type, ast.Tuple) else h.type.elts
                    if any(isinstance(n, ast.Name) and n.id == exc for n in names):
                        return True
            child, p = p, self.parents.get(p)
        return False

    def long_token(self, i, t, nxt):
        f = self.f
        o = f.enclosing_open(i)
        if o is not None and f.sig[o].s == '(' and f.is_display(o):
            close = f.match.get(o)
            elems = f.split_top(o + 1, close - 1) if close else []
            singles = [f.sig[a].s for a, b in elems if a == b]
            if len(elems) >= 2 and 'int' in singles and any(a == b == i for a, b in elems):
                keep = [(a, b) for a, b in elems if not (a == b == i) and a <= b]
                if len(keep) == 1:
                    parts = [f.span(*keep[0])]
                else:
                    parts = ['(']
                    for n, (a, b) in enumerate(keep):
                        if n:
                            parts.append(', ')
                        parts.append(f.span(a, b))
                    parts.append(')')
                self.add('R8', 'long-in-tuple', t.row, f.span(o, close), [('int', parts, '')], 'int',
                         key='R8:long-in-tuple')
                return
        if nxt is not None and nxt.s == '(':
            node = self.name_node(t)
            call = self.call_of(node) if node is not None else None
            p = self.parents.get(call) if call is not None else None
            fn = p.func.id if isinstance(p, ast.Call) and isinstance(p.func, ast.Name) else None
            if fn in ('hex', 'oct', 'repr', 'str'):
                self.add('R8', 'long-in-%s' % fn, t.row, [t.start, t.end], [('int', ['int'], '')], 'int',
                         judgement=True,
                         why='%s() of a Py2 long ends in "L" (e.g. hex(1L) == "0x1L"); Py3 has no L, so '
                             'the string differs. Check who reads it.' % fn, key='R8:long-in-%s' % fn)
                return
        self.add('R8', 'long', t.row, [t.start, t.end], [('int', ['int'], '')], 'int', key='R8:long')

    def number_token(self, t):
        s = t.s
        digits = s[:-1] if s[-1] in 'lL' else s
        if s[-1] in 'lL':
            self.add('R8', 'L-literal', t.row, [t.end - 1, t.end], [('int', [''], '')], 'int',
                     key='R8:L-literal')
        if len(digits) > 1 and digits[0] == '0' and digits[1:].isdigit() and int(digits[1:]) != 0:
            self.add('OUT', 'octal-literal', t.row, [t.start, t.start + len(digits)],
                     [('octal', ['0o' + digits.lstrip('0')], '')], 'octal', judgement=True,
                     why='0NNN octal literals are a SyntaxError in Python 3; 0oNNN is the same value',
                     key='OUT:octal-literal')

    def string_token(self, t):
        p = t.s[:2].lower()
        if t.s[:1] in 'uU' and p != 'ur':
            self.add('R6', 'u-prefix', t.row, [t.start, t.start + 1], [('drop', [''], '')], 'drop',
                     key='R6:u-prefix')
        elif p == 'ur':
            esc = '\\u' in t.s or '\\U' in t.s
            self.add('R6', 'ur-prefix', t.row, [t.start, t.start + 1], [('raw', [''], '')], 'raw',
                     judgement=esc, why=('Py2 ur"" still processed \\u escapes; Py3 r"" does not'
                                         if esc else ''), key=None if esc else 'R6:ur-prefix')

    def method_token(self, i, t, nxt):
        f = self.f
        s = t.s
        if nxt is None or nxt.s != '(':
            return
        close = f.match.get(i + 1)
        if close is None:
            return
        obj0 = f.primary_start(i - 2)
        no_args = close == i + 2
        if s == 'has_key':
            self.has_key_site(i, t, obj0, close)
        elif s in ('iteritems', 'iterkeys', 'itervalues') and no_args:
            new = s[4:]
            call = self.call_of(self.attr_node(s, t)) if self.attr_node(s, t) is not None else None
            ctx = self.iteration_context(call) if call is not None else None
            if ctx:
                self.add('R3', s, t.row, [t.start, t.end], [('view', [new], '')], 'view', key='R3:%s' % s)
            else:
                self.add('R3', s, t.row, [t.start, t.end],
                         [('view', [new], 'a view is enough'),
                          ('iter', [], 'needs an iterator: next() or single-pass use')], 'view',
                         judgement=True,
                         why='Py2 .%s() returned an iterator, Py3 .%s() returns a view; here it is %s'
                             % (s, new, self.describe_use(call) if call is not None else 'not a plain call'),
                         extra={'iter_span': [f.sig[obj0].start, f.sig[close].end], 'name_span': [t.start, t.end],
                                'new_name': new})
                site = self.sites[-1]
                site['options'][1]['parts'] = ['iter(', [f.sig[obj0].start, t.start], new,
                                               [t.end, f.sig[close].end], ')']
                site['options'][1]['text'] = 'iter(%s%s%s)' % (f.text(f.sig[obj0].start, t.start), new,
                                                              f.text(t.end, f.sig[close].end))
                site['start'] = f.sig[obj0].start
                site['end'] = f.sig[close].end
                site['before'] = f.text(site['start'], site['end'])
                site['options'][0]['parts'] = [[f.sig[obj0].start, t.start], new, [t.end, f.sig[close].end]]
        elif s in ('items', 'keys', 'values') and no_args:
            an = self.attr_node(s, t)
            call = self.call_of(an) if an is not None else None
            if call is not None and not self.iteration_context(call) and not self.in_import(t):
                span = [f.sig[obj0].start, f.sig[close].end]
                self.add('R3', 'dict-%s-list' % s, t.row, span,
                         [('list', ['list(', span, ')'], 'the result is used as a list'),
                          ('view', [span], 'a view is enough (no change)')], None, judgement=True,
                         why='Py2 .%s() returned a list, Py3 a view; here it is %s. Wrap only if it is '
                             'indexed, mutated, kept after the dict changes, or the dict changes while '
                             'iterating.' % (s, self.describe_use(call)))
        elif s in VIEW_METHODS and no_args:
            self.add('OUT', 'view-method', t.row, [t.start, t.end], [('py3', [VIEW_METHODS[s]], '')], 'py3',
                     judgement=True, why='.%s() is .%s() in Python 3' % (s, VIEW_METHODS[s]),
                     key='OUT:%s' % s)
        elif s == 'next' and no_args:
            span = [f.sig[obj0].start, f.sig[close].end]
            self.add('OUT', 'next-method', t.row, span,
                     [('builtin', ['next(', [f.sig[obj0].start, f.sig[i - 1].start], ')'], '')], 'builtin',
                     judgement=True, why='it.next() is next(it) in Python 3, unless the object defines its '
                     'own next() method that is not the iterator protocol', key='OUT:next-method')
        elif s in ('encode', 'decode') and close == i + 3 and f.sig[i + 2].type == STRING:
            codec = f.sig[i + 2].s.strip('\'"').lower()
            if codec in ('hex', 'base64', 'zlib', 'rot13', 'rot_13', 'bz2', 'string_escape', 'uu'):
                self.add('OUT', 'bytes-codec', t.row, [f.sig[obj0].start, f.sig[close].end], [], None,
                         judgement=True, why='.%s(%r) is a bytes-to-bytes codec removed from str/bytes '
                         'methods in Python 3 (use codecs or binascii)' % (s, codec),
                         key='OUT:codec:%s' % codec)

    def has_key_site(self, i, t, obj0, close):
        f = self.f
        p = obj0 - 1
        neg = f.is_(p, 'not', NAME) and not f.is_(p - 1, 'is', NAME)
        start = f.sig[p].start if neg else f.sig[obj0].start
        expr = self.wrap_if_loose(i + 2, close - 1) + [' not in ' if neg else ' in ',
                                                      [f.sig[obj0].start, f.sig[i - 1].start]]
        before_tok = f.sig[p - 1] if neg else f.sig[p] if p >= 0 else None
        after_tok = f.sig[close + 1] if close + 1 < len(f.sig) else None

        def tight(tok):
            return tok is not None and ((tok.type == OP and tok.s in TIGHT_OPS | set(['.', '(', '[']))
                                        or (tok.type == NAME and tok.s in ('in', 'is')))
        if (tight(before_tok) and not (before_tok.type == OP and before_tok.s in ('(', '['))) or (
                tight(after_tok) and after_tok.s not in ('(',)) or (after_tok is not None and after_tok.s in ('(', '.', '[')):
            expr = ['('] + expr + [')']
        self.add('R3', 'has_key', t.row, [start, f.sig[close].end], [('in', expr, '')], 'in', key='R3:has_key')

    def module_attr(self, i, t, attr_tok):
        f = self.f
        key = (t.s, attr_tok.s)
        if key not in MODULE_ATTRS or t.s not in self.plain_imports or self.in_import(t):
            return
        new = MODULE_ATTRS[key]
        why = '%s.%s does not exist in Python 3' % key
        if key == ('sys', 'maxint'):
            why = ('sys.maxint is gone; sys.maxsize is the nearest value (equal on 64-bit Linux, '
                   'different on 64-bit Windows). The catalogue sends this to decisions.md (R8).')
        self.add('OUT', 'module-attr', t.row, [t.start, attr_tok.end], [('py3', [new], '')] if new else [],
                 'py3' if new else None, judgement=True, why=why, key='OUT:%s.%s' % key)

    # defs ---------------------------------------------------------------------
    def find_defs(self):
        f = self.f
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                d = self.tok_index(node, 'def')
                if d is None or not f.is_(d + 1, node.name):
                    continue
                nt = f.sig[d + 1]
                in_class = isinstance(self.parents.get(node), ast.ClassDef)
                if any(isinstance(a, ast.Tuple) for a in node.args.args):
                    self.add('OUT', 'tuple-params', nt.row, [nt.start, nt.end], [], None, judgement=True,
                             why='def f((a, b)): tuple parameters are a SyntaxError in Python 3',
                             key='OUT:tuple-params')
                if not in_class:
                    continue
                if node.name in DIV_DUNDERS:
                    new = DIV_DUNDERS[node.name]
                    self.add('R13', 'div-dunder', nt.row, [nt.start, nt.end],
                             [('true', [new], 'true division'), ('floor', [new.replace('true', 'floor')],
                                                                  'integer (floor) division')], None,
                             judgement=True, why='Python 3 never calls %s; / calls %s and // calls %s'
                             % (node.name, new, new.replace('true', 'floor')))
                elif node.name in DUNDERS:
                    rename, unused = DUNDERS[node.name]
                    opts = []
                    if rename:
                        opts.append(('rename', [rename], 'Python 3 calls %s' % rename))
                    if unused:
                        opts.append(('remove', [''], 'Python 3 never calls it'))
                    span = [nt.start, nt.end]
                    if unused:
                        rs = self.def_span(node, d)
                        if rs:
                            span = rs
                    self.add('OUT', 'py2-dunder', nt.row, span, opts, None, judgement=True,
                             why='%s: %s' % (node.name, 'Python 3 uses %s instead' % rename if rename else
                                             'Python 3 never calls it (leftover, or behaviour to port)'),
                             key='OUT:dunder:%s' % node.name)
                elif node.name == 'next' and len(node.args.args) == 1:
                    self.add('OUT', 'next-def', nt.row, [nt.start, nt.end], [('rename', ['__next__'], '')],
                             None, judgement=True, why='a next(self) method is the Py2 iterator protocol; '
                             'Python 3 calls __next__ (explicit x.next() callers would then break)',
                             key='OUT:next-def')
            elif isinstance(node, ast.ClassDef):
                for st in node.body:
                    if (isinstance(st, ast.Assign) and len(st.targets) == 1 and
                            isinstance(st.targets[0], ast.Name) and st.targets[0].id == '__metaclass__'):
                        i = self.tok_index(st.targets[0])
                        if i is not None:
                            tk = f.sig[i]
                            self.add('OUT', 'metaclass-attr', tk.row, [tk.start, tk.end], [], None,
                                     judgement=True, why='__metaclass__ = M is ignored by Python 3; it '
                                     'needs class C(..., metaclass=M)', key='OUT:__metaclass__')
            elif isinstance(node, ast.Compare) and len(node.ops) == 1:
                l, r = node.left, node.comparators[0]
                names = set(n.id for n in (l, r) if isinstance(n, ast.Name))
                if names == set(['bytes', 'str']) and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                    i = self.tok_index(l)
                    if i is not None:
                        e = i + 2 if isinstance(node.ops[0], (ast.Is, ast.Eq, ast.NotEq)) else i + 3
                        self.add('OUT', 'compat-probe', f.sig[i].row, f.span(i, min(e, len(f.sig) - 1)), [],
                                 None, judgement=True,
                                 why='Py2/Py3 compatibility probe (bytes is str). It already runs correctly '
                                     'on Python 3: there it is False and the Python 3 branch is taken. "keep" '
                                     'leaves it working; the Py2-only branch it guards is dead code on Python 3 '
                                     'and can only be removed with edit_lines citing a decision.',
                                 key='OUT:compat-probe:bytes-is-str')
            elif isinstance(node, ast.keyword) and node.arg == 'cmp':
                p = self.parents.get(node)
                if isinstance(p, ast.Call):
                    i = self.tok_index(node.value)
                    if i is not None and f.is_(i - 2, 'cmp', NAME):
                        tk = f.sig[i - 2]
                        self.add('OUT', 'cmp-keyword', tk.row, [tk.start, f.sig[i].end], [], None,
                                 judgement=True, why='cmp= was removed from sort()/sorted(); it needs '
                                 'key=functools.cmp_to_key(...)', key='OUT:cmp-keyword')

    def def_span(self, node, d):
        """Byte span of a whole def (decorators included) up to the next statement."""
        f = self.f
        depth, j = 0, d
        while j < len(f.sig) and not (depth == 0 and f.sig[j].s == ':' and f.sig[j].type == OP):
            if f.sig[j].type == OP and f.sig[j].s in OPENERS:
                depth += 1
            elif f.sig[j].type == OP and f.sig[j].s in CLOSERS:
                depth -= 1
            j += 1
        if j + 2 >= len(f.sig) or f.sig[j + 1].type != NEWLINE or f.sig[j + 2].type != INDENT:
            return None
        level, k = 0, j + 2
        while k < len(f.sig):
            if f.sig[k].type == INDENT:
                level += 1
            elif f.sig[k].type == DEDENT:
                level -= 1
                if level == 0:
                    break
            k += 1
        last_newline = k - 1
        while last_newline > j and f.sig[last_newline].type != NEWLINE:
            last_newline -= 1
        first_row = min([node.lineno] + [x.lineno for x in node.decorator_list])
        return [f.starts[first_row - 1], f.starts[min(f.sig[last_newline].row, len(f.starts) - 1)]]


def _u(s):
    return s.decode('utf-8') if isinstance(s, bytes) and not isinstance(s, type(u'')) else s


def residual(src):
    """Python-2-only constructs still present, by token scan (works on Python 3 source too)."""
    f = File(src)
    sig = f.sig
    out = []

    def hit(rule, t, what):
        out.append({'rule': rule, 'line': t.row, 'what': what})
    stmt_start, in_import = True, False
    for i, t in enumerate(sig):
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        prev_dot = f.is_(i - 1, '.', OP)
        if t.type == NAME and not prev_dot:
            if stmt_start and t.s in ('print', 'exec') and nxt is not None and nxt.s not in ('(', '=', '.', ')', ','):
                if not (nxt.type in (NEWLINE, ENDMARKER) and t.s == 'exec'):
                    hit('R1' if t.s == 'print' else 'R12', t, '%s statement' % t.s)
            elif t.s in ('xrange',):
                hit('R2', t, 'xrange')
            elif t.s == 'raw_input':
                hit('R5', t, 'raw_input')
            elif t.s == 'unicode':
                hit('R6', t, 'unicode')
            elif t.s == 'basestring':
                hit('R7', t, 'basestring')
            elif t.s == 'long':
                hit('R8', t, 'long')
            elif t.s in RENAMED and (f.is_(i - 1, 'from', NAME) or (in_import and not f.is_(i - 1, 'as', NAME))):
                hit('R11', t, 'import %s' % t.s)
            elif t.s == 'except':
                depth = 0
                for j in range(i + 1, len(sig)):
                    s = sig[j]
                    if s.type == OP and s.s in OPENERS:
                        depth += 1
                    elif s.type == OP and s.s in CLOSERS:
                        depth -= 1
                    elif depth == 0 and s.type == OP and s.s == ',':
                        hit('R4', t, 'except X, e')
                        break
                    elif depth == 0 and s.type == OP and s.s == ':':
                        break
        elif t.type == NAME and prev_dot and t.s in ('has_key', 'iteritems', 'iterkeys', 'itervalues'):
            hit('R3', t, '.%s()' % t.s)
        elif t.type == NUMBER and t.s[-1] in 'lL':
            hit('R8', t, '%s literal' % t.s)
        elif t.type == OP and t.s == '`':
            hit('R9', t, 'backtick')
        elif t.type == OP and t.s == '<>':
            hit('R10', t, '<>')
        if stmt_start and t.type == NAME:
            in_import = t.s == 'import'
        elif t.type == NEWLINE or (t.type == OP and t.s == ';'):
            in_import = False
        stmt_start = t.type in (NEWLINE, INDENT, DEDENT) or (t.type == OP and t.s in (';', ':'))
    seen, uniq = set(), []
    for h in out:
        k = (h['rule'], h['line'], h['what'])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


def main(argv):
    path = argv[1]
    with open(path, 'rb') as fh:
        src = fh.read()
    if '--residual' in argv:
        try:
            res = residual(src)
            print(json.dumps({'path': path, 'residual': res}))
        except (tokenize.TokenError, IndentationError) as e:
            print(json.dumps({'path': path, 'residual': [], 'error': '%s: %s' % (type(e).__name__, e)}))
        return 0
    try:
        finder = Finder(src)
    except SyntaxError as e:
        print(json.dumps({'path': path, 'parse_error': 'line %s: %s' % (e.lineno, e.msg), 'sites': []}))
        return 0
    sites = finder.run()
    print(json.dumps({'path': path, 'encoding': finder.f.enc, 'future': sorted(finder.future),
                      'size': len(src), 'sites': sites}))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
