from __future__ import annotations

import os
from pathlib import Path

from analysis.test_analyzer import CompilationResult, TestAnalyzer


def test_parse_pytest_passed_only() -> None:
    analysis = TestAnalyzer()._parse_test_output("=== 3 passed in 0.12s ===", 0)

    assert analysis.passed_tests == 3
    assert analysis.total_tests == 3
    assert analysis.modules["all"].is_module_passed is True


def test_parse_pytest_order_insensitive_failed_first() -> None:
    analysis = TestAnalyzer()._parse_test_output("=== 1 failed, 2 passed in 0.12s ===", 1)

    assert analysis.passed_tests == 2
    assert analysis.total_tests == 3
    assert analysis.modules["all"].is_module_passed is False


def test_parse_pytest_errors_count_as_failures() -> None:
    analysis = TestAnalyzer()._parse_test_output("=== 2 passed, 1 error in 0.12s ===", 1)

    assert analysis.passed_tests == 2
    assert analysis.total_tests == 3
    assert analysis.passed_modules == 0


def test_parse_pytest_collection_error() -> None:
    analysis = TestAnalyzer()._parse_test_output("=== 1 error during collection ===", 2)

    assert analysis.passed_tests == 0
    assert analysis.total_tests == 1


def test_parse_gtest_multiple_binaries() -> None:
    output = """
[  PASSED  ] 3 tests.
[  FAILED  ] 2 tests, listed below:
[  PASSED  ] 4 tests.
"""
    analysis = TestAnalyzer()._parse_test_output(output, 1)

    assert analysis.passed_tests == 7
    assert analysis.total_tests == 9


def test_nonzero_unparsed_output_is_implicit_failure() -> None:
    analysis = TestAnalyzer()._parse_test_output("ImportError: bad import", 1)

    assert analysis.passed_tests == 0
    assert analysis.total_tests == 1


def test_build_env_with_toolchain_deduplicates_paths(tmp_path, monkeypatch) -> None:
    tool_dir = tmp_path / "Tools"
    tool_dir.mkdir()
    monkeypatch.setenv("PATH", str(tool_dir).lower())
    analyzer = TestAnalyzer(extra_paths=[str(tool_dir)])

    env = analyzer._build_env_with_toolchain()
    entries = env["PATH"].split(os.pathsep)

    assert sum(Path(p).resolve() == tool_dir.resolve() for p in entries if p) == 1


def test_run_and_analyze_preserves_compilation_details(monkeypatch) -> None:
    analyzer = TestAnalyzer(compile_command="compile", test_command="test")

    def fake_compile(command: str) -> CompilationResult:
        return CompilationResult(success=True, warnings="compile stdout")

    def fake_tests(command: str):
        return analyzer._parse_test_output("=== 1 passed in 0.01s ===", 0)

    monkeypatch.setattr(analyzer, "_run_compilation", fake_compile)
    monkeypatch.setattr(analyzer, "_run_tests", fake_tests)

    analysis = analyzer.run_and_analyze()

    assert analysis.compilation.success is True
    assert analysis.compilation.warnings == "compile stdout"
    assert analysis.passed_tests == 1
