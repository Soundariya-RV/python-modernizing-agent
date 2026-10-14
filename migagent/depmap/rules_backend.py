"""Offline stand-in for Qwen: a fixed procedure, NOT a model.

It talks to the toolbox through the exact same message/tool-call protocol as
the real backend, so the loop, tools, state files and logs can be tested on a
machine without a GPU. It has no judgement: every failing block is "failed",
and every issue that needs a decision is blocked for human review. Numbers
from a rules-backend run are plumbing checks, never results.
"""
from __future__ import annotations

import json
import uuid

from ..llm import Backend, Reply, ToolCall


class RulesBackend(Backend):
    name = 'rules'
    model = 'rules-stub (no LLM)'

    def __init__(self):
        self.phase = 'start'
        self.blocks, self.i, self.todo = [], 0, []

    def chat(self, messages, tools):
        last = None
        if messages[-1]['role'] == 'tool':
            try:
                last = json.loads(messages[-1]['content'])
            except ValueError:
                last = {'raw': messages[-1]['content']}
        call = self._next(last)
        if call is None:
            return Reply('rules backend: finalize refused: %s' % json.dumps(last))
        name, args = call
        return Reply('', [ToolCall('call_' + uuid.uuid4().hex[:8], name, args)])

    def _next(self, last):
        p = self.phase
        if p == 'start':
            self.phase = 'read'
            return 'read_agent_md', {}
        if p == 'read':
            if last and last.get('remaining_chars'):
                return 'read_agent_md', {'offset': last['offset'] + len(last['text'])}
            self.phase = 'interp'
            return 'check_interpreters', {}
        if p == 'interp':
            self.phase = 'list'
            return 'list_agent_md_commands', {}
        if p == 'list':
            self.blocks, self.i, self.phase = last['blocks'], 0, 'block'
            return self._next(None)
        if p == 'block':
            if self.i >= len(self.blocks):
                self.phase = 'graph'
                return 'build_dependency_graph', {}
            b = self.blocks[self.i]
            if not b['runnable_here']:
                self.i += 1
                return 'record_command_verdict', {
                    'block_id': b['id'], 'verdict': 'skipped',
                    'note': b['platform_note'] or 'every line is setup or a placeholder'}
            self.phase = 'verdict'
            return 'run_agent_md_command', {'block_id': b['id']}
        if p == 'verdict':
            b = self.blocks[self.i]
            self.i += 1
            self.phase = 'block'
            ok = last.get('exit_code') == 0
            return 'record_command_verdict', {
                'block_id': b['id'], 'verdict': 'verified' if ok else 'failed',
                'note': 'exit %s (rules backend: exit code only, no reading of Agent.md)' % last.get('exit_code')}
        if p == 'graph':
            self.todo = list(last.get('to_resolve', []))
            self.phase = 'resolve'
            return self._next(None)
        if p == 'resolve':
            if self.todo:
                return 'resolve_issue', {
                    'issue_key': self.todo.pop(0), 'action': 'block',
                    'decision': 'Flag for human review before migrating the affected files.',
                    'reason': 'Rules backend has no judgement; the conservative default is to block.'}
            self.phase = 'final'
            return 'finalize', {'summary': 'Rules-backend run (no LLM): commands verified by exit code '
                                           'only and every issue needing a decision blocked for review.'}
        return None
