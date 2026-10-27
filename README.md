# type-assert

pytest plugin that checks a value's static type and its runtime type in one assertion.

A type checker only ever sees the annotations. A runtime checker only ever sees the
values. Either can be right while the other is wrong, and overloaded signatures are
where they drift apart. `type-assert` pins both halves at once, from one line:

```python
assert_types(json.loads('[1]'), Any)
assert_types(sorted({'b', 'a'}), list[str])
```

Each line becomes two tests. One runs the expression and checks the value it produced.
The other checks what a type checker inferred for the same line. The line only passes
if the two agree.

> **Warning** — The API of this package is unstable and likely to change between
> minor versions (for example `0.1.0` to `0.2.0`). Pin the exact version you
> depend on, for example `type-assert==0.1.0`.

## Installation

```bash
pip install type-assert[mypy]     # or [pyright], [pyrefly], or [all]
```

The checker itself is an extra, because it should be whichever one your project
already uses.

## Usage

Put a directory of case files somewhere in your test tree and point the plugin at it:

```toml
[tool.pytest.ini_options]
type_assert_cases = 'tests/typing/cases'
```

Every `.py` file under that directory is a case file, subdirectories included, so cases
can be grouped the way the package they exercise is.

A case file is an ordinary Python module. Every top-level `assert_types` call is a case;
everything else — imports, helpers, constants — is setup shared by the cases in that
file:

```python
from __future__ import annotations

import json
from typing import Any

from type_assert import assert_types


def payload() -> str:
    """Return a document to parse."""
    return '{"a": 1}'


assert_types(json.loads(payload()), Any)
assert_types(sorted({'b', 'a'}), list[str])
assert_types(''.join([]), str)
```

Running pytest collects each case file as a test file of its own:

```text
tests/typing/cases/basics.py::setup
tests/typing/cases/basics.py::sorted({'b', 'a'}) -> list[str] [runtime]
tests/typing/cases/basics.py::sorted({'b', 'a'}) -> list[str] [static]
```

## How `assert_types` does both

