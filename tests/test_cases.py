"""How a case file is parsed, run, and reported on when it is malformed."""

from __future__ import annotations

from pathlib import Path

import pytest

from type_assert import CaseError
from type_assert import CaseSkipped
from type_assert import collect_case_file
from type_assert import collect_cases
from type_assert._cases import GUARD

IMPORT = 'from type_assert import assert_types\n'
SKIP_IMPORT = IMPORT + 'from type_assert import skip_runtime\n'


@pytest.fixture
def write(tmp_path):
    """Return a helper that writes a case file and parses it."""

    def _write(body, name='cases.py', *, prelude=IMPORT):
        path = tmp_path / name
        path.write_text(prelude + body, encoding='utf-8')
        return collect_case_file(path)

    return _write


def test_each_assertion_becomes_a_case(write):
    case_file = write('assert_types(len([1]), int)\nassert_types(str(1), str)\n')
    assert [case.id for case in case_file.cases] == ['len([1]) -> int', 'str(1) -> str']
    assert case_file.error is None


def test_a_case_records_its_expression_and_expected_type(write):
    case_file = write('assert_types(sorted({1}), list[int])\n')
    (case,) = case_file.cases
    assert case.expression == 'sorted({1})'
    assert case.expected == 'list[int]'


def test_formatting_is_normalised_in_the_id(write):
    case_file = write('assert_types( len( [ 1 ] ) ,   list [ int ] )\n')
    assert case_file.cases[0].id == 'len([1]) -> list[int]'


def test_a_case_knows_which_lines_it_occupies(write):
    case_file = write('X = 1\nassert_types(X, int)\n')
    assert case_file.cases[0].lines == frozenset({3})


def test_a_case_spanning_several_lines_claims_all_of_them(write):
    case_file = write('assert_types(\n    len([1]),\n    int,\n)\n')
    assert case_file.cases[0].lines == frozenset({2, 3, 4, 5})


def test_setup_lines_are_everything_that_is_not_a_case(write):
    case_file = write('X = 1\nassert_types(X, int)\nY = 2\n')
    assert case_file.setup_lines == frozenset({1, 2, 4})


def test_a_file_with_no_cases_is_all_setup(write):
    case_file = write('X = 1\n')
    assert case_file.cases == ()
    assert case_file.setup_lines == frozenset({1, 2})


def test_an_empty_file_parses(write):
    case_file = write('', prelude='')
    assert case_file.cases == ()
    assert case_file.error is None


def test_other_calls_are_setup_not_cases(write):
    case_file = write('print\nlen([1])\nassert_types(1, int)\n')
    assert len(case_file.cases) == 1


def test_the_file_name_is_available_for_test_ids(write):
    case_file = write('assert_types(1, int)\n', name='wrap.py')
    assert case_file.name == 'wrap.py'


def test_collect_cases_reads_a_directory_in_order(tmp_path):
    for name in ('b.py', 'a.py'):
        (tmp_path / name).write_text(IMPORT + 'assert_types(1, int)\n', encoding='utf-8')
    assert [case_file.name for case_file in collect_cases(tmp_path)] == ['a.py', 'b.py']


def test_collect_cases_on_an_empty_directory(tmp_path):
    assert collect_cases(tmp_path) == []


def test_collect_cases_descends_into_subdirectories(tmp_path):
    (tmp_path / 'core').mkdir()
    for name in ('z.py', 'core/a.py'):
        (tmp_path / name).write_text(IMPORT + 'assert_types(1, int)\n', encoding='utf-8')
    collected = [case_file.path.relative_to(tmp_path) for case_file in collect_cases(tmp_path)]
    assert collected == [Path('core/a.py'), Path('z.py')]


