# Agent.md - fixture

Agent tooling lives in `tools/` and is not migrated.

## Setup

```bash
conda create -y -p ~/envs/py27 python=2.7.18
export PY2_BIN=~/envs/py27/bin/python
"$PY2_BIN" -V
```

```powershell
& $env:PY2_BIN -V
```

## Test

```bash
"$PY2_BIN" -m unittest discover -s tests -t .
"$PY3_BIN" -m unittest discover -s tests -t .   # expected to fail before migration
"$PY3_BIN" tools/run.py <migrated-dir>
"$PY3_BIN" -c "print('after placeholder')"
```
