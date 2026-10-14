"""Offline stand-in for Qwen in the Code Migration Agent: a fixed procedure, NOT a model.

Same message/tool-call protocol as the real backend, so the loop, tools and
state files can be tested without a GPU. It has no judgement: every site that
needs a choice gets the tools' suggestion (probe evidence, else the default),
and "keep" when there is none. Results from it are plumbing checks only.
"""
from __future__ import annotations

import json
import re
import uuid

from ..llm import Backend, Reply, ToolCall


class RulesBackend(Backend):
    name = 'rules'
    model = 'rules-stub (no LLM)'

    def __init__(self):
        self.files, self.i, self.phase, self.probed = [], 0, 'start', None

    def chat(self, messages, tools):
        if self.phase == 'start':
            user = next(m['content'] for m in messages if m['role'] == 'user')
            self.files = re.findall(r'^- (\S+) \(', user, re.M)
            self.phase = 'list'
        last = None
        if messages[-1]['role'] == 'tool':
            try:
                last = json.loads(messages[-1]['content'])
            except ValueError:
                last = {}
        call = self._next(last)
        if call is None:
            return Reply('rules backend: stopped: %s' % json.dumps(last)[:500])
        return Reply('', [ToolCall('call_' + uuid.uuid4().hex[:8], call[0], call[1])])

    def _next(self, last):
        if self.phase == 'done':
            return None
        if self.i >= len(self.files):
            self.phase = 'done'
            return 'finish_unit', {'summary': 'Rules-backend run (no LLM): mechanical sites applied, every '
                                              'other site got the suggested or default option, else keep.'}
        path = self.files[self.i]
        if self.phase == 'list':
            self.phase = 'listed'
            return 'list_sites', {'path': path}
        if self.phase == 'listed':
            todo = last.get('needs_choice', [])
            if any(s['rule'] == 'R13' and 'evidence' not in s for s in todo) and self.probed != path:
                self.probed = path
                self.phase = 'list'
                return 'probe_division', {'path': path}
            if todo:
                self.phase = 'list'
                return 'choose', {'path': path, 'choices': [
                    {'site_id': s['site_id'], 'option': s.get('suggested') or 'keep',
                     'reason': 'rules backend: tool suggestion, no judgement'} for s in todo]}
            self.phase = 'preview'
            return 'preview', {'path': path}
        if self.phase == 'preview':
            self.i += 1
            self.phase = 'list'
            return self._next(None)
        return None
