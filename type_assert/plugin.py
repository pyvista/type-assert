"""Pytest integration: collect case files and run each case as its own test.

Registered as a pytest plugin by entry point, so a project only has to say where
its cases live::

    [tool.type-assert]
    cases = 'tests/typing/cases'
    checkers = ['mypy', 'pyright']

The same settings are read from pytest's own configuration as ``type_assert_*`` ini
options when the table leaves them out; ``-o`` overrides both.

Each case file then collects as a test file of its own: one `setup` test for the
lines that are not cases, one runtime test per case, and one static test per case
per configured checker.
"""

from __future__ import annotations

from pathlib import Path
import tomllib
import traceback
from typing import TYPE_CHECKING

import pytest

from ._cases import CaseSkipped
from ._cases import collect_case_file
from ._cases import collect_cases
from ._checkers import CHECKERS
from ._checkers import get_checker
from ._share import run_once

if TYPE_CHECKING:
    from ._cases import Case
    from ._cases import CaseFile
    from ._checkers import Diagnostic

CASES_INI = 'type_assert_cases'
CHECKERS_INI = 'type_assert_checkers'
DEFAULT_CHECKERS = ('mypy',)
TABLE = 'type-assert'
_TABLE_STASH = pytest.StashKey['dict[str, str | list[str]]']()

_DIAGNOSTICS = '_type_assert_diagnostics'

#: What pytest's own outcome helpers raise, reached through the public functions
#: that raise them rather than through the module they are defined in.
_OUTCOMES = (pytest.fail.Exception, pytest.skip.Exception, pytest.xfail.Exception)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the settings a project needs."""
    parser.addini(
        CASES_INI,
        'Directory of type_assert case files, subdirectories included, relative to the rootdir.',
        default='',
    )
    parser.addini(
        CHECKERS_INI,
        'Type checkers to check the cases with, whitespace separated. Any of: '
        f'{", ".join(sorted(CHECKERS))}. Each one gets a static test of its own per '
        "case, so a case can be held to more than one checker's inference. Defaults "
        f'to {" ".join(DEFAULT_CHECKERS)}.',
        type='args',
        default=list(DEFAULT_CHECKERS),
    )
    # One per registered checker, since their flags have nothing in common. Each
    # checker already reads the project's own configuration -- it runs from the
    # rootdir, which is where every checker looks -- so this is for what that
    # configuration cannot say: a different config file, a Python version, a
    # strictness flag that should apply to the cases and nothing else.
    for name in sorted(CHECKERS):
        parser.addini(
            checker_args_ini(name),
            f'Extra command line arguments for {name}, whitespace separated.',
            type='args',
            default=[],
        )


def pytest_configure(config: pytest.Config) -> None:
    """Read the `[tool.type-assert]` table up front, so a bad one stops the run."""
    _table(config)


def checker_args_ini(checker_name: str) -> str:
    """Return the ini option holding extra arguments for `checker_name`."""
    return f'type_assert_{checker_name}_args'


def table_args_key(checker_name: str) -> str:
    """Return the `[tool.type-assert]` key holding extra arguments for `checker_name`."""
    return f'{checker_name}-args'


def _table(config: pytest.Config) -> dict[str, str | list[str]]:
    """Return the validated `[tool.type-assert]` table of the rootdir's pyproject.toml."""
    if _TABLE_STASH in config.stash:
        return config.stash[_TABLE_STASH]
    path = Path(config.rootpath) / 'pyproject.toml'
    raw: dict[str, object] = {}
    if path.is_file():
        with path.open('rb') as file:
            raw = tomllib.load(file).get('tool', {}).get(TABLE, {})
    list_keys = {'checkers', *(table_args_key(name) for name in CHECKERS)}
    table: dict[str, str | list[str]] = {}
    for key, value in raw.items():
        if key == 'cases' and isinstance(value, str):
            table[key] = value
        elif key in list_keys and isinstance(value, list):
            items: list[object] = value
            if not all(isinstance(item, str) for item in items):
                msg = f'{path}: [tool.{TABLE}] {key} must be a list of strings, got {value!r}.'
                raise pytest.UsageError(msg)
            table[key] = [str(item) for item in items]
        elif key == 'cases' or key in list_keys:
            kind = 'a string' if key == 'cases' else 'a list of strings'
            msg = f'{path}: [tool.{TABLE}] {key} must be {kind}, got {value!r}.'
            raise pytest.UsageError(msg)
        else:
            msg = f'{path}: unknown [tool.{TABLE}] key {key!r}.'
            raise pytest.UsageError(msg)
    config.stash[_TABLE_STASH] = table
    return table


