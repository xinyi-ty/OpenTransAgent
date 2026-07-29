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


def test_parse_ctest_all_passed_summary() -> None:
    analysis = TestAnalyzer()._parse_test_output(
        "100% tests passed, 0 tests failed out of 2\n",
        0,
    )

    assert analysis.passed_tests == 2
    assert analysis.total_tests == 2
    assert analysis.modules["all"].is_module_passed is True


def test_parse_ctest_partial_failure_summary() -> None:
    analysis = TestAnalyzer()._parse_test_output(
        "50% tests passed, 1 test failed out of 2\n",
        8,
    )

    assert analysis.passed_tests == 1
    assert analysis.total_tests == 2
    assert analysis.modules["all"].is_module_passed is False


def test_parse_ctest_summary_prevents_zero_zero_regression() -> None:
    output = """
Test project D:/workspace/build
    Start 1: tests_a
1/2 Test #1: tests_a .........................   Passed    0.01 sec
    Start 2: tests_b
2/2 Test #2: tests_b .........................   Passed    0.01 sec

100% tests passed, 0 tests failed out of 2
"""
    analysis = TestAnalyzer()._parse_test_output(output, 0)

    assert analysis.passed_tests == 2
    assert analysis.total_tests == 2


def test_detect_cmake_run_tests_prefers_ctest(tmp_path: Path) -> None:
    (tmp_path / "run_tests.sh").write_text(
        "cmake -S . -B build\nctest --test-dir build\n",
        encoding="utf-8",
    )

    compile_cmd, test_cmd = TestAnalyzer(str(tmp_path)).detect_commands()

    assert 'cmake -S . -B build -G "MinGW Makefiles"' in compile_cmd
    assert "cmake --build build" in compile_cmd
    assert "ctest --test-dir build" in test_cmd
    assert "Get-ChildItem" not in test_cmd
    assert "test_*.exe" not in test_cmd


def test_detect_commands_for_supported_project_markers(tmp_path: Path) -> None:
    cases = [
        ("pom.xml", "mvn compile -q", "mvn test"),
        ("build.gradle", "gradle compileJava", "gradle test"),
        ("package.json", "npm run build --if-present", "npm test"),
        ("Cargo.toml", "cargo build --quiet", "cargo test"),
        ("demo.csproj", "dotnet build --nologo -q", "dotnet test --nologo"),
    ]
    for marker, expected_compile, expected_test in cases:
        project = tmp_path / marker.replace(".", "_")
        project.mkdir()
        (project / marker).write_text("", encoding="utf-8")

        compile_cmd, test_cmd = TestAnalyzer(str(project)).detect_commands()

        assert compile_cmd == expected_compile
        assert test_cmd == expected_test


def test_detect_run_tests_pytest_without_cmake(tmp_path: Path) -> None:
    (tmp_path / "run_tests.sh").write_text("python -m pytest -v\n", encoding="utf-8")

    compile_cmd, test_cmd = TestAnalyzer(str(tmp_path)).detect_commands()

    assert compile_cmd == "echo OK"
    assert test_cmd == "python -m pytest -v"


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
