"""Offline stand-in for Qwen in the Cross-File Consistency agent: a fixed procedure, NOT a model.

It marks every needs_judgement finding as a violation (the conservative
answer) and finishes. Results from it are plumbing checks only.
"""
from __future__ import annotations

import json
import uuid

from ..llm import Backend, Reply, ToolCall


class RulesBackend(Backend):
    name = 'rules'
    model = 'rules-stub (no LLM)'

    def __init__(self):
        self.phase, self.todo = 'start', []

    def chat(self, messages, tools):
        last = {}
        if messages[-1]['role'] == 'tool':
            try:
                last = json.loads(messages[-1]['content'])
            except ValueError:
                pass
        if self.phase == 'checked':
            self.todo, self.phase = list(last.get('needs_judgement', [])), 'resolve'
        if self.phase == 'start':
            self.phase = 'checked'
            call = ('run_checks', {})
        elif self.phase == 'resolve' and self.todo:
            call = ('resolve', {'finding_id': self.todo.pop(0), 'action': 'violation',
                                'reason': 'rules backend: no judgement, conservative default'})
        elif self.phase == 'resolve':
            self.phase = 'done'
            call = ('finish', {'summary': 'Rules-backend run (no LLM): API changes marked as violations.'})
        else:
            return Reply('rules backend: stopped')
        return Reply('', [ToolCall('call_' + uuid.uuid4().hex[:8], call[0], call[1])])
