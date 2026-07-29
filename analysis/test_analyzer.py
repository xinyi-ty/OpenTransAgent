"""
TestAnalyzer — 测试结果分析器。
独立于 Agent，可替换为不同语言的测试框架。

与 run.py 的关系：
  - run.py format_feedback() 是 LLM 反馈专用的格式化函数（含 action items）
  - 本类的职责是执行测试 + 解析结果，不涉及 LLM 反馈格式
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import TimeoutExpired, run as subprocess_run

from utils.logger import logger
from config.settings import get_toolchain_paths


@dataclass
class CompilationResult:
    """编译结果。"""

    success: bool = False
    errors: str = ""
    warnings: str = ""  # 实际存储的是 stdout 输出（可能包含编译警告或普通输出）


@dataclass
class ModuleResult:
    """模块测试结果。"""

    module_name: str = ""
    passed_tests: int = 0
    total_tests: int = 0
    is_module_passed: bool = False

    @property
    def pass_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return (self.passed_tests / self.total_tests) * 100


@dataclass
class TestAnalysis:
    """完整的测试分析结果。"""

    compilation: CompilationResult = field(default_factory=CompilationResult)
    modules: dict[str, ModuleResult] = field(default_factory=dict)
    passed_tests: int = 0
    total_tests: int = 0
    passed_modules: int = 0
    total_modules: int = 0
    raw_output: str = ""  # 测试命令的原始输出，用于反馈

    @property
    def overall_pass_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return (self.passed_tests / self.total_tests) * 100

    @property
    def module_pass_rate(self) -> float:
        if self.total_modules == 0:
            return 0.0
        return (self.passed_modules / self.total_modules) * 100


class TestAnalyzer:
    """
    测试分析器，运行测试并解析结果。

    可控性：
      - 可继承此类，实现不同语言的测试命令和解析逻辑
      - compile_command / test_command / extra_paths / raw_output_limit 均可配置
      - 工具链路径自动检测（shutil.which + 常见安装位置），也可通过
        extra_paths 参数或 TOOLCHAIN_PATHS 环境变量显式覆盖
    """

    # ── 常见工具链安装路径（用于自动检测，支持 %VAR% 环境变量） ──
    _CMAKE_LOCATIONS: list[str] = [
        r"C:\Program Files\CMake\bin",
        r"C:\Program Files (x86)\CMake\bin",
    ]
    _MINGW_LOCATIONS: list[str] = [
        r"C:\mingw64\bin",
        r"C:\MinGW\bin",
        r"C:\msys64\mingw64\bin",
        r"C:\msys64\ucrt64\bin",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages",  # 见 _locate_winget_mingw()
    ]
    _GIT_BASH_LOCATIONS: list[str] = [
        r"C:\Program Files\Git\usr\bin",
        r"C:\Program Files (x86)\Git\usr\bin",
        r"D:\Git\usr\bin",
        r"%USERPROFILE%\scoop\apps\git\current\usr\bin",
    ]

    def __init__(
        self,
        working_dir: str = ".",
        compile_command: str = "",
        test_command: str = "",
        timeout: int = 60,
        extra_paths: list[str] | None = None,
        raw_output_limit: int = 5000,
    ):
        """
        参数：
            working_dir: 工作目录
            compile_command: 编译命令（空则自动检测）
            test_command: 测试命令（空则自动检测）
            timeout: 编译/测试超时秒数
            extra_paths: 额外工具链路径（None 则读取 TOOLCHAIN_PATHS 环境变量，再退回到空列表）
            raw_output_limit: 保存给 LLM 的原始输出上限（0 表示不截断）
        """
        self.working_dir = Path(working_dir).resolve()
        self.compile_command = compile_command
        self.test_command = test_command
        self.timeout = timeout
        self.raw_output_limit = raw_output_limit
        self._extra_paths = self._resolve_extra_paths(extra_paths)

    # ── 工具链路径解析 ─────────────────────────────────────────

    def _resolve_extra_paths(
        self, extra_paths: list[str] | None
    ) -> list[str]:
        """解析额外路径：显式参数 > .env / 环境变量 > 自动检测。"""
        if extra_paths is not None:
            return extra_paths
        env_paths = get_toolchain_paths()
        if env_paths.strip():
            return [p.strip() for p in env_paths.split(os.pathsep) if p.strip()]
        return self._auto_detect_paths()

    @staticmethod
    def _find_tool_dir(
        tool_name: str, search_dirs: list[str]
    ) -> list[str]:
        """查找工具所在目录。

        如果工具已可通过 PATH 访问则不返回（无需添加）；
        否则在 search_dirs 中查找常见安装位置（支持 %VAR% 语法）。
        """
        if shutil.which(tool_name):
            return []
        for d in search_dirs:
            expanded = os.path.expandvars(d)
            if not os.path.isdir(expanded):
                continue
            for ext in ("", ".exe", ".bat", ".cmd"):
                if os.path.isfile(os.path.join(expanded, tool_name + ext)):
                    logger.info(f"Auto-detected {tool_name} at {expanded}")
                    return [expanded]
        logger.debug(f"Could not locate {tool_name} via common paths")
        return []

    def _auto_detect_paths(self) -> list[str]:
        """自动检测编译工具链路径。

        策略：
          1. 优先 shutil.which（工具已在 PATH 中则跳过）
          2. 扫描常见安装目录列表
          3. 特殊策略：从 git.exe 推导 Git Bash、扫描 WinGet 包目录
        """
        paths: list[str] = []

        # ── CMake ──────────────────────────────────────────────
        paths.extend(self._find_tool_dir("cmake", self._CMAKE_LOCATIONS))

        # ── MinGW (g++) ────────────────────────────────────────
        if not shutil.which("g++"):
            paths.extend(self._find_tool_dir("g++", self._MINGW_LOCATIONS))
        if not shutil.which("g++"):
            mingw = self._locate_winget_mingw()
            if mingw:
                logger.info(f"Auto-detected MinGW via WinGet at {mingw}")
                paths.append(mingw)

        # ── Git Bash (make) ────────────────────────────────────
        if not shutil.which("make"):
            paths.extend(self._find_tool_dir("make", self._GIT_BASH_LOCATIONS))
        if not shutil.which("make"):
            git_bash = self._locate_git_bash()
            if git_bash:
                logger.info(f"Auto-detected Git Bash at {git_bash}")
                paths.append(git_bash)

        if paths:
            logger.info(f"Auto-detected toolchain paths: {paths}")
        return paths

    @staticmethod
    def _locate_git_bash() -> str | None:
        """从 git.exe 安装位置推导 Git Bash usr/bin 目录。"""
        git_path = shutil.which("git")
        if not git_path:
            return None
        # git.exe 在 Git/bin/git.exe → 上两级得 Git/ 根 → 拼接 usr/bin
        git_root = os.path.dirname(os.path.dirname(os.path.abspath(git_path)))
        usr_bin = os.path.join(git_root, "usr", "bin")
        if os.path.isdir(usr_bin) and os.path.isfile(os.path.join(usr_bin, "make.exe")):
            return usr_bin
        return None

    @staticmethod
    def _locate_winget_mingw() -> str | None:
        """扫描 WinGet MinGW 安装目录。"""
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if not local_appdata:
            return None
        packages_dir = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if not os.path.isdir(packages_dir):
            return None
        for pkg_name in os.listdir(packages_dir):
            if "mingw" not in pkg_name.lower() and "winlibs" not in pkg_name.lower():
                continue
            mingw_bin = os.path.join(packages_dir, pkg_name, "mingw64", "bin")
            if os.path.isdir(mingw_bin) and os.path.isfile(os.path.join(mingw_bin, "g++.exe")):
                return mingw_bin
        return None

    def _build_env_with_toolchain(self) -> dict[str, str]:
        """构建含工具链路径的进程环境变量。"""
        env = os.environ.copy()
        path_entries = env.get("PATH", "").split(os.pathsep)
        normalized = {
            os.path.normcase(os.path.abspath(p))
            for p in path_entries
            if p
        }
        extra: list[str] = []
        for p in self._extra_paths:
            if not os.path.isdir(p):
                continue
            key = os.path.normcase(os.path.abspath(p))
            if key in normalized:
                continue
            extra.append(p)
            normalized.add(key)
        if extra:
            env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
        return env

    # ── 命令检测 ───────────────────────────────────────────────

    def detect_commands(self) -> tuple[str, str]:
        """
        根据项目类型自动检测编译和测试命令。

        可控性：可覆盖此方法实现自定义命令检测。
        """
        working_dir = self.working_dir
        has_maven = (working_dir / "pom.xml").exists()
        has_gradle = (working_dir / "build.gradle").exists()
        has_package = (working_dir / "package.json").exists()
        has_setup = (
            (working_dir / "setup.py").exists()
            or (working_dir / "pyproject.toml").exists()
        )
        has_cargo = (working_dir / "Cargo.toml").exists()
        has_csproj = list(working_dir.rglob("*.csproj"))
        has_run_tests = (working_dir / "run_tests.sh").exists()

        if has_maven:
            return "mvn compile -q", "mvn test"
        elif has_gradle:
            return "gradle compileJava", "gradle test"
        elif has_package:
            return "npm run build --if-present", "npm test"
        elif has_cargo:
            return "cargo build --quiet", "cargo test"
        elif has_csproj:
            return "dotnet build --nologo -q", "dotnet test --nologo"
        elif has_run_tests:
            return self._detect_from_run_tests_script(working_dir)
        else:
            return (
                "python -m compileall . -q",
                "python -m pytest tests/ -v",
            )

    @staticmethod
    def _detect_from_run_tests_script(working_dir: Path) -> tuple[str, str]:
        """从 run_tests.sh 推断编译和测试命令。

        对 cmake 项目返回跨平台的 cmake 命令（避免 bash 依赖和生成器问题）；
        对 pytest 项目直接返回 python 测试命令。
        """
        try:
            content = (working_dir / "run_tests.sh").read_text(encoding="utf-8")
            lines = [
                l.strip() for l in content.split("\n")
                if l.strip() and not l.startswith("#") and not l.startswith("set ")
            ]
            all_text = "\n".join(lines)

            # pytest 项目
            if "pytest" in all_text and "cmake" not in all_text:
                return "echo OK", "python -m pytest -v"

            # cmake 项目 — 统一使用 CTest 汇总，避免逐个 exe 搜索漏掉路径化 target 名。
            if "cmake" in all_text:
                return (
                    "cmake -S . -B build -G \"MinGW Makefiles\" -DCMAKE_BUILD_TYPE=Release "
                    "&& cmake --build build --config Release",
                    "ctest --test-dir build --output-on-failure -C Release",
                )

            return "echo OK", "bash run_tests.sh"
        except Exception:
            pass
        return "echo OK", "python -m pytest -v"

    # ── 主入口 ─────────────────────────────────────────────────

    def run_and_analyze(
        self, working_path: Path | None = None
    ) -> TestAnalysis:
        """
        运行测试并分析结果。

        返回：
            TestAnalysis：包含编译结果、模块测试结果、统计信息
        """
        if working_path:
            self.working_dir = working_path.resolve()

        analysis = TestAnalysis()

        # 命令检测（未指定的部分自动补充）
        compile_cmd = self.compile_command
        test_cmd = self.test_command
        if not compile_cmd or not test_cmd:
            detected_compile, detected_test = self.detect_commands()
            compile_cmd = compile_cmd or detected_compile
            test_cmd = test_cmd or detected_test

        # 运行编译
        compilation_result = self._run_compilation(compile_cmd)
        analysis.compilation = compilation_result
        if not compilation_result.success:
            logger.warning("  ⚠️ Compilation failed, skipping tests")
            return analysis

        # 运行测试，并保留编译阶段的 stdout/stderr 诊断信息
        analysis = self._run_tests(test_cmd)
        analysis.compilation = compilation_result
        return analysis

    # ── 编译 ───────────────────────────────────────────────────

    def _run_compilation(self, command: str) -> CompilationResult:
        try:
            env = self._build_env_with_toolchain()
            result = subprocess_run(
                command,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.working_dir,
                timeout=self.timeout,
                env=env,
            )
            status = "✅" if result.returncode == 0 else "❌"
            logger.info(f"  {status} Compilation: {'SUCCESS' if result.returncode == 0 else 'FAILED'}")
            return CompilationResult(
                success=result.returncode == 0,
                errors=result.stderr[:2000] if result.stderr else "",
                warnings=result.stdout[:2000] if result.stdout else "",
            )
        except TimeoutExpired:
            logger.warning(f"Compilation timed out after {self.timeout}s")
            return CompilationResult(
                success=False,
                errors=f"Compilation timed out after {self.timeout}s",
            )
        except Exception as e:
            logger.error(f"Compilation error: {e}")
            return CompilationResult(success=False, errors=str(e))

    # ── 测试 ───────────────────────────────────────────────────

    def _run_tests(self, command: str) -> TestAnalysis:
        """执行测试命令并解析输出。

        异常保护：任何运行时异常（超时、崩溃）均返回 failure 状态的 TestAnalysis，
        避免吞掉异常返回假成功导致外层误判。
        """
        try:
            env = self._build_env_with_toolchain()
            result = subprocess_run(
                command,
                shell=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.working_dir,
                timeout=self.timeout,
                env=env,
            )
            output = result.stdout + result.stderr

            analysis = self._parse_test_output(output, result.returncode)
            analysis.raw_output = self._truncate_output(output)
            return analysis

        except TimeoutExpired:
            logger.warning(f"Test timed out after {self.timeout}s")
            analysis = TestAnalysis()
            analysis.compilation = CompilationResult(
                success=False,
                errors=f"Test timed out after {self.timeout}s",
            )
            analysis.raw_output = f"[TIMEOUT] Test did not complete within {self.timeout}s"
            return analysis

        except Exception as e:
            logger.error(f"Test execution error: {e}")
            analysis = TestAnalysis()
            analysis.compilation = CompilationResult(
                success=False,
                errors=f"Test execution error: {e}",
            )
            analysis.raw_output = str(e)
            return analysis

    def _truncate_output(self, output: str) -> str:
        """智能截断输出：优先保留末尾（错误摘要通常在尾部）。

        当 raw_output_limit=0 时不截断。
        """
        if self.raw_output_limit <= 0 or len(output) <= self.raw_output_limit:
            return output
        half = self.raw_output_limit // 2
        return (
            output[:half]
            + f"\n... (truncated {len(output) - self.raw_output_limit} chars) ...\n"
            + output[-half:]
        )

    # ── 输出解析 ───────────────────────────────────────────────

    @staticmethod
    def _parse_pytest_summary(output: str) -> tuple[int, int] | None:
        """解析 pytest summary，返回 (passed, failed_or_errors)。"""
        import re

        passed = 0
        failed = 0
        found = False
        summary_lines = re.findall(r"=+\s*([^=\n]*(?:passed|failed|error|errors)[^=\n]*)\s*=+", output, re.IGNORECASE)
        if not summary_lines:
            summary_lines = [line for line in output.splitlines() if re.search(r"\b(passed|failed|error|errors)\b", line, re.IGNORECASE)]

        for line in summary_lines:
            for count, word in re.findall(r"(\d+)\s+(passed|failed|error|errors)\b", line, re.IGNORECASE):
                found = True
                n = int(count)
                word = word.lower()
                if word == "passed":
                    passed += n
                else:
                    failed += n
        return (passed, failed) if found else None

    @staticmethod
    def _parse_ctest_summary(output: str) -> tuple[int, int] | None:
        """解析 CTest 汇总输出，返回 (passed, failed)。"""
        import re

        if re.search(r"No tests were found", output, re.IGNORECASE):
            return (0, 0)

        m = re.search(
            r"\b\d+%\s+tests\s+passed"
            r"(?:\s*,\s*(\d+)\s+tests?\s+failed)?"
            r"\s+out\s+of\s+(\d+)\b",
            output,
            re.IGNORECASE,
        )
        if not m:
            return None
        failed = int(m.group(1) or 0)
        total = int(m.group(2))
        return (max(total - failed, 0), failed)

    @staticmethod
    def _parse_gtest_summary(output: str) -> tuple[int, int] | None:
        """解析 Google Test 输出，支持多个二进制结果累加。"""
        import re

        passed = sum(
            int(n) for n in re.findall(
                r"\[\s+PASSED\s+\]\s+(\d+)\s+test", output, re.IGNORECASE
            )
        )
        failed = sum(
            int(n) for n in re.findall(
                r"\[\s+FAILED\s+\]\s+(\d+)\s+test", output, re.IGNORECASE
            )
        )
        return (passed, failed) if passed > 0 or failed > 0 else None

    def _parse_test_output(self, output: str, exit_code: int) -> TestAnalysis:
        """
        解析测试输出，提取通过数/总数/模块信息。

        当前支持 pytest、Google Test，并在无法解析但退出码非零时降级为
        1 个隐式失败，避免把基础设施/导入错误误判为无测试。
        """
        analysis = TestAnalysis()
        analysis.compilation = CompilationResult(success=True)
        passed = 0
        failed = 0

        parsed = self._parse_pytest_summary(output)
        if parsed:
            passed, failed = parsed
            logger.debug(f"Parsed pytest output: {passed} passed, {failed} failed/errors")

        if passed == 0 and failed == 0:
            parsed = self._parse_ctest_summary(output)
            if parsed:
                passed, failed = parsed
                logger.debug(f"Parsed CTest output: {passed} passed, {failed} failed")

        if passed == 0 and failed == 0:
            parsed = self._parse_gtest_summary(output)
            if parsed:
                passed, failed = parsed
                logger.debug(f"Parsed Google Test output: {passed} passed, {failed} failed")

        if passed == 0 and failed == 0 and exit_code != 0:
            # pytest exit code 5 = "no tests collected" — 不是真正失败，
            # 不应阻止翻译流程。保留 total=0，由 run.py 的 no_tests 逻辑处理。
            if exit_code == 5:
                logger.info(
                    f"No tests collected (pytest exit code 5); "
                    f"allowing layer to proceed if completeness OK"
                )
            else:
                logger.warning(
                    f"No tests parsed but exit code = {exit_code} (non-zero), "
                    f"treating as 1 implicit failure"
                )
                failed = 1

        analysis.total_tests = passed + failed
        analysis.passed_tests = passed
        analysis.total_modules = 1
        analysis.passed_modules = 1 if failed == 0 else 0

        module = ModuleResult(
            module_name="all",
            passed_tests=passed,
            total_tests=analysis.total_tests,
            is_module_passed=failed == 0,
        )
        analysis.modules["all"] = module

        return analysis

    # ── 结果格式化（已迁移到 run.py format_feedback()，此处不再保留） ─
    # format_results() 之前在此处，与 run.py 功能重复且从未被调用，
    # 已被移除。如果需要为 LLM 生成结构化反馈，请使用 run.py 的
    # format_feedback()。
