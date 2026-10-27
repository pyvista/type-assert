"""The checker registry, and each backend against real checker output."""

from __future__ import annotations

import json
import subprocess

import pytest

from type_assert import CHECKERS
from type_assert import CheckerError
from type_assert import get_checker
from type_assert._checkers import MypyChecker
from type_assert._checkers import PyreflyChecker
from type_assert._checkers import PyrightChecker
from type_assert._checkers._pyright import _extract_report

CLEAN = """\
from type_assert import assert_types

assert_types(len([1]), int)
"""

WRONG = """\
from type_assert import assert_types

assert_types(len([1]), str)
"""

SUPERTYPE = """\
from type_assert import assert_types

assert_types(len([1]), object)
"""

BAD_SETUP = """\
from type_assert import assert_types

BAD: int = 'not an int'
assert_types(len([1]), int)
"""

AFTER_NEVER = """\
from typing import Never
from typing import NoReturn

from type_assert import assert_types


def boom() -> NoReturn:
    raise RuntimeError


assert_types(boom(), Never)
assert_types(len([1]), str)
"""


@pytest.fixture
def package(checker_root):
    """Return a helper that writes a one-module package and checks it."""

    def _package(source, checker):
        directory = checker_root / 'cases'
        directory.mkdir(exist_ok=True)
        (directory / 'sample.py').write_text(source, encoding='utf-8')
        found = checker.run('cases', root=checker_root, cache_dir=checker_root / '.cache')
        return {path.name: errors for path, errors in found.items()}

    return _package


def test_registry_lists_both_checkers():
    assert sorted(CHECKERS) == ['mypy', 'pyrefly', 'pyright']


@pytest.mark.parametrize('name', ['mypy', 'pyrefly', 'pyright'])
def test_get_checker_returns_the_named_checker(name):
    assert get_checker(name).name == name


def test_an_unknown_checker_names_the_ones_that_exist():
    with pytest.raises(CheckerError, match='Unknown type checker'):
        get_checker('nope')
    with pytest.raises(CheckerError, match='mypy, pyrefly, pyright'):
        get_checker('nope')


def test_ty_is_not_supported_yet():
    # Deliberate: it is pre-1.0 and its output format is still moving.
    assert 'ty' not in CHECKERS


@pytest.mark.parametrize(
    'checker',
    [MypyChecker(), PyreflyChecker(), PyrightChecker()],
    ids=['mypy', 'pyrefly', 'pyright'],
)
class TestBackend:
    """Every backend behaves the same way from the outside."""

    def test_clean_code_reports_nothing(self, package, checker):
        assert package(CLEAN, checker) == {}

    def test_a_wrong_type_is_reported_on_the_case_line(self, package, checker):
        found = package(WRONG, checker)
        (errors,) = found.values()
        assert len(errors) == 1
        assert errors[0].line == 3

    def test_a_supertype_is_reported_too(self, package, checker):
        # `assert_type` is exact; this is what separates it from assignability.
        found = package(SUPERTYPE, checker)
        assert len(found) == 1

    def test_an_error_outside_a_case_is_reported_on_its_own_line(self, package, checker):
        found = package(BAD_SETUP, checker)
        (errors,) = found.values()
        assert [error.line for error in errors] == [3]

    def test_the_reported_path_is_absolute_and_resolved(self, checker_root, checker):
        directory = checker_root / 'cases'
        directory.mkdir()
        (directory / 'sample.py').write_text(WRONG, encoding='utf-8')
        (path,) = checker.run('cases', root=checker_root, cache_dir=checker_root / '.cache').keys()
        assert path.is_absolute()
        assert path == path.resolve()
        assert path.name == 'sample.py'

    def test_the_message_is_a_single_line(self, package, checker):
        found = package(WRONG, checker)
        (errors,) = found.values()
        assert '\n' not in errors[0].message

    def test_a_missing_package_is_an_error_not_a_silent_pass(self, checker_root, checker):
        with pytest.raises(CheckerError):
            checker.run('absent', root=checker_root, cache_dir=checker_root / '.cache')


def test_mypy_accepts_no_cache_directory(checker_root):
    directory = checker_root / 'cases'
    directory.mkdir()
    (directory / 'sample.py').write_text(CLEAN, encoding='utf-8')
    assert MypyChecker().run('cases', root=checker_root, cache_dir=None) == {}


def test_pyright_ignores_the_cache_directory(checker_root):
    directory = checker_root / 'cases'
    directory.mkdir()
    (directory / 'sample.py').write_text(CLEAN, encoding='utf-8')
    assert PyrightChecker().run('cases', root=checker_root, cache_dir=None) == {}


