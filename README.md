# python-modernizing-agent: Python 2.7 → 3 migration agents (Qwen 32B)

An agentic system that migrates a Python 2.7 repository to Python 3.11 and
proves every change by running the code under both interpreters, never by a
model's judgement. Four agents, one entry point:

**init** → **Dependency Mapping** → **Code Migration** → **Verification Gate** → **Cross-File Consistency**

It runs on **any Python 2 repository**, a local folder or a git URL. A repo
without an `Agent.md` gets one from the `init` stage, and the default
`rule_catalogue.md` if it has none.

| Stage | Agent | Demo steps | Writes |
|---|---|---|---|
| `init` | (code only, no model) | makes any repo ready: inspects files and tests, runs every build/test command under Py2 and Py3, writes what happened | `Agent.md`, `rule_catalogue.md` |
| `map` | Dependency Mapping | 1–2: verify Agent.md commands, map imports, migration order | `migration_state.json` |
| `migrate` | Code Migration | 3–4: find catalogue rules, apply them one file at a time | migrated files, `files[].rules/changes` |
| `verify` | Verification Gate | 5: tests, verification command, characterization under Py2 vs Py3 | `passing` / `needs_repair` / `blocked` |
| `consistency` | Cross-File Consistency | R16, R29, Py3 imports, R31 across the repo | `consistency` verdict |

Every agent appends to the target repo's `progress.md` and `decisions.md`.
Facts (parsing, exit codes, comparisons) are computed by code; Qwen only makes
the judgement calls, through tools that validate them.

## Quick start

```bash
cd python-modernizing-agent
python3 modernize.py -repo ../ipaddr-py --check-llm   # model server answers with tool calls?
python3 modernize.py -repo ../ipaddr-py               # all stages, resumes from migration_state.json
python3 modernize.py -repo ../ipaddr-py --status      # where it stands (no model needed)

# any other repository: a folder, or a git URL (cloned into ../repos/) at the Python 2 baseline
python3 modernize.py -repo ~/src/legacy-app
python3 modernize.py -repo https://github.com/python-mechanize/mechanize --ref v0.3.7
```

Before running on a new repo, install its third-party dependencies into both
interpreters (`~/envs/py27/bin/pip install ...`, same for py311). The mapper
reports anything missing as an `unavailable_import` issue.

| Option | Meaning |
|---|---|
| `-repo` / `--repo PATH\|URL` | the Python 2 repository: a folder (a bare name is also looked up next to this folder) or a git URL |
| `--ref REF`, `--workdir DIR` | git tag/branch/commit to use as the Python 2 baseline; where URLs are cloned (default `../repos`) |
| `--stage all\|init\|map\|migrate\|verify\|consistency` | `all` (default): init if there is no `Agent.md`, map if not mapped yet, then the rest in order |
| init: `--reinit` | write `Agent.md` again |
| `--file PATH` | migrate / verify only the unit or file that contains it |
| `--status`, `--check-llm` | report and exit |
| `--backend rules` | offline stand-in for the model: plumbing checks only, never results |
| `--base-url`, `--model` | default `$QWEN_BASE_URL` / `$QWEN_MODEL`, else the first local server that answers (`:8000` vLLM or llama.cpp, `:11434` Ollama) and its Qwen 32B model |
| `--py2`, `--py3` | default `$PY2_BIN` / `$PY3_BIN`, else `~/envs/py27` and `~/envs/py311` |
| `--results DIR` | each run writes `DIR/<repo>-<time>-<stage>/` with `console.log`, `before/` and `after/` record files, `runs/` trajectories, `status.txt`, `EXIT` (default `../results`) |
| map: `--exclude DIR`, `--remap`, `--shell` | directories to skip, map again, which Agent.md blocks to run |
| migrate: `--retry-blocked`, `--max-units N`, `--commit` | retry files blocked by this agent after editing `decisions.md`; limit; git commit each unit |
| verify: `--reverify`, `--min-cases N`, `--min-model-cases N` | verify again; characterization cases required |
| `--max-tokens` (1024), `--llm-timeout` (3600), `--max-steps` | per reply / per request / per session |

Long runs belong in `screen` (or `tmux`); the results folder keeps everything:

```bash
screen -dmS modernize python3 modernize.py -repo ../ipaddr-py
tail -f ../results/ipaddr-py-*-all/console.log
```

## Results so far

**ipaddr-py** (Demo Repository 1, commit `19341c5`), Qwen2.5-Coder-32B (Q4_K_M) via Ollama:

| File | Status | Evidence |
|---|---|---|
| `ipaddr.py` | passing | 70/70 tests same outcome under Py2 and Py3; characterization equivalent (102 cases, 22 written by Qwen: 95 match, 7 interpreter-only divergences on invalid input accepted in `decisions.md`); rules R2, R8 |
| `ipaddr_test.py` | passing | 70/70 tests same outcome; rules R3, R8 |
| `setup.py` | needs repair | `setup.py build` fails on Py3 with recent setuptools: `InvalidVersion: 'trunk'` (no catalogue rule covers it) |

Cross-File Consistency: consistent. Compared with lib2to3 on the same baseline:
27 lines changed instead of 28 (no unrequested `list(d.items())`), every change
linked to a rule or a decision, the `__hex__` and `bytes is str` Py2 leftovers
handled through recorded decisions, and the build blocker reported instead of
passed silently.

**mechanize v0.3.7** (Demo Repository 2, 54 files): the whole pipeline runs
end to end from a git URL with the offline backend. A Qwen run is still to do;
it needs `html5lib` and `twisted` installed in both interpreters.

## Known limitations

- Speed: on one 11 GB GPU with RAM offload, Qwen 32B writes about 1.5 tokens/s,
  so one stage of a single-file repo takes minutes to tens of minutes.
- Qwen2.5-Coder repeats tool calls and writes them as plain text. The client
  parses every text form, runs identical calls once, caps calls per reply and
  keeps the complete calls of a reply cut off by the token limit.
- The model makes judgement calls the tools cannot check, and it is sometimes
  wrong (it once blocked a working compatibility probe, once flagged a safe API
  change). Every such call is in `decisions.md`; edit its `Option` line to
  override it, and the next run reuses your answer.
- There is no Repair agent: files the Gate finds not equivalent stay
  `in_progress` with `needs_repair` and the failure log.

## Setup

The agent host needs Python 3.8+ with **no packages**, plus the two interpreters:

```bash
conda create -y -p ~/envs/py27  python=2.7.18
conda create -y -p ~/envs/py311 python=3.11
```

Qwen 32B behind any OpenAI-compatible server:

| Server | Command | Notes |
|---|---|---|
| vLLM (2x 80 GB, or 1x 48 GB with `-AWQ`) | `vllm serve Qwen/Qwen2.5-Coder-32B-Instruct --tensor-parallel-size 2 --max-model-len 32768 --enable-auto-tool-choice --tool-call-parser hermes --port 8000` | structured tool calls |
| llama.cpp (small GPU + RAM) | `./serve_qwen_llamacpp.sh` (Q4_K_M GGUF, port 8000) | ~0.7 tokens/s on an 11 GB GPU |
| Ollama | `ollama pull qwen2.5-coder:32b`, then a Modelfile with `PARAMETER num_ctx 32768` | the default 2k context silently truncates; set `OLLAMA_KEEP_ALIVE=-1` |

Qwen2.5-Coder sometimes writes tool calls as text (`<tool_call>`, `<tools>` or
bare JSON, one or several per reply); `llm.py` parses all of them. Repeated
identical calls in one reply run once, at most 8 calls per reply. For Qwen3-32B
pass `--model Qwen/Qwen3-32B --no-thinking` (untested).

## Layout

```
modernize.py                 the single entry point (CLI)
serve_qwen_llamacpp.sh       serve the Q4_K_M GGUF with llama.cpp
migagent/
  cli.py                     shared options, interpreters, model server detection
  results.py                 results folder per run, --status report
  stages/                    one runner per stage: init, mapper, migrate, verify, consistency
  data/rule_catalogue.md     the default catalogue copied into repos that have none
  llm.py  loop.py            OpenAI-compatible client, tool loop with JSONL trajectory
  agent_md.py  state.py      Agent.md runner; migration_state.json / progress.md / decisions.md
  trees.py                   Py2 baseline tree and migrated tree (throwaway copies)
  depmap/                    Dependency Mapping: py2_scan, py2_probe, analyze, graph, tools, prompts
  codemig/                   Code Migration: py2_sites (finder), py2_divprobe (R13), rewrite, tools
  verify/                    Verification Gate: testrun, charrun, compare, py2_api, tools
  consistency/               Cross-File Consistency: checks, tools
tests/                       54 tests, fixtures/py2repo (mapping) and fixtures/py2mig (every rule)
runs/                        trajectories (generated, git-ignored)
```

Generated files are never part of this code: `migration_state.json`,
`progress.md`, `decisions.md` and `.migration/` live in the target repo, and
`runs/` and `results/` are git-ignored.

## Tests

```bash
PY2_BIN=~/envs/py27/bin/python PY3_BIN=~/envs/py311/bin/python python3 -m unittest discover -s tests -v
```

## Dependency Mapping Agent