class TestRunning:
    """Executing a case against its file's setup."""

    def test_a_passing_case_runs(self, write):
        case_file = write('assert_types(len([1]), int)\n')
        case_file.run(case_file.cases[0])

    def test_a_failing_case_raises(self, write):
        case_file = write('assert_types(len([1]), str)\n')
        with pytest.raises(AssertionError, match='not assignable'):
            case_file.run(case_file.cases[0])

    def test_setup_is_available_to_the_case(self, write):
        case_file = write('def make():\n    return [1]\n\nassert_types(make(), list[int])\n')
        case_file.run(case_file.cases[0])

    def test_a_case_cannot_see_another_cases_state(self, write):
        body = 'V = []\nassert_types(V.append(1), None)\nassert_types(len(V), int)\n'
        case_file = write(body)
        case_file.run(case_file.cases[0])
        namespace = case_file.setup_namespace()
        case_file.run(case_file.cases[1])
        # The append in the first case must not be visible anywhere else.
        assert namespace['V'] == []

    def test_running_a_case_twice_starts_from_the_same_state(self, write):
        body = 'V = []\nassert_types(V.append(1), None)\n'
        case_file = write(body)
        case_file.run(case_file.cases[0])
        case_file.run(case_file.cases[0])

    def test_setup_namespace_carries_the_file_identity(self, write):
        case_file = write('assert_types(1, int)\n', name='named.py')
        namespace = case_file.setup_namespace()
        assert namespace['__name__'] == 'named'
        assert namespace['__file__'].endswith('named.py')

    def test_an_error_in_setup_propagates(self, write):
        case_file = write('raise RuntimeError("boom")\nassert_types(1, int)\n')
        with pytest.raises(RuntimeError, match='boom'):
            case_file.run(case_file.cases[0])


class TestMalformed:
    """A file the parser cannot make sense of reports rather than raises."""

    def test_a_nested_assertion_is_rejected(self, write):
        body = 'def helper():\n    assert_types(1, int)\n\nassert_types(2, int)\n'
        case_file = write(body)
        assert 'must be a statement at module level' in case_file.error
        assert 'line(s) 3' in case_file.error

    def test_an_assertion_inside_a_conditional_is_rejected(self, write):
        case_file = write('if True:\n    assert_types(1, int)\n')
        assert 'must be a statement at module level' in case_file.error

    def test_the_wrong_number_of_arguments_is_rejected(self, write):
        case_file = write('assert_types(1)\n')
        assert 'takes an expression and a type' in case_file.error

    def test_too_many_arguments_are_rejected(self, write):
        case_file = write('assert_types(1, int, str)\n')
        assert 'takes an expression and a type' in case_file.error

    def test_a_syntax_error_is_reported(self, write):
        case_file = write('def broken(\n')
        assert 'never closed' in case_file.error
        assert case_file.cases == ()

    def test_a_missing_file_is_reported(self, tmp_path):
        case_file = collect_case_file(tmp_path / 'absent.py')
        assert case_file.error is not None
        assert case_file.cases == ()

    def test_a_file_that_did_not_parse_has_no_setup_to_run(self, write):
        case_file = write('def broken(\n')
        with pytest.raises(CaseError, match='did not parse'):
            case_file.setup_namespace()


class TestQuotedTypes:
    """A quoted expected type reads like an unquoted one, and may exist only for a checker."""

    NEVER = (
        'from typing import TYPE_CHECKING\n\n'
        'if TYPE_CHECKING:\n'
        '    from typing import Never\n\n\n'
        'def empty() -> list[Never]:\n'
        '    return []\n\n\n'
        "assert_types(empty(), 'list[Never]')\n"
    )

    def test_the_id_drops_the_quotes(self, write):
        case_file = write("assert_types(len([1]), 'int')\n")
        assert case_file.cases[0].id == 'len([1]) -> int'
        assert case_file.cases[0].quoted

    def test_an_unquoted_type_is_not_marked_quoted(self, write):
        case_file = write('assert_types(len([1]), int)\n')
        assert not case_file.cases[0].quoted

    def test_the_spelling_inside_the_quotes_is_normalised(self, write):
        case_file = write("assert_types(len([1]), 'list[ int ]')\n")
        assert case_file.cases[0].expected == 'list[int]'

    def test_a_type_that_exists_at_runtime_is_checked(self, write):
        case_file = write("assert_types(len([1]), 'str')\n")
        with pytest.raises(AssertionError, match='not assignable'):
            case_file.run(case_file.cases[0])

    def test_a_type_that_cannot_be_built_skips_the_runtime_half(self, write):
        case_file = write(self.NEVER)
        with pytest.raises(CaseSkipped, match=r"'list\[Never\]' cannot be built at runtime"):
            case_file.run(case_file.cases[0])

    def test_the_skip_names_the_error(self, write):
        case_file = write(self.NEVER)
        with pytest.raises(CaseSkipped, match='NameError'):
            case_file.run(case_file.cases[0])

    def test_a_quoted_type_that_is_not_an_expression_is_malformed(self, write):
        case_file = write("assert_types(len([1]), 'not a type')\n")
        assert case_file.error is not None
        assert 'not a type expression' in case_file.error


