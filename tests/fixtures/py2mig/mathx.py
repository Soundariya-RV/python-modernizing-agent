# -*- coding: utf-8 -*-
"""Fixture for the Code Migration agent: every catalogue rule, Python 2 only."""
import ConfigParser
import Queue
from cStringIO import StringIO
import sys

MASK = 0xFFL
PERM = 0755


class Ratio(object):
    def __init__(self, n, d):
        self.n, self.d = n, d

    def __nonzero__(self):
        return self.n != 0

    def __div__(self, other):
        return Ratio(self.n * other.d, self.d * other.n)


def half(n):
    return n / 2


def mean(xs):
    return sum(xs) / float(len(xs))


def describe(d):
    out = []
    for k, v in sorted(d.iteritems()):
        out.append('%s=%s' % (k, `v`))
    if d.has_key('x') and not d.has_key('y'):
        out.append('x-only')
    return ', '.join(out)


def kinds(x):
    if isinstance(x, (int, long)):
        return 'int'
    if isinstance(x, basestring):
        return 'text'
    return 'other'


def evens(n):
    return [i for i in xrange(n) if i % 2 == 0]


def squares(n):
    r = range(n)
    r.append(n)
    return [i * i for i in r]


def parse(text):
    try:
        return int(text)
    except ValueError, e:
        return None


def show(*items):
    print >>sys.stderr, 'show:',
    print ' '.join(str(i) for i in items), 10L


def run(code):
    ns = {}
    exec code in ns
    return ns.get('result')


def differ(a, b):
    return a <> b


def fail():
    raise ValueError, 'bad'


def queue_size(n):
    return Queue.Queue(n).maxsize


def cfg():
    return ConfigParser.RawConfigParser()


def buf(text):
    return StringIO(text).read()


def big():
    return sys.maxint > 0


def ask():
    return raw_input('? ')


def label(x):
    return unicode(x) + u'!'
