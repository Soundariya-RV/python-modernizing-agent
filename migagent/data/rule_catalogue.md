# Rule catalogue — accepted Python 2 → 3 conversions

Source: *Python 2 → 3 Construct Catalogue* (Sample-Rule-Catalogue). This is a
**closed set** and it defines the project scope. Every change the agent makes
must cite one rule ID from this file. A change that no rule covers is not made
silently: it is either a `blocker` or an entry in `decisions.md`.

Each rule lists:

- **Rewrite**: the default transformation.
- **Watch for**: cases where the default is wrong because the right answer
  depends on what the code means. These need evidence from execution, and the
  outcome goes to `decisions.md`.
- **Verify**: what the Verification Gate must exercise to prove the change.

The Code Migration agent finds every site of these rules under Python 2.7
(tokenizer and Python 2 AST, so text inside strings and comments is not
counted) and records which rule justified each change in `migration_state.json`.

## Summary

| ID | Python 2 construct | Python 3 equivalent |
|---|---|---|
| R1 | `print x` (statement) | `print(x)` |
| R2 | `xrange(...)` | `range(...)` |
| R3 | `d.has_key(k)` / `.iteritems()` / `.iterkeys()` / `.itervalues()` | `k in d` / `.items()` / `.keys()` / `.values()` |
| R4 | `except Exception, e:` | `except Exception as e:` |
| R5 | `raw_input()` | `input()` |
| R6 | `unicode` type / `u'...'`, split from `str` | single unified `str` (all text is Unicode) |
| R7 | `basestring` | `str` |
| R8 | `long` type / `123L` literal | unified `int` |
| R9 | Backtick repr `` `x` `` | `repr(x)` |
| R10 | `<>` operator | `!=` |
| R11 | Renamed stdlib modules (`urllib2`, `ConfigParser`, `Queue`, `cStringIO`) | `urllib.request`, `configparser`, `queue`, `io.StringIO` |
| R12 | `exec code in globals, locals` (statement) | `exec(code, globals, locals)` (function) |
| R13 | Integer division relying on Py2 `/` truncation | explicit `//` where truncation was intended; `/` left as true division |


---

## R1 — `print` statement → `print()` function

- **Rewrite:** `print x, y` → `print(x, y)`. `print` → `print()`.
  `print x,` (trailing comma) → `print(x, end=' ')`. `print >>f, x` →
  `print(x, file=f)`.
- **Watch for:** `print (a, b)` prints a *tuple* in Py2, so it becomes
  `print((a, b))`. If the file already has
  `from __future__ import print_function`, it is already Py3 form and needs no
  change.
- **Verify:** compare captured stdout byte for byte, including spacing and
  trailing newlines.

## R2 — `xrange` → `range`

- **Rewrite:** `xrange(a, b, s)` → `range(a, b, s)`.
- **Watch for:** Py2 `range(...)` (not `xrange`) returns a list. If that result
  is indexed after mutation, concatenated, compared with a list, or mutated, it
  becomes `list(range(...))`. Plain iteration needs no wrapper.
- **Verify:** loops that use the range, including empty and negative-step ranges.

## R3 — dict `has_key` / `iter*` methods

- **Rewrite:** `d.has_key(k)` → `k in d`. `d.iteritems()` → `d.items()`.
  `d.iterkeys()` → `d.keys()` (or just `d`). `d.itervalues()` → `d.values()`.
- **Watch for:** Py2 `.items()`, `.keys()` and `.values()` return lists, Py3
  returns views. Wrap in `list()` only when the result is indexed, mutated, kept
  after the dict changes, or the dict is modified while being iterated. Do not
  wrap by reflex: it is a change no rule asked for. `not d.has_key(k)` →
  `k not in d`.
- **Verify:** dict iteration order differs between 2.7 and 3.7+ (a known
  divergence outside the catalogue), so characterization output that depends on
  it must be sorted before comparison.

## R4 — `except X, e` → `except X as e`

- **Rewrite:** `except E, e:` → `except E as e:`. `except (A, B), e:` →
  `except (A, B) as e:`.
- **Watch for:** Py3 deletes `e` at the end of the `except` block. If the code
  reads `e` after the block, bind it to another name inside the block.
- **Verify:** drive the exception path and compare the exception type and message.

## R5 — `raw_input` → `input`

- **Rewrite:** `raw_input(p)` → `input(p)`.
- **Watch for:** Py2 `input(p)` *evaluates* what the user typed. If the original
  calls `input()`, the behaviour-preserving form is `eval(input(p))`. That is a
  security-relevant decision and goes to `decisions.md`.
- **Verify:** feed stdin and compare the returned value and its type.

## R6 — `unicode` / `u'...'` → unified `str`

- **Rewrite:** `unicode(x)` → `str(x)`. A `u'...'` prefix may be dropped (it is
  still legal in 3.3+). `isinstance(x, unicode)` → `isinstance(x, str)`.
- **Watch for:** this is the most meaning-dependent rule. Every Py2 `str` value
  must be classed as **text** (→ `str`) or **bytes** (→ `bytes`): for example
  `struct.pack` results, socket and file `'rb'` data, packed addresses, and
  `.encode()` results. Mixing them raises `TypeError` in Py3. Record each class
  of value in `decisions.md`.
