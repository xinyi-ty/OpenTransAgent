"""语言预检查：为目标语言自动生成构建配置文件（脚手架）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from config.languages import normalize_language


def _sanitize_name(name: str, fallback: str = "project") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    return cleaned or fallback


def _resolve_within(root: Path, path: Path) -> Path:
    """确认待生成路径位于 workspace 内。"""
    workspace = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"Precheck 路径超出工作区: {path}") from exc
    return resolved


def _ensure(root: Path, path: Path, content: str, report: list[str]) -> bool:
    """仅在 workspace 内且文件不存在时创建脚手架文件。"""
    try:
        target = _resolve_within(root, path)
        if target.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        report.append(f"[Precheck] 已创建: {target.relative_to(root).as_posix()}")
        return True
    except (OSError, ValueError) as exc:
        report.append(f"[Precheck] 创建失败: {path.name}: {exc}")
        return False


def _cpp_test_target_name(test_file: str) -> str:
    """由完整相对路径生成唯一、合法的 CMake target 名。"""
    rel_without_suffix = str(Path(test_file).with_suffix(""))
    return _sanitize_name(rel_without_suffix.replace("\\", "_").replace("/", "_"), "test")


# ── C++ ──────────────────────────────────────────────────────

def _precheck_cpp(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name, "translated")

    test_files: list[str] = []
    for directory in ("public_tests", "tests", "test"):
        test_dir = path / directory
        if not test_dir.is_dir():
            continue
        for pattern in ("*.cpp", "*.cxx", "*.cc"):
            for file in sorted(test_dir.rglob(pattern)):
                test_files.append(file.relative_to(path).as_posix())
    test_files = list(dict.fromkeys(test_files))

    cmake = path / "CMakeLists.txt"
    if not cmake.exists() and not (path / "Makefile").exists():
        lines = [
            "cmake_minimum_required(VERSION 3.16)",
            f"project({safe} LANGUAGES CXX)",
            "set(CMAKE_CXX_STANDARD 17)",
            "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
            "",
            "# Auto-discover translated source files; exclude tests/build outputs",
            'file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS "*.cpp" "*.cxx" "*.cc")',
            'list(FILTER SOURCES EXCLUDE REGEX "(^|/)(test|tests|public_tests|build|_deps|CMakeFiles)/")',
            "add_library(translated_lib ${SOURCES})",
        ]
        if test_files:
            lines.extend([
                "",
                "# Google Test — 优先使用本地安装，找不到时再下载",
                "find_package(GTest QUIET)",
                "if(NOT GTest_FOUND)",
                "    include(FetchContent)",
                "    FetchContent_Declare(",
                "        googletest",
                "        GIT_REPOSITORY https://github.com/google/googletest.git",
                "        GIT_TAG release-1.12.1",
                "    )",
                "    FetchContent_MakeAvailable(googletest)",
                "endif()",
                "enable_testing()",
                "",
                "# Per-file test executables (each compiles independently)",
            ])
            for test_file in test_files:
                target = _cpp_test_target_name(test_file)
                lines.extend([
                    f"add_executable({target} {test_file})",
                    f"target_include_directories({target} PRIVATE ${{CMAKE_SOURCE_DIR}})",
                    f"target_link_libraries({target} translated_lib GTest::gtest_main)",
                    f"add_test(NAME {target} COMMAND {target})",
                    "",
                ])
        _ensure(path, cmake, "\n".join(lines) + "\n", report)

    # 只创建目录占位，不生成会被误认为翻译产物的 C++ 源文件。
    _ensure(path, path / "src/.gitkeep", "", report)
    if not report:
        report.append("[Precheck] C++ 脚手架已存在")
    return report


# ── Python ───────────────────────────────────────────────────

def _precheck_python(path: Path, name: str) -> list[str]:
    _ = name
    report: list[str] = []
    _ensure(path, path / "requirements.txt", "# dependencies\npytest\n", report)
    _ensure(path, path / "src/__init__.py", "", report)
    if not report:
        report.append("[Precheck] Python 脚手架已存在")
    return report


# ── Java ─────────────────────────────────────────────────────

def _precheck_java(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name).lower().replace("-", "")
    pom = path / "pom.xml"
    if not pom.exists() and not (path / "build.gradle").exists():
        _ensure(path, pom, (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
            'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
            '  <modelVersion>4.0.0</modelVersion>\n'
            f'  <groupId>{safe}</groupId>\n'
            f'  <artifactId>{_sanitize_name(name)}</artifactId>\n'
            '  <version>0.1.0</version>\n'
            '  <properties>\n'
            '    <maven.compiler.source>17</maven.compiler.source>\n'
            '    <maven.compiler.target>17</maven.compiler.target>\n'
            '  </properties>\n'
            '</project>\n'
        ), report)
    if not report:
        report.append("[Precheck] Java 脚手架已存在")
    return report


# ── Rust ─────────────────────────────────────────────────────

def _precheck_rust(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name, "translated_project")
    _ensure(path, path / "Cargo.toml", (
        f'[package]\nname = "{safe}"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\n'
    ), report)
    # 不生成 src/lib.rs，避免占位源码被计入模型产出或最终结果。
    (path / "src").mkdir(parents=True, exist_ok=True)
    if not report:
        report.append("[Precheck] Rust 脚手架已存在")
    return report


# ── Go ───────────────────────────────────────────────────────

def _precheck_go(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name, "translated-project").replace("_", "-").lower()
    _ensure(path, path / "go.mod", f"module {safe}\n\ngo 1.21\n", report)
    # 不生成 main.go，避免占位源码被计入模型产出或最终结果。
    if not report:
        report.append("[Precheck] Go 脚手架已存在")
    return report


# ── C# ───────────────────────────────────────────────────────

def _precheck_csharp(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name, "TranslatedProject")
    if not list(path.rglob("*.csproj")):
        _ensure(path, path / f"{safe}.csproj", (
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <Nullable>enable</Nullable>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        ), report)
    if not report:
        report.append("[Precheck] C# 脚手架已存在")
    return report


# ── JavaScript ───────────────────────────────────────────────

def _precheck_javascript(path: Path, name: str) -> list[str]:
    report: list[str] = []
    safe = _sanitize_name(name, "translated-project")
    _ensure(path, path / "package.json", (
        '{\n'
        f'  "name": "{safe}",\n'
        '  "version": "0.1.0",\n'
        '  "private": true,\n'
        '  "scripts": {\n'
        '    "test": "node --test"\n'
        '  }\n'
        '}\n'
    ), report)
    # 不生成 src/index.js，避免占位源码被计入模型产出或最终结果。
    (path / "src").mkdir(parents=True, exist_ok=True)
    if not report:
        report.append("[Precheck] JavaScript 脚手架已存在")
    return report


# ═══════════════════════════════════════════════════════════════
#  分发入口
# ═══════════════════════════════════════════════════════════════

_PRECHECK_HANDLERS: dict[str, Callable[[Path, str], list[str]]] = {
    "cpp": _precheck_cpp,
    "python": _precheck_python,
    "java": _precheck_java,
    "rust": _precheck_rust,
    "go": _precheck_go,
    "csharp": _precheck_csharp,
    "javascript": _precheck_javascript,
}


def run_precheck(workspace_path: str, target_language: str, project_name: str) -> list[str]:
    """在当前可见 workspace 中生成目标语言构建脚手架，不访问 staging。"""
    path = Path(workspace_path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    normalized = normalize_language(target_language)
    handler = _PRECHECK_HANDLERS.get(normalized)
    if handler:
        return handler(path, project_name)
    return [f"[Precheck] 未支持的语言: {target_language}，跳过脚手架生成"]
