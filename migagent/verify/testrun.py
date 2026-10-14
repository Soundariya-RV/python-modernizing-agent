# -*- coding: utf-8 -*-
"""Run one unittest file and record every test's outcome. Python 2.7 and 3.x.

    python testrun.py <tree> <test_file_rel> <out.json>

The file is imported by module name with its directory and the tree root on
sys.path, from cwd=<tree>, so `if __name__ == '__main__'` blocks do not run.
out.json: {"python", "n", "tests": {id: {"outcome", "exc", "msg", "trace"}},
"import_error"}; outcome is pass | fail | error | skip | xfail | xpass.
"""
from __future__ import print_function

import json
import os
import sys
import traceback
import unittest


class Recorder(unittest.TestResult):
    def __init__(self):
        unittest.TestResult.__init__(self)
        self.buffer = True          # keep test output out of ours; it is appended to tracebacks
        self.out = {}

    def _put(self, test, outcome, err=None):
        rec = {'outcome': outcome}
        if err is not None:
            etype, value, tb = err
            rec['exc'] = etype.__name__
            rec['msg'] = _text(value)[:300]
            rec['trace'] = ''.join(traceback.format_exception(etype, value, tb))[-1500:]
        self.out[test.id()] = rec

    def addSuccess(self, test):
        self._put(test, 'pass')

    def addFailure(self, test, err):
        unittest.TestResult.addFailure(self, test, err)
        self._put(test, 'fail', err)

    def addError(self, test, err):
        unittest.TestResult.addError(self, test, err)
        self._put(test, 'error', err)

    def addSkip(self, test, reason):
        self._put(test, 'skip')

    def addExpectedFailure(self, test, err):
        self._put(test, 'xfail')

    def addUnexpectedSuccess(self, test):
        self._put(test, 'xpass')


def _text(v):
    try:
        s = str(v)
    except Exception:
        s = repr(v)
    if not isinstance(s, type(u'')):
        s = s.decode('utf-8', 'replace')
    return s


def main(argv):
    tree, rel, out = os.path.abspath(argv[1]), argv[2], argv[3]
    path = os.path.join(tree, rel)
    os.chdir(tree)
    for p in (tree, os.path.dirname(path)):
        if p not in sys.path:
            sys.path.insert(0, p)
    res = {'python': sys.version.split()[0], 'n': 0, 'tests': {}, 'import_error': None}
    try:
        name = os.path.splitext(os.path.basename(path))[0]
        mod = __import__(name)
        suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
        rec = Recorder()
        suite.run(rec)
        res['tests'] = rec.out
        res['n'] = len(rec.out)
    except BaseException:
        res['import_error'] = traceback.format_exc()[-2000:]
    with open(out, 'w') as f:
        json.dump(res, f)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