def test_both_checkers_agree_on_a_plain_case(checker_root):
    directory = checker_root / 'cases'
    directory.mkdir()
    (directory / 'sample.py').write_text(WRONG, encoding='utf-8')
    lines = set()
    for checker in (MypyChecker(), PyrightChecker()):
        (errors,) = checker.run('cases', root=checker_root, cache_dir=None).values()
        lines.update(error.line for error in errors)
    assert lines == {3}


class TestPyrightOutputParsing:
    """pyright's report is not always the whole of its output."""

    def test_plain_json_is_read(self):
        assert _extract_report('{"generalDiagnostics": []}') == {'generalDiagnostics': []}

    def test_a_preamble_before_the_report_is_skipped(self):
        # The `pyright` distribution downloads node on first use and says so.
        stdout = (
            ' * Install prebuilt node (26.8.1) ..... done.\n{\'x86\': False}\n{\n  "a": 1\n}\n'
        )
        assert _extract_report(stdout) == {'a': 1}

    def test_no_report_at_all_returns_none(self):
        assert _extract_report('command not found\n') is None

    def test_empty_output_returns_none(self):
        assert _extract_report('') is None

    def test_a_truncated_report_returns_none(self):
        assert _extract_report('{"generalDiagnostics": [') is None


class TestOutputHandling:
    """Parsing decisions, driven by crafted output rather than a real run."""

    @staticmethod
    def _fake_run(stdout='', stderr='', returncode=0):
        """Return a `subprocess.run` replacement yielding fixed output."""

        def _run(*args, **kwargs):
            del args, kwargs
            return subprocess.CompletedProcess([], returncode, stdout, stderr)

        return _run

    def test_mypy_ignores_notes_and_keeps_errors(self, monkeypatch, tmp_path):
        stdout = (
            'cases/s.py:1: error: real problem  [code]\n'
            'cases/s.py:1: note: See https://mypy.readthedocs.io/\n'
            'cases/s.py:2: warning: not an error either\n'
        )
        monkeypatch.setattr(subprocess, 'run', self._fake_run(stdout, returncode=1))
        (errors,) = MypyChecker().run('cases', root=tmp_path, cache_dir=None).values()
        assert [error.message for error in errors] == ['real problem  [code]']

    def test_mypy_ignores_lines_that_are_not_diagnostics(self, monkeypatch, tmp_path):
        stdout = 'something unstructured\ncases/s.py:3: error: kept  [x]\n'
        monkeypatch.setattr(subprocess, 'run', self._fake_run(stdout, returncode=1))
        (errors,) = MypyChecker().run('cases', root=tmp_path, cache_dir=None).values()
        assert [error.line for error in errors] == [3]

    def test_mypy_says_how_to_install_itself_when_missing(self, monkeypatch, tmp_path):
        stderr = 'No module named mypy\n'
        monkeypatch.setattr(subprocess, 'run', self._fake_run(stderr=stderr, returncode=1))
        with pytest.raises(CheckerError, match=r'pip install type-assert\[mypy\]'):
            MypyChecker().run('cases', root=tmp_path, cache_dir=None)

    def test_mypy_exit_code_one_with_no_stderr_is_diagnostics_not_failure(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(subprocess, 'run', self._fake_run(returncode=1))
        assert MypyChecker().run('cases', root=tmp_path, cache_dir=None) == {}

    def test_mypy_command_line_stays_short_with_many_sources(self, monkeypatch, tmp_path):
        commands = []

        def _run(command, **kwargs):
            del kwargs
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, '', '')

        monkeypatch.setattr(subprocess, 'run', _run)
        sources = {tmp_path / f'case_{index}.py': '' for index in range(2000)}
        MypyChecker().run('cases', root=tmp_path, cache_dir=None, sources=sources)
        (command,) = commands
        assert sum(len(part) for part in command) < 1000

    def test_mypy_exit_code_two_is_a_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, 'run', self._fake_run(returncode=2))
        with pytest.raises(CheckerError, match='mypy failed to run'):
            MypyChecker().run('cases', root=tmp_path, cache_dir=None)

    def test_pyright_ignores_warnings_and_keeps_errors(self, monkeypatch, tmp_path):
        report = {
            'generalDiagnostics': [
                {
                    'file': str(tmp_path / 'cases' / 's.py'),
                    'severity': 'warning',
                    'message': 'ignored',
                    'range': {'start': {'line': 0}},
                },
                {
                    'file': str(tmp_path / 'cases' / 's.py'),
                    'severity': 'error',
                    'message': 'kept\nsecond line',
                    'range': {'start': {'line': 4}},
                },
            ]
        }
        monkeypatch.setattr(subprocess, 'run', self._fake_run(json.dumps(report), returncode=1))
        (errors,) = PyrightChecker().run('cases', root=tmp_path, cache_dir=None).values()
        assert len(errors) == 1
        # pyright counts lines from zero, and its messages can wrap.
        assert errors[0].line == 5
        assert errors[0].message == 'kept second line'

    def test_pyright_says_how_to_install_itself_when_missing(self, monkeypatch, tmp_path):
        stderr = 'No module named pyright\n'
        monkeypatch.setattr(subprocess, 'run', self._fake_run(stderr=stderr, returncode=1))
        with pytest.raises(CheckerError, match=r'pip install type-assert\[pyright\]'):
            PyrightChecker().run('cases', root=tmp_path, cache_dir=None)

    def test_pyright_with_no_diagnostics_key_reports_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, 'run', self._fake_run('{}', returncode=0))
        assert PyrightChecker().run('cases', root=tmp_path, cache_dir=None) == {}


