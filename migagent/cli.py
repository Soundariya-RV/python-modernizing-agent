"""Command-line plumbing shared by every stage: options, interpreters, the model backend."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Optional

from .llm import OpenAICompatBackend

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENDPOINTS = ('http://localhost:8000/v1', 'http://localhost:11434/v1')   # vLLM / llama.cpp, Ollama


def add_common(ap):
    g = ap.add_argument_group('repository and interpreters')
    g.add_argument('-repo', '--repo', '-r', dest='repo', required=True,
                   help='the Python 2 repository: a local path, or a git URL to clone')
    g.add_argument('--ref', help='with a git URL (or a git repo): branch, tag or commit to check out as the '
                                 'Python 2 baseline')
    g.add_argument('--workdir', default=os.path.join(AGENT_DIR, '..', 'repos'),
                   help='where git URLs are cloned (default ../repos)')
    g.add_argument('--py2', default=os.environ.get('PY2_BIN'),
                   help='Python 2.7 interpreter (default $PY2_BIN, else ~/envs/py27/bin/python)')
    g.add_argument('--py3', default=os.environ.get('PY3_BIN'),
                   help='Python 3 interpreter (default $PY3_BIN, else ~/envs/py311/bin/python)')
    g = ap.add_argument_group('model')
    g.add_argument('--backend', choices=['openai', 'rules'], default='openai',
                   help='openai: Qwen behind an OpenAI-compatible server; rules: offline stand-in (plumbing only)')
    g.add_argument('--base-url', default=os.environ.get('QWEN_BASE_URL'),
                   help='default $QWEN_BASE_URL, else the first of %s that answers' % ', '.join(DEFAULT_ENDPOINTS))
    g.add_argument('--model', default=os.environ.get('QWEN_MODEL'),
                   help='default $QWEN_MODEL, else the Qwen model the server lists')
    g.add_argument('--api-key', default=os.environ.get('QWEN_API_KEY', 'EMPTY'))
    g.add_argument('--temperature', type=float, default=0.0)
    g.add_argument('--max-tokens', type=int, default=1024, help='per model reply')
    g.add_argument('--llm-timeout', type=int, default=int(os.environ.get('QWEN_TIMEOUT', '3600')),
                   help='seconds per model request (default $QWEN_TIMEOUT or 3600)')
    g.add_argument('--no-thinking', action='store_true', help='Qwen3: disable <think> mode')
    g = ap.add_argument_group('runs')
    g.add_argument('--command-timeout', type=int, default=600, help='seconds per command the agents run')
    g.add_argument('--runs-dir', default=os.path.join(AGENT_DIR, 'runs'), help='trajectories (default runs/)')
    g.add_argument('--quiet', action='store_true', help='do not print every tool call')


def is_url(path: str) -> bool:
    return path.startswith(('http://', 'https://', 'git@', 'ssh://', 'git://', 'file://')) or \
        (path.endswith('.git') and not os.path.isdir(path))


def resolve_repo(path: str, ref: Optional[str] = None, workdir: Optional[str] = None) -> str:
    """A local directory, or a git URL cloned into workdir (reused if already there)."""
    if is_url(path):
        name = path.rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1]
        name = name[:-4] if name.endswith('.git') else name
        dest = os.path.abspath(os.path.join(workdir or os.path.join(AGENT_DIR, '..', 'repos'), name))
        if not os.path.isdir(os.path.join(dest, '.git')):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            print('cloning %s into %s' % (path, dest), flush=True)
            if subprocess.run(['git', 'clone', '--quiet', path, dest]).returncode != 0:
                sys.exit('git clone failed: %s' % path)
        repo = dest
    else:
        repo = next((os.path.abspath(c) for c in (path, os.path.join(AGENT_DIR, '..', path)) if os.path.isdir(c)), None)
        if not repo:
            sys.exit('repository not found: %s' % path)
    if ref:
        head = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        want = subprocess.run(['git', '-C', repo, 'rev-parse', ref + '^{commit}'], capture_output=True, text=True)
        if want.returncode != 0:
            sys.exit('%s: no such git ref in %s' % (ref, repo))
        if want.stdout.strip() != head:
            if subprocess.run(['git', '-C', repo, 'status', '--porcelain', '--untracked-files=no'],
                              capture_output=True, text=True).stdout.strip():
                sys.exit('%s has uncommitted changes; not switching to %s' % (repo, ref))
            print('checking out %s in %s' % (ref, repo), flush=True)
            if subprocess.run(['git', '-C', repo, 'checkout', '--quiet', ref]).returncode != 0:
                sys.exit('git checkout %s failed' % ref)
    return repo


def resolve_interpreters(a):
    home = os.path.expanduser('~')
    a.py2 = a.py2 or _existing(os.path.join(home, 'envs', 'py27', 'bin', 'python'))
    a.py3 = a.py3 or _existing(os.path.join(home, 'envs', 'py311', 'bin', 'python'))
    if not a.py2 or not a.py3:
        sys.exit('Python 2.7 and 3 interpreters are needed: set --py2/--py3 or PY2_BIN/PY3_BIN')


def _existing(p):
    return p if os.path.exists(p) else None


def _models(base_url: str) -> Optional[list]:
    try:
        with urllib.request.urlopen(base_url.rstrip('/') + '/models', timeout=3) as r:
            data = json.loads(r.read().decode('utf-8'))
        return [m.get('id') or m.get('name') for m in data.get('data', data.get('models', []))]
    except Exception:
        return None


def resolve_llm(a):
    """Fill base_url and model from the environment, or from whichever local server answers."""
    if a.backend != 'openai':
        return
    if not a.base_url:
        for url in DEFAULT_ENDPOINTS:
            if _models(url) is not None:
                a.base_url = url
                break
        else:
            sys.exit('no model server answers at %s: start one or set --base-url' % ', '.join(DEFAULT_ENDPOINTS))
    if not a.model:
        models = _models(a.base_url) or []
        qwen = [m for m in models if m and 'qwen' in m.lower()]
        best = [m for m in qwen if '32b' in m.lower()] or qwen or models
        if not best:
            sys.exit('the server at %s lists no model: set --model' % a.base_url)
        ctx = {m: _ollama_ctx(a.base_url, m) for m in best}
        a.model = max(best, key=lambda m: (ctx[m] or 0, -best.index(m)))
        if ctx[a.model] is not None and ctx[a.model] < 16384:
            print('warning: %s has a %d-token context; the agents need about 32k (Ollama: a Modelfile '
                  'with PARAMETER num_ctx 32768), or pass --model' % (a.model, ctx[a.model]), flush=True)


def _ollama_ctx(base_url: str, model: str) -> Optional[int]:
    """Context length Ollama will use for a model (num_ctx, else its 2048 default); None if not Ollama."""
    root = base_url.rstrip('/')
    root = root[:-3] if root.endswith('/v1') else root
    try:
        req = urllib.request.Request(root + '/api/show', data=json.dumps({'model': model}).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            info = json.loads(r.read().decode('utf-8'))
    except Exception:
        return None
    for line in (info.get('parameters') or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == 'num_ctx' and parts[1].isdigit():
            return int(parts[1])
    return 2048


def make_backend(a, rules_cls):
    if a.backend == 'rules':
        return rules_cls()
    return OpenAICompatBackend(a.base_url, a.model, a.api_key, a.temperature, a.max_tokens,
                               timeout=a.llm_timeout, disable_thinking=a.no_thinking)


def say(a, s):
    if not getattr(a, 'quiet', False):
        print(s, flush=True)