Demo steps 1 and 2: read the target repo's `Agent.md` and **verify every
command**, then **map dependencies** (imports and file references), **compute
the migration order** and write `migration_state.json`. It does not edit code.
On Linux it runs the **bash** blocks of Agent.md, on Windows the PowerShell
ones (`--shell` to override, `AGENTMD_BASH` to pick a bash binary).

### Who decides what

| Decided by code (facts) | Decided by Qwen (judgement, validated by the tools) |
|---|---|
| Parsing every `.py` file **under Python 2.7** (`py2_scan.py`) | Which Agent.md failures are *expected* (it must quote Agent.md) |
| Where each module comes from, asked of **both real interpreters** (`py2_probe.py`) | How to resolve each `needs_decision` issue: `decide` (logged to `decisions.md`) or `block` |
| Import graph, cycles (R29 units), dependency-first order | Which directories are tooling rather than code (`exclude_dirs`) |
| Exit code of every Agent.md line | Verification-command overrides, with a reason |
| What is allowed to run: setup lines, `<placeholders>` and env overrides are dropped | The progress summary |

The model cannot reorder files, invent an import or call a non-zero exit
"verified". The `finalize` tool refuses to write state until every Agent.md
block has a verdict and every issue that needs a decision is resolved. An
issue whose key is already in `decisions.md` reuses that decision. On a re-run,
`migration_state.json` keeps the statuses and rules that later agents set.

#### Issues the analysis raises

| Kind | Severity | Example |
|---|---|---|
| `implicit_relative_import` | needs_decision | `import models` inside package `shop/`: Python 3 treats it as absolute |
| `py2_only_stdlib` | needs_decision (info if guarded by try/if) | `urlparse`. Not covered by the catalogue, which only accepts R11 renames |
| `stdlib_removed_later` | needs_decision | `distutils`: present in 3.11, removed in 3.12 |
| `third_party_missing_py3`, `unavailable_import` | needs_decision | a package that is installed for Py2 only, or not at all |
| `dynamic_code` | needs_decision | `__import__(name)`, `exec`, `eval` with a non-literal argument |
| `parse_error` | needs_decision | the file is not valid Python 2 |
| `renamed_stdlib` | info (R11, all-or-nothing per R16) | `urllib2`, `ConfigParser`, `Queue`, `cStringIO` |
| `import_cycle` | info (R29 unit) | `models` ↔ `pricing` |
| `missing_file_ref`, `module_name_collision` | warning | `open('missing.cfg')` |

## Code Migration Agent

Demo steps 3 and 4: identify every applicable `rule_catalogue.md` rule in each
file, then apply the migration one file at a time. It reads the Dependency
Mapper's `migration_state.json` and migrates, in `migration_order`, each unit
whose files are all `pending` (units with a `blocked` file are skipped). Each
unit is one short model session, which keeps the context small.

```bash
python3 modernize.py -repo ../ipaddr-py --stage migrate                    # every pending unit
python3 modernize.py -repo ../ipaddr-py --stage migrate --file ipaddr.py   # one unit
python3 modernize.py -repo ../ipaddr-py --stage migrate --retry-blocked    # after editing decisions.md
```

