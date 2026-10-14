"""Parse Agent.md into runnable command blocks and execute them safely.

The model never sends a shell string. It picks a block id; this module decides
which lines of that block may run:

  * setup lines (create envs, clone, change branches, install, delete) are
    dropped. They change the machine, and verifying a setup means checking its
    result (check_interpreters), not repeating it.
  * assignments to PY2_BIN / PY3_BIN are dropped. The tool injects them, so a
    path written for another machine cannot override them.
  * lines with <placeholders> are dropped.
  * a block in another platform's shell (powershell on Linux, bash on Windows)
    is not run.

Every dropped line is reported with its reason, so the verdict stays honest.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

LANGS = {'powershell': 'powershell', 'ps1': 'powershell', 'pwsh': 'powershell',
         'bash': 'bash', 'sh': 'bash', 'shell': 'bash', 'console': 'bash', 'zsh': 'bash'}

SETUP_PATTERNS = [
    (r'\bconda(\.exe)?\W.*\b(create|install|remove|env)\b', 'environment setup'),
    (r'\bgit\s+(clone|switch|checkout|tag|worktree|reset|push|commit|pull|fetch|stash|rebase|merge)\b', 'git setup / state change'),
    (r'\bpip3?\s+install\b|-m\s+pip\s+install\b', 'package install'),
    (r'\b(Remove-Item|rm|rmdir|del)\b', 'deletes files'),
    (r'\bmake\b.*\binstall\b|\./configure\b|\bmake\s+-j', 'interpreter build'),
    (r'\b(sudo|apt|apt-get|yum|dnf|brew|choco|winget)\b', 'system package manager'),
    (r'^\s*(cd|Set-Location)\s', 'changes directory (tool runs in the repo root)'),
]
ENV_ASSIGN = re.compile(r'^\s*(\$env:|export\s+)?(PY2_BIN|PY3_BIN)\s*=')
PLACEHOLDER = re.compile(r'<[A-Za-z][\w-]*>')
CONTINUES = ('`', '\\', '{', '(', '|', ',', '&&', '||')


@dataclass
class Block:
    id: str
    section: str
    lang: str
    code: str
    run_lines: List[str] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)
    runnable_here: bool = False
    platform_note: str = ''
    # results
    exit_code: Optional[int] = None
    output_tail: str = ''
    seconds: float = 0.0
    line_exits: List[dict] = field(default_factory=list)
    verdict: Optional[str] = None
    note: str = ''

    def summary(self) -> dict:
        return {'id': self.id, 'section': self.section, 'lang': self.lang,
                'runnable_here': self.runnable_here, 'platform_note': self.platform_note,
                'will_run': self.run_lines, 'dropped': [{'line': l, 'reason': r} for l, r in self.dropped],
                'exit_code': self.exit_code, 'verdict': self.verdict}


def host_shell() -> str:
    return 'powershell' if sys.platform.startswith('win') else 'bash'


def parse(text: str, shell: Optional[str] = None) -> List[Block]:
    shell = shell or host_shell()
    blocks: List[Block] = []
    section = ''
    in_block, lang, buf = False, '', []
    for line in text.splitlines():
        if not in_block:
            m = re.match(r'^(#{1,6})\s+(.*)', line)
            if m:
                section = m.group(2).strip()
            m = re.match(r'^\s*```\s*([\w+-]*)', line)
            if m:
                in_block, lang, buf = True, m.group(1).lower(), []
            continue
        if re.match(r'^\s*```\s*$', line):
            in_block = False
            norm = LANGS.get(lang)
            if norm:
                blocks.append(_classify(Block('B%d' % (len(blocks) + 1), section, norm, '\n'.join(buf)), shell))
            continue
        buf.append(line)
    return blocks


def _classify(b: Block, shell: str) -> Block:
    if b.lang != shell:
        b.platform_note = '%s block; this host runs %s' % (b.lang, shell)
    after_placeholder = False
    for raw in b.code.splitlines():
        line = raw.rstrip()
        bare = line.strip()
        if not bare or bare.startswith('#'):
            continue
        if after_placeholder:
            # later lines usually consume what the placeholder line produced
            b.dropped.append((bare, 'follows a dropped <placeholder> line'))
            continue
        if ENV_ASSIGN.search(bare):
            b.dropped.append((bare, 'sets PY2_BIN/PY3_BIN; the tool injects these'))
            continue
        if PLACEHOLDER.search(bare):
            b.dropped.append((bare, 'contains a <placeholder>'))
            after_placeholder = True
            continue
        reason = next((r for p, r in SETUP_PATTERNS if re.search(p, bare, re.I)), None)
        if reason:
            b.dropped.append((bare, reason))
            continue
        b.run_lines.append(line)
    b.runnable_here = bool(b.run_lines) and not b.platform_note
    return b


MARK = '##[agentmd line %d exit '
MARK_RE = re.compile(r'##\[agentmd line (\d+) exit (-?\d+)\]')


def _script(b: Block) -> Tuple[str, List[str]]:
    """Run every line, report each statement's exit code, exit with the first failure.

    Not stop-on-first-error: the model needs to see every line's result to
    tell an expected failure (documented in Agent.md) from a real one.
    """
    if b.lang == 'powershell':
        out = ["$ErrorActionPreference = 'Continue'", "$ProgressPreference = 'SilentlyContinue'",
               '$__first = 0']
        pending = False
        for n, line in enumerate(b.run_lines, 1):
            if not pending:
                out.append('$global:LASTEXITCODE = 0')
            out.append(line)
            pending = line.rstrip().endswith(CONTINUES)
            if not pending:
                out.append('$__ok = $?; $__c = if ($LASTEXITCODE) { $LASTEXITCODE } elseif (-not $__ok) { 1 } else { 0 }')
                out.append('Write-Output "%s$__c]"; if ($__c -and -not $__first) { $__first = $__c }' % (MARK % n))
        exe = 'powershell' if sys.platform.startswith('win') else 'pwsh'
        return '\n'.join(out) + '\nexit $__first\n', [exe, '-NoProfile', '-NonInteractive',
                                                      '-ExecutionPolicy', 'Bypass', '-File']
    out = ['set -o pipefail', '__first=0']
    pending = False
    for n, line in enumerate(b.run_lines, 1):
        out.append(line)
        pending = line.rstrip().endswith(CONTINUES)
        if not pending:
            out.append('__c=$?; echo "%s$__c]"; if [ $__c -ne 0 ] && [ $__first -eq 0 ]; then __first=$__c; fi'
                       % (MARK % n))
    return '\n'.join(out) + '\nexit $__first\n', [_bash()]


def _bash() -> str:
    """$AGENTMD_BASH, else bash. On Windows `bash` is often the WSL launcher, so prefer Git Bash."""
    if os.environ.get('AGENTMD_BASH'):
        return os.environ['AGENTMD_BASH']
    if sys.platform.startswith('win'):
        for p in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files (x86)\Git\bin\bash.exe'):
            if os.path.exists(p):
                return p
    return 'bash'


def line_exits(b: Block, output: str) -> List[dict]:
    res = []
    for m in MARK_RE.finditer(output):
        n = int(m.group(1))
        res.append({'line': b.run_lines[n - 1].strip()[:160], 'exit': int(m.group(2))})
    return res


def run_block(b: Block, repo: str, env_extra: dict, timeout: int = 600) -> Block:
    body, cmd = _script(b)
    suffix = '.ps1' if b.lang == 'powershell' else '.sh'
    fd, path = tempfile.mkstemp(suffix=suffix, prefix='agentmd_%s_' % b.id)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body)
    env = dict(os.environ)
    env.update(env_extra)
    env.setdefault('TMPDIR', tempfile.gettempdir())
    t0 = time.time()
    try:
        p = subprocess.run(cmd + [path], cwd=repo, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors='replace', timeout=timeout)
        b.exit_code = p.returncode
        out = p.stdout or ''
    except subprocess.TimeoutExpired as e:
        b.exit_code = -1
        partial = e.stdout.decode('utf-8', 'replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
        out = partial + '\nTIMEOUT after %ss' % timeout
    except FileNotFoundError as e:
        b.exit_code = -2
        out = 'shell not found: %s' % e
    finally:
        b.seconds = round(time.time() - t0, 2)
        try:
            os.unlink(path)
        except OSError:
            pass
    b.line_exits = line_exits(b, out)
    b.output_tail = out[-3000:]
    return b
