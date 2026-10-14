# -*- coding: utf-8 -*-
"""Ask *this* interpreter where modules come from. Runs under 2.7 and 3.x.

    python py2_probe.py <module> [<module> ...]

Prints JSON {name: {"available", "stdlib", "builtin", "origin"}}.
Modules are located, never imported: Python 2 uses imp.find_module on the
top-level name, Python 3 uses importlib.util.find_spec (stdlib parents only).
"""
from __future__ import print_function

import json
import os
import sys

# Never resolve against this tool's own directory.
if sys.path and os.path.abspath(sys.path[0] or '.') == os.path.dirname(os.path.abspath(__file__)):
    sys.path.pop(0)


def _stdlib_dir():
    import distutils.sysconfig as sc  # noqa: deprecated on 3.10+, removed 3.12
    return os.path.normcase(os.path.realpath(sc.get_python_lib(standard_lib=True)))


def probe_py2(name):
    import imp
    top = name.split('.')[0]
    if top in sys.builtin_module_names:
        return {'available': True, 'stdlib': True, 'builtin': True, 'origin': 'built-in'}
    try:
        f, path, desc = imp.find_module(top)
    except ImportError:
        return {'available': False, 'stdlib': False, 'builtin': False, 'origin': None}
    if f:
        f.close()
    real = os.path.normcase(os.path.realpath(path))
    std = _stdlib_dir()
    stdlib = (real.startswith(std) and 'site-packages' not in real
              and 'dist-packages' not in real)
    # DLLs/ (Windows) and lib-dynload/ (POSIX) hold C stdlib extensions.
    if not stdlib and ('lib-dynload' in real or os.sep + 'dlls' + os.sep in real):
        stdlib = True
    res = {'available': True, 'stdlib': stdlib, 'builtin': False, 'origin': path}
    if '.' in name and stdlib:
        # e.g. os.path, xml.etree.ElementTree: walk the package.
        try:
            __import__(name)
        except ImportError:
            res['available'] = False
    return res


def probe_py3(name):
    import importlib.util
    top = name.split('.')[0]
    stdlib = top in getattr(sys, 'stdlib_module_names', ()) or top in sys.builtin_module_names
    try:
        spec = importlib.util.find_spec(name if stdlib else top)
    except (ImportError, ValueError):
        spec = None
    origin = getattr(spec, 'origin', None) if spec else None
    return {'available': spec is not None, 'stdlib': bool(stdlib),
            'builtin': top in sys.builtin_module_names, 'origin': origin}


def main(argv):
    probe = probe_py2 if sys.version_info[0] == 2 else probe_py3
    out = {}
    for name in argv[1:]:
        try:
            out[name] = probe(name)
        except Exception as e:  # noqa: BLE001 - report, never crash the scan
            out[name] = {'available': False, 'stdlib': False, 'builtin': False,
                         'origin': None, 'error': '%s: %s' % (type(e).__name__, e)}
    json.dump({'python': sys.version.split()[0], 'modules': out}, sys.stdout,
              indent=1, sort_keys=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