| Decided by code (facts) | Decided by Qwen (judgement, validated by the tools) |
|---|---|
| Every R1–R13 site and its candidate rewrites, found under **Python 2.7** (`codemig/py2_sites.py`, tokenizer + Py2 AST) | The option for each site whose answer depends on meaning (the catalogue's "Watch for": `hex(long(x))`, a stored `range()`, `unicode`, `basestring`, `exec` in a function, StringIO text vs bytes) |
| Mechanical sites (`xrange`, `has_key`, `123L`, `(int, long)`, `print x`, `<>`, backticks ...) applied without asking | Python 2 constructs outside the catalogue (rule `OUT`: `raise E, v`, octal `0755`, `__nonzero__`, `__hex__`, `sys.maxint`, `bytes is str` probes ...): choose an option, `keep` or `block`, recorded in `decisions.md` |
| R13 evidence: the file's verification command runs under Py2 with every `/` instrumented (`codemig/py2_divprobe.py`) and reports the operand types per line | `floor` / `true` / `block` for each division, from that evidence |
| Rendering (nested rewrites kept, overlaps refused), Py3 compile check, residual scan for Py2 constructs left | Edits no option covers (`edit_lines`, must cite a rule or decision id) and the summary |

Guarantees enforced by the tools:

- Nothing is written before `finish_unit`, and it refuses while a site has no
  choice, a file does not compile under Python 3, or a Py2 construct is left
  that no choice kept.
- A file with a `block` choice is left untouched and marked `blocked` with the
  reason. Migrated files become **`in_progress`**: passing/failing is the
  Verification Gate's call.
- Every change is recorded in `files[].changes` with its rule, `files[].rules`
  lists the catalogue rules applied, `files[].applicable_rules` counts what was
  found. `files[].migration` keeps the Py2 baseline: the git commit
  (`baseline_ref`) when the file was tracked and clean, else a copy in
  `.migration/baseline/`.
- Decision keys are pattern-level where the answer does not depend on context
  (`OUT:raise-comma`, `OUT:octal-literal`, `R8:long-in-hex`), so the first answer
  is reused in every later file. The `Option` field of a `decisions.md` entry
  is what gets reused; edit it to override.
- The R13 probe runs on a copy of the repo with already-migrated files restored
  to their Py2 baseline, so evidence for later files is still Python 2 behaviour.

On ipaddr-py the finder reports the same 27 catalogue occurrences as Agent.md
§6 (R2 x5, R3 x12, R8 x10), plus the `bytes is str` probe and `__hex__` as
`OUT`. With the default rewrites alone, the Py3 suite passes 70/70 and
characterization matches 1091/1093 (the two pickle divergences come from the
interpreter).

Tests: `tests/test_code_migration.py` (12 tests) uses `tests/fixtures/py2mig`,
a module with every rule, R13 divisions of both kinds and nested rewrites. One
test migrates it with the right choices and runs its test suite under Python 3.

## Verification Gate

Demo step 5: prove each migrated file behaves like its Python 2 original by
running code under both interpreters. It verifies every file that the Code
Migration agent set to `in_progress`, in migration order.

```bash
python3 modernize.py -repo ../ipaddr-py --stage verify                  # every migrated file
python3 modernize.py -repo ../ipaddr-py --stage verify --file ipaddr.py --reverify
```

Both sides run in throwaway copies: the **Python 2 baseline** (migrated files
restored from `files[].migration.baseline_ref` or the `.migration/baseline/`
copy) and the **migrated tree**, with `PYTHONHASHSEED=0`.

| Check | When | Equivalent when |
|---|---|---|
| Tests | the verification command runs a file of kind `test` | every test has the same outcome (and exception type) under Py2 and Py3 (`verify/testrun.py`) |
| Command | any other verification command (`setup.py build`, compile checks) | it succeeds under Py3 where it succeeds under Py2 |
| Characterization | `library` files, or `needs_characterization_tests` | every case gives the same result: value, exception type and printed output (`verify/charrun.py`) |

Characterization cases are single expressions, valid in Python 2 and 3, that
Qwen writes from the module's public API (`verify/py2_api.py`) and the
catalogue's "Verify" hints for the rules applied to the file. `seed_cases` adds
generated calls with a fixed value pool. Cases are rejected unless they compile
under both interpreters and avoid non-deterministic calls (`hash`, `id`, time,
files, ...). They are kept in `.migration/characterization/<file>.json` with the
Py2 recording and reused on the next run. The only normalisations are the
catalogue's: long = int (R8), Py2 `str` compared as bytes/text (R6), unordered
containers sorted (R3), memory addresses masked.

| Verdict | Status | Recorded |
|---|---|---|
| equivalent | `passing` | `files[].verification` with every check |
| not_equivalent | stays `in_progress`, `needs_repair: true` | each difference with Py2/Py3 values or the test traceback (for the Repair agent) |
| unverifiable (the Py2 baseline cannot run the check) | `blocked` | a blocker |

Qwen may `accept_divergence` for a characterization case whose difference comes
from the interpreter (recorded in `decisions.md`, key
`divergence:<file>:<expr>`, reused next time). Test and command differences
cannot be accepted. Tests: `tests/test_verification_gate.py` (7 tests),
including a deliberately wrong R13 choice that the Gate must catch.

## Cross-File Consistency Agent

Repository-wide checks once files are migrated and verified
(`python3 modernize.py -repo ../ipaddr-py --stage consistency`):

| Check | Decided by code | Qwen |
|---|---|---|
| R16 renames all-or-nothing | old module names still imported anywhere, against files that migrated that module | — (a violation fails the run) |
| R29 cycles together | every file of a cycle unit has one status and one migration unit | — |
| Python 3 imports | every library and test module imports in the migrated tree; scripts compile | — |
| R31 public API unchanged | public and special members and signatures, Py2 baseline vs migrated; references in other files; implicit uses (`hex(x)` called `__hex__` in Py2); which decision recorded the change | accept (recorded in `decisions.md`, key `api:<file>:<name>`, reused) or violation |

Verdict `consistent` / `inconsistent` goes to `migration_state.json`
(`consistency`). A file with a violation gets `needs_repair`, and a `passing`
one goes back to `in_progress`. Tests: `tests/test_consistency.py`.