def _overridden(config: pytest.Config, name: str) -> bool:
    """Tell whether an ini option is given on the command line with `-o`."""
    overrides = config.getoption('override_ini', None) or []
    return any(item.split('=', 1)[0] == name for item in overrides)


def _list_setting(config: pytest.Config, ini_name: str, key: str) -> list[str]:
    """Return a list setting from `-o`, else the table, else pytest's configuration."""
    value = _table(config).get(key)
    if isinstance(value, list) and not _overridden(config, ini_name):
        return value
    return [str(item) for item in config.getini(ini_name)]


def configured_checkers(config: pytest.Config) -> list[str]:
    """Return the names of the checkers the cases are checked with."""
    names = [name for name in _list_setting(config, CHECKERS_INI, 'checkers') if name.strip()]
    return names or list(DEFAULT_CHECKERS)


def checker_args(config: pytest.Config, checker_name: str) -> list[str]:
    """Return the extra command line arguments for `checker_name`."""
    return _list_setting(config, checker_args_ini(checker_name), table_args_key(checker_name))


def cases_dir(config: pytest.Config) -> Path | None:
    """Return the configured cases directory, or `None` when unset."""
    value = _table(config).get('cases')
    if not isinstance(value, str) or _overridden(config, CASES_INI):
        value = str(config.getini(CASES_INI))
    configured = value.strip()
    if not configured:
        return None
    return (Path(config.rootpath) / configured).resolve()


def _is_case_file(file_path: Path, config: pytest.Config) -> bool:
    """Tell whether `file_path` is a `.py` file under the configured cases directory."""
    directory = cases_dir(config)
    return directory is not None and file_path.suffix == '.py' and directory in file_path.parents


def pytest_collect_file(file_path: Path, parent: pytest.Collector):
    """Collect a `.py` file in the configured cases directory as a case file."""
    # A path named on the command line reaches `pytest_pycollect_makemodule` below,
    # which is where pytest's own Python collector would otherwise pick it up.
    if not _is_case_file(file_path, parent.config) or parent.session.isinitpath(file_path):
        return None
    return CaseFileCollector.from_parent(parent, path=file_path)


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path: Path, parent: pytest.Collector):
    """Collect a case file named on the command line as a case file, not a module.

    pytest collects any `.py` path it is given explicitly as a test module whatever
    its name, which would import the case file and run every assertion at import.
    Answering this hook first hands the file to the case file collector instead.
    """
    if not _is_case_file(module_path, parent.config):
        return None
    return CaseFileCollector.from_parent(parent, path=module_path)


def _diagnostics(config: pytest.Config, checker_name: str) -> dict[Path, list[Diagnostic]]:
    """Run one checker once per session and cache its result on the config."""
    cache = getattr(config, _DIAGNOSTICS, None)
    if cache is None:
        cache = {}
        setattr(config, _DIAGNOSTICS, cache)
    if checker_name not in cache:
        directory = cases_dir(config)
        assert directory is not None  # only reachable from a collected case file
        root = Path(config.rootpath)
        checker = get_checker(checker_name)

        def run() -> dict[Path, list[Diagnostic]]:
            """Type-check the cases with this checker, each case guarded."""
            return checker.run(
                '.'.join(directory.relative_to(root).parts),
                root=root,
                cache_dir=root / '.mypy_cache' / f'type_assert-{checker_name}',
                extra_args=checker_args(config, checker_name),
                sources={
                    case_file.path: case_file.checked_source
                    for case_file in collect_cases(directory)
                    if case_file.checked_source is not None
                },
            )

        cache[checker_name] = run_once(config, checker_name, run)
    return cache[checker_name]


def _report(case_file: CaseFile, checker_name: str, errors: list[Diagnostic]) -> str:
    """Return the checker's messages against the source lines they came from."""
    source = case_file.path.read_text(encoding='utf-8').splitlines()
    body = '\n'.join(
        f'{case_file.name}:{error.line}: {error.message}\n\t{source[error.line - 1].strip()}'
        for error in errors
    )
    return f'{checker_name} reported {len(errors)} error(s):\n{body}'


