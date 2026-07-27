"""语言预检查：为目标语言自动生成构建配置文件（脚手架）。"""

import re
from pathlib import Path
from typing import Callable


def _normalize_language(language: str) -> str:
    raw = language.strip().lower()
    aliases = {"c++": "cpp", "cplusplus": "cpp", "c#": "csharp", "cs": "csharp", "golang": "go", "js": "javascript"}
    return aliases.get(raw, raw)


def _sanitize_name(name: str, fallback: str = "project") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    return cleaned or fallback


def _ensure(path: Path, content: str, report: list[str]):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    report.append(f"  [Precheck] 已创建: {path.name}")


# ── C++ ──────────────────────────────────────────────────────

def _precheck_cpp(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name, "translated")

    # 收集已有的测试源文件
    test_files = []
    for d in ["public_tests", "tests", "test"]:
        test_dir = path / d
        if test_dir.exists():
            for ext in ("*.cpp", "*.cxx"):
                for f in sorted(test_dir.rglob(ext)):
                    test_files.append(str(f.relative_to(path).as_posix()))

    cmake = path / "CMakeLists.txt"
    if not cmake.exists() and not (path / "Makefile").exists():
        lines = [
            f"cmake_minimum_required(VERSION 3.16)",
            f"project({safe} LANGUAGES CXX)",
            f"set(CMAKE_CXX_STANDARD 17)",
            f"set(CMAKE_CXX_STANDARD_REQUIRED ON)",
            f"",
            f"# Auto-discover translated source files",
            f"file(GLOB_RECURSE SOURCES \"*.cpp\" \"*.cxx\")",
            f"add_library(translated_lib ${{SOURCES}})",
            f"",
            f"# Google Test — 优先找本地安装，找不到再从 GitHub 下载",
            f"find_package(GTest QUIET)",
            f"if(NOT GTest_FOUND)",
            f"    include(FetchContent)",
            f"    FetchContent_Declare(",
            f"        googletest",
            f"        GIT_REPOSITORY https://github.com/google/googletest.git",
            f"        GIT_TAG release-1.12.1",
            f"    )",
            f"    FetchContent_MakeAvailable(googletest)",
            f"endif()",
            f"enable_testing()",
        ]
        if test_files:
            lines.append(f"")
            lines.append(f"# Per-file test executables (each compiles independently)")
            for tf in test_files:
                test_name = Path(tf).stem
                lines.append(f"add_executable({test_name} {tf})")
                lines.append(f"target_include_directories({test_name} PRIVATE ${{CMAKE_SOURCE_DIR}})")
                lines.append(f"target_link_libraries({test_name} translated_lib GTest::gtest_main)")
                lines.append(f"")
        _ensure(cmake, "\n".join(lines), r)
    _ensure(path / "src/.gitkeep", "", r)
    if not r:
        r.append("  [Precheck] CMakeLists.txt 已存在")
    return r


# ── Python ───────────────────────────────────────────────────

def _precheck_python(path: Path, name: str) -> list[str]:
    r = []
    req = path / "requirements.txt"
    if not req.exists():
        _ensure(req, "# dependencies\npytest\n", r)
    _ensure(path / "src/__init__.py", "", r)
    if not r:
        r.append("  [Precheck] Python 脚手架已存在")
    return r


# ── Java ─────────────────────────────────────────────────────

def _precheck_java(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name).lower().replace("-", "")
    pom = path / "pom.xml"
    if not pom.exists() and not (path / "build.gradle").exists():
        _ensure(pom, (
            '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
            'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
            f'  <modelVersion>4.0.0</modelVersion>\n'
            f'  <groupId>{safe}</groupId>\n'
            f'  <artifactId>{_sanitize_name(name)}</artifactId>\n'
            f'  <version>0.1.0</version>\n'
            f'  <properties>\n'
            f'    <maven.compiler.source>17</maven.compiler.source>\n'
            f'    <maven.compiler.target>17</maven.compiler.target>\n'
            f'  </properties>\n'
            f'</project>\n'
        ), r)
    if not r:
        r.append("  [Precheck] pom.xml 已存在")
    return r


# ── Rust ─────────────────────────────────────────────────────

def _precheck_rust(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name, "translated_project")
    cargo = path / "Cargo.toml"
    if not cargo.exists():
        _ensure(cargo, (
            f'[package]\nname = "{safe}"\nversion = "0.1.0"\nedition = "2021"\n\n'
            f'[dependencies]\n'
        ), r)
    _ensure(path / "src/lib.rs", "// Precheck scaffold\n", r)
    if not r:
        r.append("  [Precheck] Cargo.toml 已存在")
    return r


# ── Go ───────────────────────────────────────────────────────

def _precheck_go(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name, "translated-project").replace("_", "-").lower()
    mod = path / "go.mod"
    if not mod.exists():
        _ensure(mod, f"module {safe}\n\ngo 1.21\n", r)
    _ensure(path / "main.go", "package main\n\nfunc main() {}\n", r)
    if not r:
        r.append("  [Precheck] go.mod 已存在")
    return r


# ── C# ───────────────────────────────────────────────────────

def _precheck_csharp(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name, "TranslatedProject")
    csproj_files = list(path.rglob("*.csproj"))
    if not csproj_files:
        _ensure(path / f"{safe}.csproj", (
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <Nullable>enable</Nullable>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        ), r)
    if not r:
        r.append("  [Precheck] .csproj 已存在")
    return r


# ── JavaScript ───────────────────────────────────────────────

def _precheck_javascript(path: Path, name: str) -> list[str]:
    r = []
    safe = _sanitize_name(name, "translated-project")
    pkg = path / "package.json"
    if not pkg.exists():
        _ensure(pkg, (
            '{\n'
            f'  "name": "{safe}",\n'
            '  "version": "0.1.0",\n'
            '  "private": true,\n'
            '  "scripts": {\n'
            '    "test": "node --test"\n'
            '  }\n'
            '}\n'
        ), r)
    _ensure(path / "src/index.js", "module.exports = {};\n", r)
    if not r:
        r.append("  [Precheck] package.json 已存在")
    return r


# ═══════════════════════════════════════════════════════════════
#  分发入口
# ═══════════════════════════════════════════════════════════════

_PRECHECK_HANDLERS: dict[str, Callable[..., list[str]]] = {
    "cpp": _precheck_cpp,
    "python": _precheck_python,
    "java": _precheck_java,
    "rust": _precheck_rust,
    "go": _precheck_go,
    "csharp": _precheck_csharp,
    "javascript": _precheck_javascript,
}


def run_precheck(workspace_path: str, target_language: str, project_name: str) -> list[str]:
    """在 workspace 中生成目标语言所需的构建配置文件（如已存在则跳过）。"""
    path = Path(workspace_path).resolve()
    normalized = _normalize_language(target_language)
    report: list[str] = []
    handler = _PRECHECK_HANDLERS.get(normalized)
    if handler:
        report.extend(handler(path, project_name))
    else:
        report.append(f"[Precheck] 未支持的语言: {target_language}，跳过脚手架生成")
    return report