To a type checker, `assert_types` *is*
[`typing.assert_type`](https://docs.python.org/3/library/typing.html#typing.assert_type),
aliased under `TYPE_CHECKING`. Checkers resolve an aliased import back to its original
definition, so the special case still applies: the inferred type must match the second
argument **exactly**, and a supertype is a failure rather than a pass.

At runtime that name is bound to a real checker instead, backed by
[pycroscope](https://pycroscope.readthedocs.io/), which walks containers exhaustively —
it catches a `None` at any position in a `list[int]`, not only the first element.

Writing the type once covers both halves, and there is no way for them to drift apart.

## What each half checks

The checker's `assert_type` is exact: the inferred type must be the expected type, so
`object` fails for an `int` and `list[float]` fails for a `list[int]`. The runtime
check is assignability, the relation the type system itself uses for a value: an
instance of a subclass passes for its base class, and every element of a container is
held to the same rule.

Three things are read more strictly than the type system would, because they are where
a declared type and a produced value drift apart in practice:

- A number has to be an instance of the numeric class named. An `int` does not pass for
  `float`, a `bool` does not pass for `int`, and `[1, 2]` does not pass for
  `list[float]`. A function that really returns either should say `float | int`.
- A NumPy array is checked as the array type it actually is, dtype and number of
  dimensions included, so a `float64` array does not pass for `NDArray[np.int64]`
  and a 1-D array does not pass for `ndarray[tuple[int, int], ...]`.
- A container is walked item by item, a class of your own as much as a built-in one. A
  class reaches its item type through the arguments it passes to the `Sequence`, `Set`
  or `Mapping` it derives from, so `Blocks[Polygon]` rejects a `Blocks` holding a
  `Circle` exactly as `list[Polygon]` would.

All three apply at any depth. The last one is followed through a base class of your own
and through whichever parameter the class actually passes down, so a class whose item
type is its *second* argument is read correctly rather than assumed. An `Iterator` is
never walked, since reading it would consume the value the case is about.

What the runtime half still cannot check is a type argument no value carries: a
`Result[int]` wrapping a single value is only a `Result` at runtime, and an empty
container agrees with every item type. There the checker is the sole authority. A
supertype still fails only the static half, which is the intended division of labour:
the checker guards what was declared, the run guards what was produced, and a case
passes only when the declaration is exact and the value honours it.

## Types that only a checker can spell

Some types have no runtime spelling: a name imported under `TYPE_CHECKING`, or a class
a checker treats as generic that cannot be subscripted at runtime, such as
`np.dtype[np.generic[object]]`. Write the type as a string, the way an annotation can be
quoted:

```python
assert_types(dtype_of(array), 'np.dtype[np.generic[object]]')
```

The checker reads the string as the type it names and holds the case to it exactly. At
runtime the string is evaluated in the case file's namespace. When that succeeds the
value is checked against it as usual, so a wrong quoted type still fails both halves.
When the type cannot be built, the runtime half is skipped with the reason, since there
is nothing to check the value against.

To keep a runtime check as well, name the type under `TYPE_CHECKING` and give it a
runtime stand-in:

```python
if TYPE_CHECKING:
    DType = np.dtype[np.generic[object]]
else:
    DType = np.dtype

assert_types(dtype_of(array), DType)
```

The checker still sees the exact type; the value is checked against the stand-in.

## Choosing a checker

```toml
[tool.pytest.ini_options]
type_assert_checkers = 'mypy'                   # the default
type_assert_checkers = 'pyright'
type_assert_checkers = 'mypy pyright pyrefly'   # each with its own test
```

Naming more than one gives every case a static test per checker, so a case has to hold
under all of them:

```text
cases/basics.py::sorted({'b', 'a'}) -> list[str] [runtime]
cases/basics.py::sorted({'b', 'a'}) -> list[str] [static: mypy]
cases/basics.py::sorted({'b', 'a'}) -> list[str] [static: pyright]
```

The runtime test is not repeated, since the value does not depend on who checked it.
Bear in mind that two checkers do not always infer the same type for the same
expression, so a case that satisfies one may need rewording to satisfy both.

`ty` is deliberately not supported yet: it is pre-1.0 and its output format is still
moving. Adding a backend is a single module — see `type_assert/_checkers/`.

## Skipping a case at runtime

A case that cannot run everywhere — it crashes on a platform, or needs something that is
not always installed — goes in a `skip_runtime` block. A case that expects `Never` does
not belong here: its expression raising is the claim it makes, and the runtime half
checks it.

```python
from type_assert import assert_types, skip_runtime

with skip_runtime(reason='why running it fails here'):
    assert_types(an_expression(), int)
    assert_types(another_expression(), str)

with skip_runtime(sys.platform == 'win32', reason='why it fails on Windows'):
    assert_types(a_third_expression(), bytes)
```

Only the runtime half is skipped; the checker still checks each case. The arguments are
evaluated after the file's setup has run. A block holds only cases and binds no name.

A case that never runs leaves its lines uncovered. This coverage pattern excludes a block
without a condition:

```toml
[tool.coverage.report]
exclude_also = ['^with skip_runtime\(\s*reason=']
```

## A case that never returns

A case can claim that a call raises, by expecting `Never`:

```python
assert_types(pointset().slice(), Never)
```

To a checker that expression ends the module: everything after it is unreachable, and
mypy and pyright check none of it, so a wrong case further down would pass. The plugin
therefore hands each checker the file with every case guarded, which keeps each one
reachable without changing its scope or its line number, so `Never` cases can sit
anywhere and in any number.

The runtime half runs the expression on its own and passes when it does not return,
so the case is a real assertion rather than something to skip: an expression that
returns a value fails it, naming what came back. The assertion itself is not run,
because the call raises while its own arguments are being evaluated, before
`assert_types` is entered. `NoReturn` is read the same way as `Never`, and a case
in a `skip_runtime` block is still skipped. mypy reads the guarded text through `--shadow-file`. pyright
has no equivalent, so it reads a copy of the cases directory made outside the project:
imports relative to that directory resolve inside the copy, absolute ones resolve from
the project as before, and settings scoped to the directory's path do not reach it.
pyrefly checks unreachable code on its own and reads the files as they are.

## Running the cases on their own

Case files collect like any other test file, so a job can run just them:

```bash
pytest tests/typing/cases --no-cov
```

pytest-cov's `--no-cov` matters when the project sets a coverage threshold in
`addopts`: the cases exercise only what they call, so `--cov-fail-under` would fail a
run that is only about types. Runs of the whole suite are unaffected.

## License

MIT
