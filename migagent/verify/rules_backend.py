"""Offline stand-in for Qwen in the Verification Gate: a fixed procedure, NOT a model.

It seeds characterization cases (no cases of its own, so run it with
--min-model-cases 0), runs the checks and finishes. It never accepts a
divergence. Results from it are plumbing checks only.
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

    def chat(self, messages, tools):
        last = {}
        if messages[-1]['role'] == 'tool':
            try:
                last = json.loads(messages[-1]['content'])
            except ValueError:
                pass
        call = self._next(last)
        if call is None:
            return Reply('rules backend: stopped: %s' % json.dumps(last)[:400])
        return Reply('', [ToolCall('call_' + uuid.uuid4().hex[:8], call[0], call[1])])

    def _next(self, last):
        p = self.phase
        if p == 'start':
            self.phase = 'listed'
            return 'list_checks', {}
        if p == 'listed':
            self.phase = 'run'
            if any(c.startswith('characterization') for c in last.get('checks', [])):
                return 'seed_cases', {}
            return self._next(last)
        if p == 'run':
            self.phase = 'finish'
            return 'run_checks', {}
        if p == 'finish':
            self.phase = 'done'
            return 'finish_unit', {'summary': 'Rules-backend run (no LLM): seeded cases only, no judgement.'}
        return None