class TestSkipBlock:
    """A `with skip_runtime(...):` block skips the runtime half of the cases inside it."""

    def test_a_case_in_the_block_is_skipped(self, write):
        body = "with skip_runtime(reason='a reason'):\n    assert_types(len([1]), int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        with pytest.raises(CaseSkipped, match='a reason'):
            case_file.run(case_file.cases[0])

    def test_every_case_in_the_block_is_skipped(self, write):
        body = (
            "with skip_runtime(reason='a reason'):\n"
            '    assert_types(len([1]), int)\n'
            '    assert_types(str(1), str)\n'
        )
        case_file = write(body, prelude=SKIP_IMPORT)
        assert len(case_file.cases) == 2
        for case in case_file.cases:
            with pytest.raises(CaseSkipped):
                case_file.run(case)

    def test_a_case_outside_the_block_still_runs(self, write):
        body = (
            "with skip_runtime(reason='a reason'):\n"
            '    assert_types(len([1]), int)\n'
            'assert_types(str(1), str)\n'
        )
        case_file = write(body, prelude=SKIP_IMPORT)
        case_file.run(case_file.cases[1])

    def test_a_false_condition_runs_the_case(self, write):
        body = "with skip_runtime(False, reason='a reason'):\n    assert_types(len([1]), int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        case_file.run(case_file.cases[0])

    def test_the_condition_sees_the_setup(self, write):
        body = (
            'FLAG = True\n'
            "with skip_runtime(FLAG, reason='a reason'):\n"
            '    assert_types(len([1]), int)\n'
        )
        case_file = write(body, prelude=SKIP_IMPORT)
        with pytest.raises(CaseSkipped, match='a reason'):
            case_file.run(case_file.cases[0])

    def test_a_block_holding_anything_else_is_malformed(self, write):
        body = "with skip_runtime(reason='a reason'):\n    x = 1\n    assert_types(x, int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        assert case_file.error is not None
        assert 'holds only' in case_file.error

    def test_a_block_binding_a_name_is_malformed(self, write):
        body = "with skip_runtime(reason='a reason') as s:\n    assert_types(len([1]), int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        assert case_file.error is not None

    def test_a_case_in_another_block_is_still_rejected(self, write):
        body = 'if True:\n    assert_types(len([1]), int)\n'
        case_file = write(body, prelude=SKIP_IMPORT)
        assert case_file.error is not None
        assert 'module level' in case_file.error

    def test_the_guard_goes_after_the_indentation(self, write):
        body = "with skip_runtime(reason='a reason'):\n    assert_types(len([1]), int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        assert f'    {GUARD}assert_types(len([1]), int)' in case_file.checked_source

    def test_running_the_file_runs_the_block(self, write):
        body = "with skip_runtime(reason='a reason'):\n    assert_types(len([1]), int)\n"
        case_file = write(body, prelude=SKIP_IMPORT)
        exec(compile(case_file.path.read_text(), str(case_file.path), 'exec'), {})  # noqa: S102


