"""Generic tool-calling loop shared by every agent role.

The loop owns no domain logic. It sends messages, executes the tool calls the
model asks for, feeds results back, and stops when the toolbox reports that the
task is finished (or the step budget runs out). Every turn is appended to a
JSONL trajectory so a run can be audited or used for distillation later.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Dict, List

from .llm import Backend

MAX_TOOL_RESULT_CHARS = 12000
MAX_NUDGES = 3
MAX_CALLS_PER_REPLY = 8      # a model stuck repeating itself must not run a tool dozens of times


class Toolbox:
    """Base class: subclasses define `schemas()` and `tool_<name>` methods."""
    finished = False

    def schemas(self) -> List[dict]:
        raise NotImplementedError

    def call(self, name: str, args: dict) -> str:
        fn: Callable = getattr(self, 'tool_' + name, None)
        if fn is None or name not in {s['function']['name'] for s in self.schemas()}:
            return json.dumps({'error': 'unknown tool %r' % name})
        try:
            result = fn(**args)
        except TypeError as e:
            return json.dumps({'error': 'bad arguments for %s: %s' % (name, e)})
        except Exception as e:  # noqa: BLE001 - surface tool failures to the model
            return json.dumps({'error': '%s failed: %s: %s' % (name, type(e).__name__, e)})
        text = result if isinstance(result, str) else json.dumps(result, indent=1, default=str)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + '\n...[truncated %d chars; request a narrower view]' % (
                len(text) - MAX_TOOL_RESULT_CHARS)
        return text


def run(backend: Backend, toolbox: Toolbox, system: str, user: str,
        max_steps: int, log_path: str, on_step: Callable[[str], None] = print) -> Dict:
    messages: List[dict] = [{'role': 'system', 'content': system},
                            {'role': 'user', 'content': user}]
    tools = toolbox.schemas()
    nudges = 0
    usage = {'prompt_tokens': 0, 'completion_tokens': 0}
    with open(log_path, 'a', encoding='utf-8') as log:
        def write(ev):
            ev['t'] = round(time.time(), 3)
            log.write(json.dumps(ev, default=str) + '\n')
            log.flush()

        write({'event': 'start', 'backend': backend.name, 'model': backend.model,
               'system': system, 'user': user, 'tools': [t['function']['name'] for t in tools]})
        for step in range(1, max_steps + 1):
            reply = backend.chat(messages, tools)
            for k in usage:
                usage[k] += int(reply.usage.get(k, 0) or 0)
            messages.append(reply.as_message())
            write({'event': 'assistant', 'step': step, 'content': reply.content,
                   'tool_calls': [{'name': c.name, 'arguments': c.arguments, 'parse_error': c.parse_error}
                                  for c in reply.tool_calls],
                   'usage': reply.usage, 'finish_reason': reply.finish_reason})
            if not reply.tool_calls:
                nudges += 1
                if nudges > MAX_NUDGES:
                    write({'event': 'stop', 'reason': 'model stopped calling tools'})
                    return {'finished': False, 'steps': step, 'reason': 'model stopped calling tools',
                            'usage': usage}
                messages.append({'role': 'user', 'content':
                                 'Your last reply contained no tool call that could be executed. '
                                 'Continue by calling a tool, and never write tool results yourself: '
                                 'they come only from the tools. The task ends only when `finalize` '
                                 'returns ok=true.'})
                on_step('[%02d] (no tool call) %s' % (step, (reply.content or '')[:120]))
                continue
            nudges = 0                       # MAX_NUDGES counts consecutive replies without a call
            seen = set()
            for n, c in enumerate(reply.tool_calls):
                sig = (c.name, json.dumps(c.arguments, sort_keys=True, default=str))
                if c.parse_error:
                    result = json.dumps({'error': c.parse_error})
                elif sig in seen:
                    result = json.dumps({'skipped': 'same call as earlier in this reply; not run again'})
                elif n >= MAX_CALLS_PER_REPLY:
                    result = json.dumps({'skipped': 'more than %d calls in one reply; call it again after '
                                                    'reading the results' % MAX_CALLS_PER_REPLY})
                else:
                    result = toolbox.call(c.name, c.arguments)
                seen.add(sig)
                messages.append({'role': 'tool', 'tool_call_id': c.id, 'name': c.name, 'content': result})
                write({'event': 'tool', 'step': step, 'name': c.name, 'arguments': c.arguments,
                       'result': result})
                on_step('[%02d] %s(%s) -> %s' % (step, c.name, _short(c.arguments), _short(result)))
                if toolbox.finished:
                    write({'event': 'stop', 'reason': 'finalized'})
                    return {'finished': True, 'steps': step, 'reason': 'finalized', 'usage': usage}
        write({'event': 'stop', 'reason': 'step budget exhausted'})
        return {'finished': False, 'steps': max_steps, 'reason': 'step budget exhausted', 'usage': usage}


def _short(v, n=110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = ' '.join(s.split())
    return s if len(s) <= n else s[:n - 3] + '...'
