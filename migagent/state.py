"""The agent's resumable record: migration_state.json, progress.md, decisions.md.

All three live in the target repository root next to Agent.md. Writes are
merge-not-overwrite: a later agent's work (status, rules, history) is never
thrown away when the dependency map is rebuilt.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Dict, List, Optional

STATE_FILE = 'migration_state.json'
PROGRESS_FILE = 'progress.md'
DECISIONS_FILE = 'decisions.md'
SCHEMA_VERSION = 1
STATUSES = ('pending', 'in_progress', 'passing', 'failing', 'blocked')


def read_text(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec='seconds')


# ---------------------------------------------------------------- state.json
def load_state(repo: str) -> Optional[dict]:
    p = os.path.join(repo, STATE_FILE)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def write_state(repo: str, state: dict) -> str:
    p = os.path.join(repo, STATE_FILE)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(state, f, indent=2, sort_keys=False)
        f.write('\n')
    os.replace(tmp, p)
    return p


def merge_files(old: Optional[dict], new_files: List[dict], excluded_dirs: List[str] = ()) -> List[dict]:
    """Keep what later agents own (status, rules, history, blockers).

    A file that is no longer in the graph because its directory is now excluded
    is dropped if no agent touched it yet; one already migrated is kept, marked
    excluded, so finished work never disappears silently.
    """
    prev: Dict[str, dict] = {f['path']: f for f in (old or {}).get('files', [])}
    out = []
    for f in new_files:
        o = prev.pop(f['path'], None)
        if o:
            for k in ('status', 'rules', 'history', 'attempts', 'last_verified', 'migration', 'changes',
                      'planned_changes', 'applicable_rules', 'verification', 'needs_repair'):
                if k in o:
                    f[k] = o[k]
            f['decisions'] = sorted(set(f.get('decisions', [])) | set(o.get('decisions', [])))
            # blockers raised by other agents survive; ours are recomputed
            f['blockers'] = [b for b in o.get('blockers', []) if b.get('agent') != 'dependency_mapper'] + f.get('blockers', [])
        out.append(f)
    for path, o in sorted(prev.items()):   # file vanished from the graph
        o = dict(o)
        if any(path == d or path.startswith(d.rstrip('/') + '/') for d in excluded_dirs):
            if o.get('status', 'pending') == 'pending' and not o.get('migration'):
                continue
            o.setdefault('notes', []).append('in an excluded directory since %s' % now())
            o['excluded'] = True
            out.append(o)
            continue
        o['status'] = o.get('status', 'pending')
        o.setdefault('notes', []).append('not found by dependency_mapper at %s' % now())
        o['missing_from_repo'] = True
        out.append(o)
    return out


# ---------------------------------------------------------------- decisions.md
DEC_HEADER = re.compile(r'^### (D-\d{3,})\b.*$', re.M)
DEC_FIELD = re.compile(r'^- \*\*(\w[\w ]*):\*\* ?(.*)$', re.M)


def load_decisions(repo: str) -> Dict[str, dict]:
    """key -> {id, decision, reason, ...} parsed from decisions.md."""
    p = os.path.join(repo, DECISIONS_FILE)
    if not os.path.exists(p):
        return {}
    text = read_text(p)
    out: Dict[str, dict] = {}
    heads = list(DEC_HEADER.finditer(text))
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        fields = {m.group(1).strip().lower(): m.group(2).strip() for m in DEC_FIELD.finditer(body)}
        key = fields.get('key', '').strip('`')
        if key:
            out[key] = {'id': h.group(1), **fields, 'key': key}
    return out


def next_decision_id(repo: str) -> int:
    p = os.path.join(repo, DECISIONS_FILE)
    if not os.path.exists(p):
        return 1
    ids = [int(m.group(1)[2:]) for m in DEC_HEADER.finditer(read_text(p))]
    return max(ids, default=0) + 1


def append_decisions(repo: str, entries: List[dict]) -> Optional[str]:
    if not entries:
        return None
    p = os.path.join(repo, DECISIONS_FILE)
    new = not os.path.exists(p)
    with open(p, 'a', encoding='utf-8', newline='\n') as f:
        if new:
            f.write('# Decisions\n\nAmbiguous cases and the decision taken. When the same **Key** '
                    'comes up again (in any file, in any session), the agent reuses the decision '
                    'instead of deciding again. Edit a **Decision** line to override it; the next '
                    'run picks the change up.\n')
        for e in entries:
            f.write('\n### %s - %s\n\n' % (e['id'], e['title']))
            for label in ('Key', 'Kind', 'Action', 'Option', 'Where', 'Context', 'Decision', 'Reason',
                          'Agent', 'Model', 'Date'):
                v = e.get(label.lower())
                if v:
                    f.write('- **%s:** %s\n' % (label, '`%s`' % v if label == 'Key' else v))
    return p


# ---------------------------------------------------------------- progress.md
def append_progress(repo: str, text: str) -> str:
    p = os.path.join(repo, PROGRESS_FILE)
    new = not os.path.exists(p)
    with open(p, 'a', encoding='utf-8', newline='\n') as f:
        if new:
            f.write('# Progress\n\nHuman-readable log of what the migration agents did, newest '
                    'last. Machine state is in `migration_state.json`; ambiguous calls are in '
                    '`decisions.md`.\n')
        f.write('\n' + text.rstrip() + '\n')
    return p
