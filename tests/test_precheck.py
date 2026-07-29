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
    assert "find_package(GTest CONFIG QUIET)" in cmake
    assert "find_package(GTest QUIET)" in cmake


def test_cpp_precheck_prefers_local_gtest_before_fetchcontent(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_demo.cpp"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("", encoding="utf-8")

    run_precheck(str(tmp_path), "cpp", "demo")
    cmake = (tmp_path / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "D:/googletest/install" in cmake
    assert "D:/gtest/install" in cmake
    assert cmake.index("D:/googletest/install") < cmake.index("FetchContent")
    assert cmake.index("find_package(GTest CONFIG QUIET)") < cmake.index("FetchContent")
    assert "GTest::gtest_main" in cmake


def test_cpp_target_name_uses_relative_path() -> None:
    assert _cpp_test_target_name("tests/a/test_utils.cpp") == "tests_a_test_utils"


def test_existing_build_config_is_not_overwritten(tmp_path: Path) -> None:
    cmake = tmp_path / "CMakeLists.txt"
    cmake.write_text("existing\n", encoding="utf-8")

    run_precheck(str(tmp_path), "cpp", "demo")

    assert cmake.read_text(encoding="utf-8") == "existing\n"


def test_cpp_precheck_generates_cmake_even_when_makefile_exists(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")

    run_precheck(str(tmp_path), "cpp", "demo")

    assert (tmp_path / "CMakeLists.txt").exists()


def test_python_precheck_creates_requirements_and_init_only(tmp_path: Path) -> None:
    run_precheck(str(tmp_path), "python", "demo")

    assert "pytest" in (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert (tmp_path / "src" / "__init__.py").exists()
    assert not (tmp_path / "src" / "main.py").exists()


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


def test_precheck_uses_aliases_for_supported_handlers(tmp_path: Path) -> None:
    go = tmp_path / "go"
    js = tmp_path / "js"
    csharp = tmp_path / "csharp"

    run_precheck(str(go), "golang", "demo")
    run_precheck(str(js), "js", "demo")
    run_precheck(str(csharp), "C#", "demo")

    assert (go / "go.mod").exists()
    assert (js / "package.json").exists()
    assert list(csharp.rglob("*.csproj"))


def test_precheck_unknown_language_skips_scaffolding(tmp_path: Path) -> None:
    report = run_precheck(str(tmp_path), "kotlin", "demo")

    assert any("未支持的语言" in line for line in report)
    assert list(tmp_path.iterdir()) == []


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
