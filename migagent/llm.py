"""Chat backends. Qwen 32B is reached through any OpenAI-compatible server
(vLLM, SGLang, TGI, Ollama, LM Studio). Standard library only, so the agent
host needs nothing installed beyond Python 3.8+.

Tool calls: with vLLM run `--enable-auto-tool-choice --tool-call-parser hermes`
and they arrive as structured `tool_calls`. If the server returns raw text
instead, the Qwen/Hermes `<tool_call>{...}</tool_call>` format is parsed here.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

THINK = re.compile(r'<think>.*?</think>', re.S)
# Qwen2.5-Coder sometimes wraps the call in <tools> (the tag of the tool list) instead of <tool_call>
TOOL_CALL_TEXT = re.compile(r'<(tool_call|tools)>\s*(\{.*?\})\s*</\1>', re.S)
# ...or (through Ollama) as bare JSON objects, one or more, optionally in a code fence
FENCE = re.compile(r'^```(?:json)?\s*(.*?)\s*```$', re.S)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    parse_error: Optional[str] = None


@dataclass
class Reply:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str = ''

    def as_message(self) -> dict:
        """The assistant turn echoed back in OpenAI format."""
        msg = {'role': 'assistant', 'content': self.content or ''}
        if self.tool_calls:
            msg['tool_calls'] = [{'id': c.id, 'type': 'function',
                                  'function': {'name': c.name,
                                               'arguments': json.dumps(c.arguments)}}
                                 for c in self.tool_calls]
        return msg


class Backend:
    name = 'base'
    model = ''

    def chat(self, messages: List[dict], tools: List[dict]) -> Reply:
        raise NotImplementedError


class OpenAICompatBackend(Backend):
    name = 'openai'

    def __init__(self, base_url: str, model: str, api_key: str = 'EMPTY',
                 temperature: float = 0.0, max_tokens: int = 2048, timeout: int = 600,
                 disable_thinking: bool = False, retries: int = 3):
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.disable_thinking = disable_thinking
        self.retries = retries

    def chat(self, messages, tools):
        body = {'model': self.model, 'messages': messages, 'tools': tools,
                'tool_choice': 'auto', 'temperature': self.temperature,
                'max_tokens': self.max_tokens}
        if self.temperature == 0:
            body['seed'] = 0
        if self.disable_thinking:          # Qwen3: skip <think> for tool loops
            body['chat_template_kwargs'] = {'enable_thinking': False}
        data = json.dumps(body).encode('utf-8')
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(self.url, data=data, headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer %s' % self.api_key})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode('utf-8'))
                return self._parse(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', 'replace')[:2000]
                if 400 <= e.code < 500:      # a bad request will not fix itself
                    raise RuntimeError('LLM server rejected the request (%d): %s' % (e.code, detail))
                last = 'HTTP %d: %s' % (e.code, detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = str(e)
            time.sleep(2 ** attempt)
        raise RuntimeError('LLM server unreachable at %s: %s' % (self.url, last))

    @staticmethod
    def _parse(payload: dict) -> Reply:
        choice = payload['choices'][0]
        msg = choice.get('message') or {}
        content = THINK.sub('', msg.get('content') or '').strip()
        calls = []
        for tc in msg.get('tool_calls') or []:
            fn = tc.get('function') or {}
            calls.append(_make_call(tc.get('id'), fn.get('name', ''), fn.get('arguments')))
        if not calls:
            for m in TOOL_CALL_TEXT.finditer(content):
                try:
                    obj = json.loads(m.group(2))
                    calls.append(_make_call(None, obj.get('name', ''), obj.get('arguments', {})))
                except ValueError as e:
                    calls.append(ToolCall('call_' + uuid.uuid4().hex[:8], '', {}, 'bad tool_call JSON: %s' % e))
            if calls:
                content = TOOL_CALL_TEXT.sub('', content).strip()
        if not calls:
            calls = _bare_calls(content, truncated=choice.get('finish_reason') == 'length')
            if calls:
                content = ''
        return Reply(content, calls, payload.get('usage') or {}, choice.get('finish_reason') or '')


def _bare_calls(content: str, truncated: bool = False) -> List[ToolCall]:
    """The whole reply is {"name": ..., "arguments": {...}} objects and nothing else: treat them as calls.

    A reply cut off by the token limit keeps its complete calls; only the unfinished tail is dropped.
    """
    m = FENCE.match(content)
    text = m.group(1) if m else content
    dec, pos, calls = json.JSONDecoder(), 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text):
            return calls
        try:
            obj, pos = dec.raw_decode(text, pos)
        except ValueError:
            return calls if truncated else []
        if (not isinstance(obj, dict) or not isinstance(obj.get('name'), str)
                or set(obj) - {'name', 'arguments'}):
            return []
        calls.append(_make_call(None, obj['name'], obj.get('arguments', {})))


def _make_call(cid, name, args) -> ToolCall:
    cid = cid or 'call_' + uuid.uuid4().hex[:8]
    if isinstance(args, dict):
        return ToolCall(cid, name, args)
    try:
        parsed = json.loads(args or '{}')
        if not isinstance(parsed, dict):
            raise ValueError('arguments must be a JSON object')
        return ToolCall(cid, name, parsed)
    except ValueError as e:
        return ToolCall(cid, name, {}, 'arguments are not valid JSON: %s' % e)
