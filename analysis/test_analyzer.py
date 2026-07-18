"""
TestAnalyzer — 测试结果分析器。
独立于 Agent，可替换为不同语言的测试框架。
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CompilationResult:
    """编译结果"""

    success: bool = False
    errors: str = ""
    warnings: str = ""


@dataclass
class ModuleResult:
    """模块测试结果"""

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
    """完整的测试分析结果"""

    compilation: CompilationResult = field(default_factory=CompilationResult)
    modules: dict[str, ModuleResult] = field(default_factory=dict)
    passed_tests: int = 0
    total_tests: int = 0
    passed_modules: int = 0
    total_modules: int = 0

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
    - compile_command 和 test_command 可配置
    """

    def __init__(
        self,
        working_dir: str = ".",
        compile_command: str = "",
        test_command: str = "",
        timeout: int = 60,
    ):
        self.working_dir = Path(working_dir).resolve()
        self.compile_command = compile_command
        self.test_command = test_command
        self.timeout = timeout

    def detect_commands(self) -> tuple[str, str]:
        """
        根据项目类型自动检测编译和测试命令。

        可控性：可覆盖此方法实现自定义命令检测。
        """
        has_maven = (self.working_dir / "pom.xml").exists()
        has_gradle = (self.working_dir / "build.gradle").exists()
        has_package = (self.working_dir / "package.json").exists()
        has_setup = (self.working_dir / "setup.py").exists() or (self.working_dir / "pyproject.toml").exists()
        has_cargo = (self.working_dir / "Cargo.toml").exists()
        has_csproj = list(self.working_dir.rglob("*.csproj"))

        if has_maven:
            return "mvn compile -q", "mvn test"
        elif has_gradle:
            return "gradle compileJava", "gradle test"
        elif has_package:
            return "npm run build --if-present", "npm test"
        elif has_setup:
            return "python -m compileall . -q", "python -m pytest tests/ -v"
        elif has_cargo:
            return "cargo build --quiet", "cargo test"
        elif has_csproj:
            return "dotnet build --nologo -q", "dotnet test --nologo"
        else:
            return "python -m compileall . -q", "python -m pytest tests/ -v || true"

    def run_and_analyze(self, working_path: Optional[Path] = None) -> TestAnalysis:
        """
        运行测试并分析结果。

        返回：
            TestAnalysis：包含编译结果、模块测试结果、统计信息
        """
        if working_path:
            self.working_dir = working_path.resolve()

        analysis = TestAnalysis()

        # 编译
        compile_cmd = self.compile_command
        test_cmd = self.test_command
        if not compile_cmd or not test_cmd:
            detected_compile, detected_test = self.detect_commands()
            compile_cmd = compile_cmd or detected_compile
            test_cmd = test_cmd or detected_test

        # 运行编译
        analysis.compilation = self._run_compilation(compile_cmd)
        if not analysis.compilation.success:
            return analysis

        # 运行测试
        analysis = self._run_tests(test_cmd)
        return analysis

    def _run_compilation(self, command: str) -> CompilationResult:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=self.working_dir, timeout=self.timeout,
            )
            return CompilationResult(
                success=result.returncode == 0,
                errors=result.stderr[:2000] if result.stderr else "",
                warnings=result.stdout[:2000] if result.stdout else "",
            )
        except Exception as e:
            return CompilationResult(success=False, errors=str(e))

    def _run_tests(self, command: str) -> TestAnalysis:
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=self.working_dir, timeout=self.timeout,
            )
            output = result.stdout + result.stderr
            return self._parse_test_output(output, result.returncode)
        except Exception as e:
            analysis = TestAnalysis()
            analysis.compilation = CompilationResult(success=True)
            return analysis

    def _parse_test_output(self, output: str, exit_code: int) -> TestAnalysis:
        """
        解析测试输出，提取通过数/总数/模块信息。

        可控性：可覆盖此方法适配不同测试框架的输出格式。
        """
        import re

        analysis = TestAnalysis()
        analysis.compilation = CompilationResult(success=True)

        # 尝试解析 pytest 格式: "1 passed, 2 failed"
        passed = 0
        failed = 0

        # pytest summary line
        summary = re.search(
            r"=+\s+(\d+)\s+passed.*?(\d+)\s+failed",
            output, re.DOTALL,
        )
        if summary:
            passed = int(summary.group(1))
            failed = int(summary.group(2))
        else:
            # 尝试解析 passed/failed counts
            p = re.search(r"(\d+)\s+passed", output)
            f = re.search(r"(\d+)\s+failed", output)
            if p:
                passed = int(p.group(1))
            if f:
                failed = int(f.group(1))

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

    def format_results(self, analysis: TestAnalysis) -> str:
        """格式化测试分析结果为可读文本。"""
        lines = []
        lines.append(f"Compilation: {'SUCCESS' if analysis.compilation.success else 'FAILED'}")
        if not analysis.compilation.success:
            lines.append(f"Errors: {analysis.compilation.errors[:500]}")

        lines.append(f"Overall: {analysis.passed_tests}/{analysis.total_tests} "
                     f"({analysis.overall_pass_rate:.1f}%)")
        lines.append(f"Modules: {analysis.passed_modules}/{analysis.total_modules} "
                     f"({analysis.module_pass_rate:.1f}%)")

        if analysis.modules:
            for name, mod in analysis.modules.items():
                status = "PASS" if mod.is_module_passed else "FAIL"
                lines.append(f"  [{status}] {name}: {mod.passed_tests}/{mod.total_tests}")

        return "\n".join(lines)
