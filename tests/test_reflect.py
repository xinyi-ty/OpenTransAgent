from __future__ import annotations

from tools.reflect import (
    ReflectAction,
    ReflectExecutor,
    _combine_error_text,
    _infer_root_cause,
)


def test_reflect_action_allows_missing_test_results() -> None:
    action = ReflectAction(
        source_function="def add(a, b): return a + b",
        translated_code="int add(int a, int b) { return a - b; }",
        error_message="AssertionError: expected 3 got -1",
    )

    assert action.test_results == ""


def test_reflect_detects_python_traceback_with_exception_type() -> None:
    cause = _infer_root_cause('Traceback\n  File "main.py", line 12, in run\nValueError: bad')

    assert "Category: python traceback" in cause
    assert "main.py:12" in cause
    assert "ValueError: bad" in cause


def test_reflect_detects_cpp_compile_error() -> None:
    cause = _infer_root_cause("src/main.cpp:42: error: expected ';' before '}' token")

    assert "Category: c++ compile error" in cause
    assert "src/main.cpp:42" in cause
    assert "expected" in cause


def test_reflect_detects_c_and_hxx_compile_errors() -> None:
    assert "foo.c:9" in _infer_root_cause("foo.c:9: error: bad c")
    assert "foo.hxx:3" in _infer_root_cause("foo.hxx:3: error: bad hxx")


def test_reflect_detects_msvc_compile_error() -> None:
    cause = _infer_root_cause("foo.cpp(12): error C2143: syntax error: missing ';'")

    assert "MSVC compile error C2143" in cause
    assert "foo.cpp:12" in cause


def test_reflect_detects_pytest_assertion() -> None:
    cause = _infer_root_cause("E   AssertionError: expected 3 got -1")

    assert "Category: test assertion" in cause
    assert "expected 3 got -1" in cause


def test_reflect_detects_cannot_import_name() -> None:
    cause = _infer_root_cause("ImportError: cannot import name 'Foo' from 'bar'")

    assert "Cannot import name Foo from bar" in cause


def test_reflect_detects_msvc_include_error() -> None:
    cause = _infer_root_cause("fatal error C1083: Cannot open include file: 'foo.h': No such file")

    assert "Category: missing include" in cause
    assert "foo.h" in cause


def test_reflect_detects_linker_errors() -> None:
    unresolved = _infer_root_cause("main.obj : error LNK2019: unresolved external symbol Foo referenced")
    multiple = _infer_root_cause("multiple definition of `Foo::bar()'")

    assert "Category: linker unresolved symbol" in unresolved
    assert "Category: linker multiple definition" in multiple


def test_combine_error_text_preserves_tail() -> None:
    text = _combine_error_text("A" * 9000, "TAIL_ERROR")

    assert "truncated" in text
    assert "TAIL_ERROR" in text


def test_reflect_executor_returns_structured_strategy() -> None:
    action = ReflectAction(
        source_function="def add(a, b): return a + b",
        translated_code="int add(int a, int b) { return a - b; }",
        error_message="FAILED tests/test_add.py::test_add - AssertionError",
        test_results="expected 3 got -1",
    )
    obs = ReflectExecutor()(action)

    assert obs.root_cause
    assert obs.fix_strategy
    assert "[Reflect Analysis]" in obs.text
    assert "Next actions:" in obs.text
    assert "1." in obs.fix_strategy