class CaseFileCollector(pytest.File):
    """Collects one case file as a test file."""

    def collect(self):
        """Yield the file's setup test, and per case a runtime test and a static one."""
        case_file = collect_case_file(Path(self.path))
        checkers = configured_checkers(self.config)
        yield SetupItem.from_parent(self, name='setup', case_file=case_file)
        for case in case_file.cases:
            yield RuntimeItem.from_parent(
                self, name=f'{case.id} [runtime]', case_file=case_file, case=case
            )
            for checker_name in checkers:
                yield StaticItem.from_parent(
                    self,
                    name=f'{case.id} [static: {checker_name}]',
                    case_file=case_file,
                    case=case,
                    checker_name=checker_name,
                )


class _Item(pytest.Item):
    """Shared plumbing for the tests a case file collects."""

    def __init__(self, *args, case_file: CaseFile, **kwargs) -> None:
        """Record the case file this test belongs to."""
        super().__init__(*args, **kwargs)
        self.case_file = case_file

    def reportinfo(self):
        """Locate this test in its case file."""
        return self.path, None, self.name

    def errors_on(self, checker_name: str, lines: frozenset[int]) -> list[Diagnostic]:
        """Return one checker's errors falling on `lines` of this case file."""
        reported = _diagnostics(self.config, checker_name).get(self.case_file.path.resolve(), [])
        return [diagnostic for diagnostic in reported if diagnostic.line in lines]

    def failure_header(self) -> str:
        """Return the first line of a failure report: what failed, and where."""
        return self.name

    def repr_failure(self, excinfo, style=None):
        """Report what the case file did, not how this plugin called into it.

        A case runs as compiled module code, so the frames between pytest and it
        belong to pytest, pluggy and this plugin. None of them tell the reader
        anything, and there are enough of them to bury the assertion that failed.
        """
        if isinstance(excinfo.value, _OUTCOMES):
            # `pytest.fail(..., pytrace=False)`: already a plain message.
            return super().repr_failure(excinfo, style)

        error = excinfo.value
        lines = [self.failure_header(), '']
        if isinstance(error, AssertionError) and str(error):
            lines.append(str(error))
        else:
            lines.append(f'Running this raised {type(error).__name__}: {error}')

        frames = [
            frame
            for frame in traceback.extract_tb(excinfo.tb)
            if frame.filename == str(self.case_file.path)
        ]
        if frames:
            lines += ['', 'in the case file:']
            lines += [
                f'  {self.case_file.name}:{frame.lineno}: {(frame.line or "").strip()}'
                for frame in frames
            ]
        return '\n'.join(lines)


class SetupItem(_Item):
    """Checks a case file's setup: that it is well formed, runs, and type-checks."""

    def runtest(self) -> None:
        """Assert the file parses, its setup executes, and nothing else is wrong."""
        # Report the file's own problem before asking for a type-check of it: a file
        # the checker cannot parse fails the whole run, which would mask the reason.
        if self.case_file.error is not None:
            pytest.fail(self.case_file.error, pytrace=False)

        self.case_file.setup_namespace()

        for checker_name in configured_checkers(self.config):
            errors = self.errors_on(checker_name, self.case_file.setup_lines)
            if errors:
                pytest.fail(_report(self.case_file, checker_name, errors), pytrace=False)


class _CaseItem(_Item):
    """A test about one case."""

    def __init__(self, *args, case: Case, **kwargs) -> None:
        """Record the case this test is about."""
        super().__init__(*args, **kwargs)
        self.case = case

    def failure_header(self) -> str:
        """Return the case this test is about, and the line it is written on."""
        return f'{self.case_file.name}:{min(self.case.lines)}: {self.case.id}'


class RuntimeItem(_CaseItem):
    """Checks the value a case builds against the type the case expects."""

    def runtest(self) -> None:
        """Run this case, and only it."""
        try:
            self.case_file.run(self.case)
        except CaseSkipped as skipped:
            pytest.skip(str(skipped))


class StaticItem(_CaseItem):
    """Checks the type one checker infers for a case against the type it expects."""

    def __init__(self, *args, checker_name: str, **kwargs) -> None:
        """Record which checker this test speaks for."""
        super().__init__(*args, **kwargs)
        self.checker_name = checker_name

    def runtest(self) -> None:
        """Assert the checker reports nothing on this case's lines."""
        errors = self.errors_on(self.checker_name, self.case.lines)
        if errors:
            pytest.fail(_report(self.case_file, self.checker_name, errors), pytrace=False)
