"""Split a case file into the setup it needs and the cases it declares."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

from typing_extensions import Never

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from types import CodeType

ASSERTION = 'assert_types'
SKIP_BLOCK = 'skip_runtime'
#: Prefixed to each case when a checker reads the file. See `_guarded`.
GUARD = 'if bool(): '


class CaseError(Exception):
    """Raised when a case file is not shaped the way the framework expects."""


class CaseSkipped(Exception):  # noqa: N818
    """Raised instead of running a case the file asks to skip at runtime."""


@dataclass(frozen=True)
class Case:
    """One `assert_types(expression, ExpectedType)` line."""

    path: Path
    lines: frozenset[int]
    expression: str
    expected: str
    code: CodeType
    #: The expression on its own. A case expecting `Never` runs this rather than the
    #: whole assertion, because the call raising is the claim it makes.
    expression_code: CodeType | None = None
    #: Whether the expected type was written as a string. A checker reads it the same
    #: way; at runtime it is built in the file's namespace, which may not be possible.
    quoted: bool = False
    #: The `skip_runtime(...)` call of the `with` block holding this case, if any.
    skip_code: CodeType | None = None

    @property
    def id(self) -> str:
        """Return the test id, which reads as the claim the case makes."""
        return f'{self.expression} -> {self.expected}'


@dataclass(frozen=True)
class CaseFile:
    """One case file: the cases it declares, and the setup they share."""

    path: Path
    cases: tuple[Case, ...]
    setup_lines: frozenset[int]
    setup_code: CodeType | None = None
    #: The file as a checker reads it: every case guarded, so that a case whose
    #: expression never returns does not hide the cases after it. See `_guarded`.
    checked_source: str | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        """Return the file name, used to scope test ids."""
        return self.path.name

    def setup_namespace(self) -> dict[str, Any]:
        """Execute this file's setup and return the namespace it produced.

        A file that did not parse carries its reason in `error` and has no setup to
        run, so callers check that first; asking anyway is a mistake worth naming.
        """
        if self.setup_code is None:
            msg = f'{self.name} did not parse, so it has no setup to run: {self.error}'
            raise CaseError(msg)
        namespace: dict[str, Any] = {'__name__': self.path.stem, '__file__': str(self.path)}
        # Running the case file's own code is the point of the framework.
        exec(self.setup_code, namespace)  # noqa: S102
        return namespace

    def run(self, case: Case) -> None:
        """Execute one case, and only it, against a fresh copy of the setup.

        Rebuilding the setup for every case is what keeps cases independent: one
        cannot observe another's state, and reordering the file changes nothing.
        """
        namespace = self.setup_namespace()
        reason = skip_reason(namespace, case) or unbuildable_reason(namespace, case)
        if reason is not None:
            raise CaseSkipped(reason)
        if expects_never(namespace, case):
            run_never(namespace, case)
            return
        exec(case.code, namespace)  # noqa: S102


def expects_never(namespace: dict[str, Any], case: Case) -> bool:
    """Tell whether `case` claims its expression never returns.

    Read from the value the expected type names rather than from how it is spelled,
    so `Never`, `NoReturn` and either module's version of them all count.
    """
    if case.expression_code is None:
        return False
    try:
        expected = eval(case.expected, namespace)
    except Exception:  # noqa: BLE001 - an unbuildable type is simply not `Never`
        return False
    return expected is Never or expected is NoReturn


def run_never(namespace: dict[str, Any], case: Case) -> None:
    """Run the expression of a case that claims it never returns.

    The assertion is not run: the expression raising *is* the claim, and it raises
    while its own arguments are being evaluated, before `assert_types` is entered.
    An expression that returns has disproved the case, whatever it returned.
    """
    try:
        value = eval(case.expression_code, namespace)
    except Exception:  # noqa: BLE001 - not returning is what the case claims
        return
    msg = (
        f'{case.expression} was expected never to return, but it returned '
        f'{type(value).__name__} {value!r}'
    )
    raise AssertionError(msg)


def skip_reason(namespace: dict[str, Any], case: Case) -> str | None:
    """Return why `case` should not run, from the `skip_runtime` block holding it.

    The block's arguments are evaluated after the file's setup has run, so a skip
    can depend on it. Only the runtime half is skipped; a checker still checks the case.
    """
    if case.skip_code is None:
        return None
    skip = eval(case.skip_code, namespace)
    return skip.reason if skip.condition else None


def unbuildable_reason(namespace: dict[str, Any], case: Case) -> str | None:
    """Return why `case`'s quoted type cannot be built at runtime, if it cannot.

    A type that exists only for a checker -- a name imported under `TYPE_CHECKING`,
    a class that cannot be subscripted at runtime -- can still be written as a
    string. The checker holds the case to it; the runtime half has nothing to check
    against, so it is skipped rather than failed.
    """
    if not case.quoted:
        return None
    try:
        eval(case.expected, namespace)
    except Exception as error:  # noqa: BLE001
        return (
            f'the expected type {case.expected!r} cannot be built at runtime '
            f'({type(error).__name__}: {error}), so only a checker can check this case'
        )
    return None


def _guarded(source: str, cases: Sequence[Case]) -> str:
    """Return `source` with every case guarded, so a checker reads each on its own.

    A case whose expression never returns, a call the checker types as `Never`,
    ends the module's flow for the checker: mypy and pyright treat every statement
    after it as unreachable and check none of them, so a wrong case there would
    pass. A guard whose truth the checker cannot decide keeps each case reachable.
    It shares the module's scope and adds no line, so what the checker infers for
    the expression and the line it reports on are unchanged.
    """
    lines = source.splitlines(keepends=True)
    for case in cases:
        first = min(case.lines) - 1
        indent = len(lines[first]) - len(lines[first].lstrip())
        lines[first] = lines[first][:indent] + GUARD + lines[first][indent:]
    return ''.join(lines)


def _case_call(node: ast.AST) -> ast.Call | None:
    """Return the `assert_types` call a top-level statement makes, if it makes one."""
    if not isinstance(node, ast.Expr):
        return None
    call = node.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    return call if call.func.id == ASSERTION else None


def _skip_block(node: ast.AST, path: Path) -> ast.With | None:
    """Return a top-level `with skip_runtime(...):` block holding only cases, if `node` is one."""
    if not isinstance(node, ast.With) or len(node.items) != 1:
        return None
    item = node.items[0]
    call = item.context_expr
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == SKIP_BLOCK
    ):
        return None
    if item.optional_vars is not None or any(_case_call(child) is None for child in node.body):
        msg = (
            f'{path.name}:{node.lineno}: a `with {SKIP_BLOCK}(...):` block holds only '
            f'`{ASSERTION}` cases and binds no name.'
        )
        raise CaseError(msg)
    return node


def _case_nodes(tree: ast.Module, path: Path) -> list[tuple[ast.stmt, ast.With | None]]:
    """Return every case statement, with the skip block that holds it, in file order."""
    found: list[tuple[ast.stmt, ast.With | None]] = []
    for node in tree.body:
        block = _skip_block(node, path)
        if block is not None:
            found.extend((child, block) for child in block.body)
        elif _case_call(node) is not None:
            found.append((node, None))
    return found


def _reject_nested_assertions(tree: ast.Module, path: Path) -> None:
    """Reject `assert_types` calls that are not cases at module level or in a skip block.

    Such a call still type-checks, but it never becomes a case of its own, so it
    would be silently left out of the runtime half.
    """
    top_level = {id(_case_call(node)) for node, _ in _case_nodes(tree, path)}
    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == ASSERTION
        and id(node) not in top_level
    ]
    if nested:
        lines = ', '.join(str(node.lineno) for node in nested)
        msg = (
            f'{path.name}: `{ASSERTION}` must be a statement at module level so that it '
            f'becomes a case of its own. Found one nested at line(s) {lines}.'
        )
        raise CaseError(msg)


def collect_case_file(path: Path) -> CaseFile:
    """Parse one case file into its cases and the setup they share.

    A file this cannot make sense of yields a `CaseFile` carrying the reason
    rather than raising, so a malformed file fails its own test instead of
    aborting collection for the session.
    """
    try:
        return _parse_case_file(path)
    except (CaseError, SyntaxError, OSError) as error:
        return CaseFile(path=path, cases=(), setup_lines=frozenset(), error=str(error))


def _parse_case_file(path: Path) -> CaseFile:
    """Parse one case file, raising `CaseError` if it is not shaped as expected."""
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    _reject_nested_assertions(tree, path)

    setup = [
        node for node in tree.body if _case_call(node) is None and _skip_block(node, path) is None
    ]
    setup_module = ast.Module(body=setup, type_ignores=[])
    setup_code = compile(ast.fix_missing_locations(setup_module), str(path), 'exec')

    cases = []
    for node, block in _case_nodes(tree, path):
        call = _case_call(node)
        assert call is not None
        if len(call.args) != 2:
            msg = f'{path.name}:{node.lineno}: `{ASSERTION}` takes an expression and a type.'
            raise CaseError(msg)
        expected, quoted = _expected_type(call.args[1], path, node.lineno)
        module = ast.Module(body=[node], type_ignores=[])
        cases.append(
            Case(
                path=path,
                lines=frozenset(range(node.lineno, (node.end_lineno or node.lineno) + 1)),
                expression=ast.unparse(call.args[0]),
                expected=expected,
                code=compile(ast.fix_missing_locations(module), str(path), 'exec'),
                expression_code=compile(
                    ast.fix_missing_locations(ast.Expression(body=call.args[0])),
                    str(path),
                    'eval',
                ),
                quoted=quoted,
                skip_code=None
                if block is None
                else compile(
                    ast.fix_missing_locations(ast.Expression(body=block.items[0].context_expr)),
                    str(path),
                    'eval',
                ),
            )
        )

    case_lines = frozenset().union(*(case.lines for case in cases)) if cases else frozenset()
    return CaseFile(
        path=path,
        cases=tuple(cases),
        setup_lines=frozenset(range(1, len(source.splitlines()) + 1)) - case_lines,
        setup_code=setup_code,
        checked_source=_guarded(source, cases),
    )


def _expected_type(node: ast.expr, path: Path, lineno: int) -> tuple[str, bool]:
    """Return the expected type as written, unquoted if it was a string, and whether it was.

    Both spellings normalise to the same text, so a case reads the same in a test id
    whichever way it was written.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return ast.unparse(node), False
    try:
        tree = ast.parse(node.value, mode='eval')
    except SyntaxError:
        msg = f'{path.name}:{lineno}: the quoted type {node.value!r} is not a type expression.'
        raise CaseError(msg) from None
    return ast.unparse(tree.body), True


def collect_cases(directory: Path) -> list[CaseFile]:
    """Parse every case file in `directory`, subdirectories included."""
    return [collect_case_file(path) for path in sorted(directory.rglob('*.py'))]