def test_the_base_checker_leaves_running_to_its_subclasses(tmp_path):
    from type_assert import Checker

    with pytest.raises(NotImplementedError):
        Checker().run('cases', root=tmp_path, cache_dir=None)


class TestPyreflyGuards:
    """pyrefly passes silently in two situations where it checked nothing."""

    def test_no_configuration_is_refused_rather_than_passed(self, tmp_path):
        # Given no config, pyrefly ignores the path it is handed and reports success.
        directory = tmp_path / 'cases'
        directory.mkdir()
        (directory / 'sample.py').write_text(WRONG, encoding='utf-8')
        with pytest.raises(CheckerError, match='checked nothing'):
            PyreflyChecker().run('cases', root=tmp_path, cache_dir=None)

    def test_a_missing_target_is_refused(self, tmp_path):
        from tests.conftest import write_pyrefly_config

        write_pyrefly_config(tmp_path)
        with pytest.raises(CheckerError, match='does not exist'):
            PyreflyChecker().run('absent', root=tmp_path, cache_dir=None)

    def test_the_refusal_says_how_to_configure_it(self, tmp_path):
        directory = tmp_path / 'cases'
        directory.mkdir()
        (directory / 'sample.py').write_text(CLEAN, encoding='utf-8')
        with pytest.raises(CheckerError, match=r'project-includes'):
            PyreflyChecker().run('cases', root=tmp_path, cache_dir=None)


class TestSources:
    """A checker reads the text it is given in place of the file, and reports on the file."""

    @pytest.mark.parametrize('checker', [MypyChecker(), PyrightChecker()])
    def test_the_given_text_is_checked_not_the_file(self, checker_root, checker):
        directory = checker_root / 'cases'
        directory.mkdir()
        path = directory / 'sample.py'
        path.write_text(WRONG, encoding='utf-8')
        found = checker.run(
            'cases', root=checker_root, cache_dir=checker_root / '.cache', sources={path: CLEAN}
        )
        assert found == {}

    @pytest.mark.parametrize('checker', [MypyChecker(), PyrightChecker()])
    def test_an_error_is_reported_on_the_file_not_the_copy(self, checker_root, checker):
        directory = checker_root / 'cases'
        directory.mkdir()
        path = directory / 'sample.py'
        path.write_text(CLEAN, encoding='utf-8')
        found = checker.run(
            'cases', root=checker_root, cache_dir=checker_root / '.cache', sources={path: WRONG}
        )
        assert list(found) == [path.resolve()]
        assert [error.line for error in found[path.resolve()]] == [3]

    @pytest.mark.parametrize('checker', [MypyChecker(), PyrightChecker()])
    def test_a_case_after_a_never_is_not_checked_from_the_file_alone(self, package, checker):
        # What the guard exists for: without it, the checker reads no further.
        assert package(AFTER_NEVER, checker) == {}

    def test_pyrefly_reads_past_a_never_on_its_own(self, package):
        # So it is handed nothing, and the file on disk is what it checks.
        found = package(AFTER_NEVER, PyreflyChecker())
        assert [error.line for error in found['sample.py']] == [12]

    def test_pyright_resolves_an_import_relative_to_the_package_in_the_copy(self, checker_root):
        directory = checker_root / 'cases'
        directory.mkdir()
        (directory / 'support.py').write_text(
            'def one() -> int:\n    return 1\n', encoding='utf-8'
        )
        path = directory / 'sample.py'
        source = (
            'from type_assert import assert_types\n\n'
            'from .support import one\n\n'
            'assert_types(one(), str)\n'
        )
        path.write_text(source, encoding='utf-8')
        found = PyrightChecker().run(
            'cases', root=checker_root, cache_dir=None, sources={path: source}
        )
        assert [error.line for error in found[path.resolve()]] == [5]
