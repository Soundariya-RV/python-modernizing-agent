"""Stage `map`: the Dependency Mapping Agent (demo steps 1-2).

Verifies every Agent.md command, maps file and import dependencies, computes the
migration order and writes migration_state.json (+ progress.md, decisions.md).
"""
from __future__ import annotations

import os

from .. import loop, state
from ..cli import make_backend, say
from ..depmap import prompts
from ..depmap.rules_backend import RulesBackend
from ..depmap.tools import DependencyMapperTools

NAME = 'dependency_mapper'


def add_args(ap):
    g = ap.add_argument_group('stage map (Dependency Mapping)')
    g.add_argument('--exclude', action='append', default=[], help='repo-relative dir to skip (repeatable)')
    g.add_argument('--shell', choices=['powershell', 'bash'], default=None,
                   help='which Agent.md blocks to run (default: powershell on Windows, bash elsewhere)')
    g.add_argument('--remap', action='store_true',
                   help='with --stage all: map again although migration_state.json exists')


def run(a, log_path: str) -> dict:
    backend = make_backend(a, RulesBackend)
    tools = DependencyMapperTools(a.repo, a.py2, a.py3, a.exclude, backend.model, shell=a.shell,
                                  command_timeout=a.command_timeout, run_log=log_path)
    old = state.load_state(a.repo)
    user = prompts.USER.format(
        repo=a.repo, agent_md=tools.agent_md_path or 'NOT FOUND', py2=a.py2, py3=a.py3,
        shell=tools.shell, excludes=a.exclude or 'none', n_decisions=len(tools.prior_decisions),
        state_note=('present, updated %s; statuses set by later agents are preserved'
                    % old.get('updated_at')) if old else 'none (first run)')
    say(a, 'shell=%s  agent_md_blocks=%d' % (tools.shell, len(tools.blocks)))
    res = loop.run(backend, tools, prompts.SYSTEM, user, a.max_steps or 80, log_path,
                   on_step=lambda s: say(a, s))
    res['outputs'] = tools.outputs
    return res


def check_llm(backend) -> int:
    """One round trip with a dummy tool: is the server up, and are tool calls parsed?"""
    tool = {'type': 'function', 'function': {
        'name': 'ping', 'description': 'Reply to a health check.',
        'parameters': {'type': 'object', 'properties': {'msg': {'type': 'string'}}, 'required': ['msg']}}}
    try:
        reply = backend.chat([{'role': 'user', 'content': 'Call the ping tool with msg="ok".'}], [tool])
    except RuntimeError as e:
        print('FAIL: %s' % e, flush=True)
        return 2
    calls = [(c.name, c.arguments, c.parse_error) for c in reply.tool_calls]
    print('model=%s finish=%s usage=%s' % (backend.model, reply.finish_reason, reply.usage), flush=True)
    print('tool_calls=%s' % calls, flush=True)
    ok = bool(calls) and calls[0][0] == 'ping' and not calls[0][2]
    print('OK: tool calling works' if ok else
          'FAIL: no usable tool call. With vLLM start the server with '
          '--enable-auto-tool-choice --tool-call-parser hermes. Content was: %r' % reply.content[:300], flush=True)
    return 0 if ok else 1


def needed(a) -> bool:
    return a.remap or not os.path.exists(os.path.join(a.repo, state.STATE_FILE))