class TestCheckedSource:
    """The text a checker reads: every case guarded, nothing else touched."""

    def test_every_case_is_guarded(self, write):
        case_file = write('assert_types(len([1]), int)\nassert_types(str(1), str)\n')
        assert case_file.checked_source == (
            f'{IMPORT}{GUARD}assert_types(len([1]), int)\n{GUARD}assert_types(str(1), str)\n'
        )

    def test_setup_lines_are_untouched(self, write):
        body = 'x = [1]\n\n\ndef f() -> int:\n    return 1\n\n\nassert_types(f(), int)\n'
        case_file = write(body)
        assert case_file.checked_source == IMPORT + body.replace(
            'assert_types', f'{GUARD}assert_types'
        )

    def test_the_line_count_is_preserved(self, write):
        body = 'assert_types(\n    len([1]),\n    int,\n)\nassert_types(str(1), str)\n'
        case_file = write(body)
        assert case_file.checked_source is not None
        assert case_file.checked_source.count('\n') == (IMPORT + body).count('\n')

    def test_a_case_spanning_several_lines_is_guarded_on_its_first(self, write):
        case_file = write('assert_types(\n    len([1]),\n    int,\n)\n')
        assert (
            case_file.checked_source
            == f'{IMPORT}{GUARD}assert_types(\n    len([1]),\n    int,\n)\n'
        )

    def test_the_guard_shares_the_module_scope(self, write):
        # A guard that opened a new scope would lose narrowing done in the setup.
        assert GUARD.startswith('if ')

    def test_a_file_that_did_not_parse_has_none(self, write):
        assert write('assert_types(len([1]) int)\n').checked_source is None


class TestNeverCases:
    """A case expecting `Never` runs its expression and asserts it does not return."""

    RAISES = (
        'from typing import Never\n'
        'from typing import NoReturn\n\n'
        'def boom() -> NoReturn:\n'
        "    raise RuntimeError('nope')\n\n"
        'def fine() -> int:\n'
        '    return 1\n\n'
    )

    def case(self, write, body, name='cases.py'):
        """Parse a file whose setup defines a raising and a returning function."""
        return write(body, name=name, prelude=SKIP_IMPORT + self.RAISES)

    def test_an_expression_that_raises_passes(self, write):
        case_file = self.case(write, 'assert_types(boom(), Never)\n')
        (case,) = case_file.cases
        case_file.run(case)

    def test_an_expression_that_returns_fails(self, write):
        case_file = self.case(write, 'assert_types(fine(), Never)\n')
        (case,) = case_file.cases
        with pytest.raises(AssertionError, match='never to return'):
            case_file.run(case)

    def test_the_failure_names_what_it_returned(self, write):
        case_file = self.case(write, 'assert_types(fine(), Never)\n')
        (case,) = case_file.cases
        with pytest.raises(AssertionError, match='int 1'):
            case_file.run(case)

    def test_no_return_is_read_the_same_way(self, write):
        case_file = self.case(write, 'assert_types(boom(), NoReturn)\n')
        (case,) = case_file.cases
        case_file.run(case)

    def test_a_skip_still_applies(self, write):
        body = "with skip_runtime(reason='not here'):\n    assert_types(boom(), Never)\n"
        case_file = self.case(write, body)
        (case,) = case_file.cases
        with pytest.raises(CaseSkipped):
            case_file.run(case)

    def test_an_ordinary_case_still_runs_the_assertion(self, write):
        case_file = self.case(write, 'assert_types(fine(), str)\n')
        (case,) = case_file.cases
        with pytest.raises(AssertionError):
            case_file.run(case)

    def test_a_case_in_the_same_file_is_unaffected(self, write):
        body = 'assert_types(boom(), Never)\nassert_types(fine(), int)\n'
        case_file = self.case(write, body)
        for case in case_file.cases:
            case_file.run(case)

    def test_every_case_carries_its_expression(self, write):
        case_file = self.case(write, 'assert_types(fine(), int)\n')
        (case,) = case_file.cases
        assert case.expression_code is not None
