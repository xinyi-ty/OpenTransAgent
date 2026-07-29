from __future__ import annotations

from pathlib import Path

from workspace.precheck import _cpp_test_target_name, _ensure, run_precheck


def test_cpp_precheck_excludes_tests_and_only_configures_gtest_when_needed(tmp_path: Path) -> None:
    report = run_precheck(str(tmp_path), "c++", "demo")
    cmake = (tmp_path / "CMakeLists.txt").read_text(encoding="utf-8")

    assert report
    assert '"*.cc"' in cmake
    assert "list(FILTER SOURCES EXCLUDE REGEX" in cmake
    assert "public_tests" in cmake
    assert "find_package(GTest" not in cmake
    assert (tmp_path / "src" / ".gitkeep").exists()


def test_cpp_precheck_generates_unique_test_targets(tmp_path: Path) -> None:
    first = tmp_path / "tests" / "a" / "test_utils.cpp"
    second = tmp_path / "tests" / "b" / "test_utils.cpp"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    run_precheck(str(tmp_path), "cpp", "demo")
    cmake = (tmp_path / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "add_executable(tests_a_test_utils tests/a/test_utils.cpp)" in cmake
    assert "add_executable(tests_b_test_utils tests/b/test_utils.cpp)" in cmake
    assert cmake.count("find_package(GTest QUIET)") == 1


def test_cpp_target_name_uses_relative_path() -> None:
    assert _cpp_test_target_name("tests/a/test_utils.cpp") == "tests_a_test_utils"


def test_existing_build_config_is_not_overwritten(tmp_path: Path) -> None:
    cmake = tmp_path / "CMakeLists.txt"
    cmake.write_text("existing\n", encoding="utf-8")

    run_precheck(str(tmp_path), "cpp", "demo")

    assert cmake.read_text(encoding="utf-8") == "existing\n"


def test_go_rust_and_javascript_do_not_create_source_placeholders(tmp_path: Path) -> None:
    go = tmp_path / "go"
    rust = tmp_path / "rust"
    js = tmp_path / "js"

    run_precheck(str(go), "golang", "demo")
    run_precheck(str(rust), "rust", "demo")
    run_precheck(str(js), "js", "demo")

    assert (go / "go.mod").exists()
    assert not (go / "main.go").exists()
    assert (rust / "Cargo.toml").exists()
    assert not (rust / "src" / "lib.rs").exists()
    assert (js / "package.json").exists()
    assert not (js / "src" / "index.js").exists()


def test_language_alias_uses_shared_normalization(tmp_path: Path) -> None:
    run_precheck(str(tmp_path), "C++", "demo")

    assert (tmp_path / "CMakeLists.txt").exists()


def test_ensure_rejects_path_outside_workspace(tmp_path: Path) -> None:
    report: list[str] = []
    outside = tmp_path.parent / "outside.txt"

    created = _ensure(tmp_path, outside, "bad", report)

    assert created is False
    assert not outside.exists()
    assert any("路径超出工作区" in line for line in report)


def test_precheck_reports_are_not_double_indented(tmp_path: Path) -> None:
    report = run_precheck(str(tmp_path), "python", "demo")

    assert report
    assert all(not line.startswith("  ") for line in report)
