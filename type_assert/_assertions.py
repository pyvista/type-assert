"""The one assertion a typing case makes.

To a type checker `assert_types` is `typing_extensions.assert_type`; at runtime it
is a real checker. See the module body for why that works.

The runtime check is assignability, read strictly in two places where a declared
type and a produced value drift apart in practice: a number has to be an instance
of the numeric class named, so an `int` does not pass for `float`, and a NumPy
array is checked as the array type it actually is, dtype and dimensions included.

The expected type may be quoted, as annotations may. A checker reads the string as
the type it names; at runtime the string is evaluated in the caller's namespace, so
it means the same thing to both halves.
"""

from __future__ import annotations

import functools
import sys
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from types import FrameType

    # A checker resolves an aliased import back to its original definition, so it
    # applies its `assert_type` special case here: the inferred type must match
    # `expected` *exactly*, not merely be assignable to it. Verified against both
    # mypy and pyright. At runtime the definition below runs instead and checks the
    # value, so one call covers both halves and they cannot drift apart.
    from typing_extensions import assert_type as assert_types
else:
    from pycroscope.checker import Checker
    from pycroscope.runtime import CanAssignError
    from pycroscope.runtime import KnownValue
    from pycroscope.runtime import Relation
    from pycroscope.runtime import has_relation
    from pycroscope.runtime import type_from_runtime

    from ._exact import mismatch

    @functools.cache
    def _checker() -> Checker:
        """Return the shared checker, built on first use."""
        return Checker()

    def assert_types(value: object, expected: Any) -> object:
        """Assert `value` is assignable to `expected` at runtime, and return it."""
        if isinstance(expected, str):
            expected = _resolve(expected, sys._getframe(1))
        # pycroscope's own `get_assignability_error` memoises against a module-global
        # checker, which keeps every checked value alive for the rest of the session.
        # Use our own so the memo can be dropped after each check.
        checker = _checker()
        try:
            relation = has_relation(
                type_from_runtime(expected), KnownValue(value), Relation.ASSIGNABLE, checker
            )
            if isinstance(relation, CanAssignError):
                msg = (
                    f'Runtime value of type {type(value).__name__!r} is not assignable '
                    f'to the expected type:\n\t{expected}\n\n{relation.display(depth=0)}'
                )
                # An assertion that failed, not a caller passing the wrong kind of argument.
                raise AssertionError(msg)  # noqa: TRY004
            problem = mismatch(value, expected, checker=checker)
        finally:
            cache = checker.get_relation_cache()
            if cache is not None:
                cache.clear()

        if problem is not None:
            msg = (
                f'Runtime value of type {type(value).__name__!r} does not have the '
                f'expected type:\n\t{expected}\n\n{problem}'
            )
            raise AssertionError(msg)
        return value

    def _resolve(expected: str, frame: FrameType) -> object:
        """Build a quoted type in the caller's namespace, where a checker also reads it.

        Left as a string, pycroscope would take it for an unresolvable forward
        reference and check against `Any`, which passes for every value.
        """
        try:
            return eval(expected, frame.f_globals, frame.f_locals)
        except Exception as error:
            msg = (
                f'The expected type {expected!r} cannot be built at runtime '
                f'({type(error).__name__}: {error}). A case file skips the runtime half '
                f'of such a case; elsewhere, spell a type that exists at runtime.'
            )
            raise TypeError(msg) from error
