#!/usr/bin/env python3
"""Python 2.7 -> 3 modernizing agent: one entry point for the whole pipeline.

  init -> Dependency Mapping -> Code Migration -> Verification Gate -> Cross-File Consistency

Any Python 2 repository works: a local path or a git URL. A repo without an
Agent.md gets one from the init stage, built from commands it ran under both
interpreters, plus the default rule catalogue.

Every stage is driven by Qwen 32B behind an OpenAI-compatible server (vLLM,
llama.cpp or Ollama) and resumes from the repo's migration_state.json, so running
the same command again continues where the last run stopped.

  python3 modernize.py -repo ../ipaddr-py                    # all stages, in order
  python3 modernize.py -repo https://github.com/org/proj --ref v1.0   # clone, then all stages
  python3 modernize.py -repo ../ipaddr-py --stage migrate --file ipaddr.py
  python3 modernize.py -repo ../ipaddr-py --status           # where it stands (no model)
  python3 modernize.py -repo ../ipaddr-py --check-llm        # is the model server usable?
  python3 modernize.py -repo ../ipaddr-py --backend rules    # offline plumbing run, no model

Each run writes a results folder (console.log, before/after record files, agent
trajectories, status.txt, EXIT) under --results.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from migagent import cli  # noqa: E402
from migagent.results import Results, status_report  # noqa: E402
from migagent.stages import consistency, init, mapper, migrate, verify  # noqa: E402

STAGES = [('init', init), ('map', mapper), ('migrate', migrate), ('verify', verify), ('consistency', consistency)]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog='modernize.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_common(ap)
    g = ap.add_argument_group('what to run')
    g.add_argument('--stage', choices=['all'] + [n for n, _ in STAGES], default='all',
                   help='all (default): init if there is no Agent.md, map if not mapped yet, then migrate, '
                        'verify, consistency')
    g.add_argument('--file', help='migrate / verify only the unit or file containing this repo-relative path')
    g.add_argument('--max-steps', type=int, default=0, help='model turns per session (default per stage)')
    g.add_argument('--status', action='store_true', help='print where the migration stands and exit')
    g.add_argument('--check-llm', action='store_true', help='check the model server answers with tool calls')
    g.add_argument('--results', default=os.path.join(HERE, '..', 'results'),
                   help='where results folders go (default ../results)')
    g.add_argument('--no-results', action='store_true', help='do not write a results folder')
    for _, mod in STAGES:
        mod.add_args(ap)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    a.repo = cli.resolve_repo(a.repo, a.ref, a.workdir)
    if a.status:
        print(status_report(a.repo))
        return 0
    cli.resolve_interpreters(a)
    cli.resolve_llm(a)
    if a.check_llm:
        return mapper.check_llm(cli.make_backend(a, None))
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    res_dir = None if a.no_results else Results(os.path.abspath(a.results), a.repo, stamp, a.stage)
    code = 1
    try:
        code = run_stages(a, stamp, res_dir)
    finally:
        if res_dir:
            res_dir.close(code)
    return code


def run_stages(a, stamp: str, res_dir) -> int:
    print('modernize  repo=%s  stage=%s' % (a.repo, a.stage), flush=True)
    print('python2=%s  python3=%s' % (a.py2, a.py3), flush=True)
    print('backend=%s%s' % (a.backend, '  model=%s  url=%s' % (a.model, a.base_url) if a.backend == 'openai' else ''),
          flush=True)
    if a.backend == 'openai':
        print('\n== preflight', flush=True)
        if mapper.check_llm(cli.make_backend(a, None)) != 0:
            return 2
    for name, mod in STAGES:
        if a.stage not in ('all', name):
            continue
        if name == 'init' and a.stage == 'all' and not init.needed(a):
            print('\n== init: skipped (Agent.md exists; --reinit to write it again)', flush=True)
            continue
        if name == 'map' and a.stage == 'all' and not mapper.needed(a):
            print('\n== map: skipped (migration_state.json exists; --remap to map again)', flush=True)
            continue
        run_dir = os.path.join(a.runs_dir, os.path.basename(a.repo), mod.NAME, stamp)
        os.makedirs(run_dir, exist_ok=True)
        if res_dir:
            res_dir.run_dirs.append(run_dir)
        log_path = os.path.join(run_dir, 'trajectory.jsonl')
        print('\n== %s (%s)  log=%s' % (name, dt.datetime.now().strftime('%H:%M:%S'), log_path), flush=True)
        res = mod.run(a, log_path)
        with open(os.path.join(run_dir, 'result.json'), 'w', encoding='utf-8') as f:
            json.dump(res, f, indent=2, default=str)
        print('== %s: %s (%s)' % (name, 'finished' if res.get('finished') else 'NOT FINISHED', res.get('reason')),
              flush=True)
        if not res.get('finished'):
            print('\n' + status_report(a.repo), flush=True)
            return 1
    print('\n' + status_report(a.repo), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