- **Verify:** compare bytes as hex and text as Unicode, and exercise non-ASCII input.

## R7 — `basestring` → `str`

- **Rewrite:** `isinstance(x, basestring)` → `isinstance(x, str)`.
- **Watch for:** if the function also accepted Py2 byte strings, it becomes
  `isinstance(x, (str, bytes))`. Decide from the callers (see R6).
- **Verify:** call it with text, bytes, and a non-string.

## R8 — `long` / `123L` → `int`

- **Rewrite:** `123L` → `123`. `long(x)` → `int(x)`. `(int, long)` → `int` in
  `isinstance` tuples. `(int, long, X)` → `(int, X)`.
- **Watch for:** `hex()`, `oct()` and `repr()` of a Py2 long end in `L`, and Py3
  output does not. Code that strips or parses that `L` must change. `type(x) is
  long` checks become `int`. `sys.maxint` (→ `sys.maxsize`) is a neighbouring
  construct outside the catalogue and goes to `decisions.md`.
- **Verify:** values on both sides of 2³¹, 2³², 2⁶³ and 2⁶⁴. Normalisation
  treats `long` and `int` as one type and drops a trailing `L` on `hex()` output.

## R9 — backticks → `repr()`

- **Rewrite:** `` `x` `` → `repr(x)`.
- **Verify:** compare the repr strings, including for longs (R8 interaction:
  Py2 `` `5L` `` is `'5L'`).

## R10 — `<>` → `!=`

- **Rewrite:** `a <> b` → `a != b`.
- **Watch for:** a class that defines `__cmp__` but not `__ne__` changes
  behaviour in Py3. That is outside the catalogue and goes to `decisions.md`.

## R11 — renamed stdlib modules

- **Rewrite:** `urllib2` → `urllib.request` (and `urllib.error` for
  `URLError`/`HTTPError`). `ConfigParser` → `configparser`. `Queue` → `queue`.
  `cStringIO`/`StringIO` → `io.StringIO`.
- **Watch for:** `StringIO` holding bytes → `io.BytesIO` (see R6). Attribute
  names moved between modules (for example `urllib2.urlopen` →
  `urllib.request.urlopen`, `urllib2.quote` → `urllib.parse.quote`). Every
  attribute use must be remapped, not just the import line.
- **Repo-wide rule (R16):** a rename is all-or-nothing. Every file is migrated
  to the new name in the same step, and any file left on an old name fails the
  run.
- **Verify:** import every module under Py3 and exercise the code paths that
  use the moved attributes.

## R12 — `exec` statement → `exec()` function

- **Rewrite:** `exec code` → `exec(code)`. `exec code in g` → `exec(code, g)`.
  `exec code in g, l` → `exec(code, g, l)`.
- **Watch for:** in Py3, a bare `exec` inside a function cannot create or
  rebind that function's local variables. If later code relies on that, pass an
  explicit dict and read from it. Log this in `decisions.md`.
- **Verify:** compare the namespace contents after `exec`.

## R13 — integer division

- **Rewrite:** `a / b` where both operands are integers and truncation was
  intended → `a // b`. `/=` → `//=` under the same condition. `__div__` →
  `__floordiv__` and/or `__truediv__`.
- **Leave as `/`:** if either operand can be a float, if true division was
  intended, or if the module already has `from __future__ import division`.
- **Watch for:** the decision depends on the **runtime operand types**, not the
  syntax. That is why lib2to3 leaves `/` alone and gets it wrong. Determine the
  types by running the Py2 code on characterization inputs. Py2 int `/` floors
  (`-7 / 2 == -4`), which matches `//`, not `int(a / b)`. Record each ambiguous
  site in `decisions.md`.
- **Verify:** negative operands, odd/even pairs, large ints (true division of
  big ints loses precision as a float), and type checks on the result.

---

## Repository-level checks

These are named in the problem statement and enforced by the Cross-File
Consistency agent. They are invariants over the whole repo, not code rewrites.

| ID | Check |
|---|---|
| R16 | Renames are all-or-nothing: every R11 module is either fully migrated across the repo or untouched. |
| R29 | Modules in an import cycle are migrated and verified as one unit. |
| R31 | The public API is unchanged: function names, signatures and return types that other files depend on stay the same. If a rule forces a change, every caller is updated in the same commit and the change is recorded. |
| — | After every commit, every module still imports under Python 3. |

## Outside the catalogue

Py2 constructs that no rule covers are **not** rewritten silently. Examples: a
`raise E, v` statement, `0755` octal literals, `__nonzero__`/`__hex__`/`__cmp__`
methods, `sys.maxint`, `bytes is str` compatibility probes, packaging that needs
a newer toolchain.

Each one becomes either a `blocker` in `migration_state.json` or a
`decisions.md` entry stating keep, remove or accept-divergence, and why. Other
divergences known to fall outside the catalogue: `math.floor`/`ceil` return
type, `chr(n > 127)`, dict iteration order, list-comprehension variable leakage,
octal `0NNN` literals, `__nonzero__`/`__cmp__`, and the laziness of
`map`/`filter`/`zip`.
