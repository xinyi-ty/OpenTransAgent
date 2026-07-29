"""反思纠错工具：reflect — 分析编译/测试错误的根因并给出修复策略（不修改文件）。"""

from __future__ import annotations

import re

from openhands.sdk.tool import Action, Observation, ToolExecutor, ToolDefinition, register_tool
from pydantic import Field

_ERROR_TEXT_LIMIT = 8000


class ReflectAction(Action):
    source_function: str = Field(default="", description="待翻译的源函数/源代码片段")
    translated_code: str = Field(default="", description="当前的翻译结果")
    error_message: str = Field(default="", description="编译或测试错误信息")
    test_results: str = Field(default="", description="测试执行结果")


class ReflectObservation(Observation):
    root_cause: str = Field(default="")
    fix_strategy: str = Field(default="")


class ReflectExecutor(ToolExecutor):
    def __call__(self, action, conversation=None):
        text = _combine_error_text(action.error_message, action.test_results)
        root_cause = _infer_root_cause(text)
        fix_strategy = _build_fix_strategy(
            root_cause=root_cause,
            source_function=action.source_function,
            translated_code=action.translated_code,
            error_text=text,
        )
        result = _format_reflection(root_cause, fix_strategy)
        return ReflectObservation.from_text(
            text=result,
            root_cause=root_cause,
            fix_strategy=fix_strategy,
        )


# ═══════════════════════════════════════════════════════════════
#  确定性错误分析
# ═══════════════════════════════════════════════════════════════

def _combine_error_text(error_message: str, test_results: str) -> str:
    """合并错误文本，首尾保留，避免丢失末尾 summary。"""
    parts = [p.strip() for p in (error_message, test_results) if p and p.strip()]
    text = "\n".join(parts)
    if len(text) <= _ERROR_TEXT_LIMIT:
        return text
    half = _ERROR_TEXT_LIMIT // 2
    return (
        text[:half]
        + f"\n... (truncated {len(text) - _ERROR_TEXT_LIMIT} chars) ...\n"
        + text[-half:]
    )


def _last_python_exception(text: str) -> str:
    matches = re.findall(r"^([A-Za-z_][\w.]*Error|[A-Za-z_][\w.]*Exception):\s*(.+)$", text, re.MULTILINE)
    if not matches:
        return ""
    exc_type, message = matches[-1]
    return f"{exc_type}: {message.strip()}"


def _infer_root_cause(text: str) -> str:
    """从常见编译/测试输出中推断根因。"""
    if not text:
        return "Category: missing error details | No error details were provided."

    cannot_import = re.search(r"ImportError: cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]", text)
    if cannot_import:
        name, module = cannot_import.groups()
        return f"Category: python import error | Cannot import name {name} from {module}."

    module = re.search(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]", text)
    if module:
        return f"Category: python import error | Missing Python module/import: {module.group(1)}."

    import_error = re.search(r"ImportError: (.+)", text)
    if import_error:
        return f"Category: python import error | {import_error.group(1).strip()}."

    traceback_loc = re.findall(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?', text)
    if traceback_loc:
        file, line, func = traceback_loc[-1]
        suffix = f" in {func.strip()}" if func else ""
        exc = _last_python_exception(text)
        detail = f": {exc}" if exc else ""
        return f"Category: python traceback | Python traceback points to {file}:{line}{suffix}{detail}."

    pytest_assert = re.search(r"^E\s+(AssertionError:.*|assert\s+.+)$", text, re.MULTILINE)
    if pytest_assert:
        return f"Category: test assertion | Pytest assertion failure: {pytest_assert.group(1).strip()}."

    pytest_failed = re.search(r"FAILED\s+([^\s]+)(?:\s+-\s+([^\n]+))?", text)
    if pytest_failed:
        test_name, reason = pytest_failed.groups()
        suffix = f": {reason.strip()}" if reason else ""
        return f"Category: test assertion | Failed test {test_name}{suffix}."

    msvc_compile = re.search(
        r"([^\n()]+\.(?:c|cc|cpp|cxx|h|hpp|hxx))\((\d+)\):\s*(fatal\s+)?error\s+([A-Z]+\d+)?:?\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if msvc_compile:
        file, line, _, code, message = msvc_compile.groups()
        code_part = f" {code}" if code else ""
        return f"Category: c++ compile error | MSVC compile error{code_part} at {file.strip()}:{line}: {message.strip()}."

    cpp_error = re.search(
        r"((?:[A-Za-z]:)?[^\n:]+\.(?:c|cc|cpp|cxx|h|hpp|hxx)):(\d+)(?::\d+)?:\s*(?:fatal\s+)?error:\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if cpp_error:
        file, line, message = cpp_error.groups()
        return f"Category: c++ compile error | C/C++ compile error at {file.strip()}:{line}: {message.strip()}."

    msvc_include = re.search(r"fatal error C1083: Cannot open include file: ['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
    if msvc_include:
        return f"Category: missing include | Missing include/header file: {msvc_include.group(1).strip()}."

    missing_include = re.search(r"fatal error: ([^:\n]+): No such file or directory", text)
    if missing_include:
        return f"Category: missing include | Missing include/header file: {missing_include.group(1).strip()}."

    msvc_unresolved = re.search(r"LNK2019: unresolved external symbol ([^\n]+)", text, re.IGNORECASE)
    if msvc_unresolved:
        return f"Category: linker unresolved symbol | MSVC unresolved external symbol: {msvc_unresolved.group(1).strip()}."

    unresolved = re.search(r"undefined reference to [`'\"]?([^`'\"\n]+)", text, re.IGNORECASE)
    if unresolved:
        return f"Category: linker unresolved symbol | Linker unresolved symbol: {unresolved.group(1).strip()}."

    multiple_def = re.search(r"multiple definition of [`'\"]?([^`'\"\n]+)", text, re.IGNORECASE)
    if multiple_def:
        return f"Category: linker multiple definition | Multiple definition of symbol: {multiple_def.group(1).strip()}."

    assertion = re.search(r"(?:AssertionError|assert\s+.+|FAILED\s+[^\n]+)", text)
    if assertion:
        return f"Category: test assertion | Test assertion failure: {assertion.group(0).strip()}."

    syntax = re.search(r"SyntaxError: (.+)", text)
    if syntax:
        return f"Category: python syntax error | Python syntax error: {syntax.group(1).strip()}."

    for line in text.splitlines():
        stripped = line.strip()
        if any(k in stripped for k in ("error:", "FAILED", "Exception", "Traceback")):
            return f"Category: unknown error | {stripped[:500]}"

    return f"Category: unknown error | {text[:500]}"


def _numbered(actions: list[str]) -> str:
    return " ".join(f"{i}. {action}" for i, action in enumerate(actions, start=1))


def _build_fix_strategy(
    root_cause: str,
    source_function: str,
    translated_code: str,
    error_text: str,
) -> str:
    """根据根因生成下一步修复建议。"""
    cause = root_cause.lower()
    actions: list[str] = []

    if "python import error" in cause:
        actions.extend([
            "check translated import names against the created target files",
            "use find_target_imports/read_file to inspect imports before editing",
            "update package/module paths or create the missing translated file",
        ])
    elif "python traceback" in cause or "python syntax error" in cause:
        actions.extend([
            "read the failing target file around the reported line",
            "compare the corresponding source logic with translated_code",
            "fix the minimal target function and rerun tests",
        ])
    elif "c++ compile error" in cause:
        actions.extend([
            "inspect the reported C/C++ file and line",
            "compare function signatures, types, includes, and namespace/class scope",
            "fix compile errors before changing runtime logic",
        ])
    elif "linker unresolved symbol" in cause:
        actions.extend([
            "verify the missing function/method is implemented with the exact signature",
            "check CMakeLists/build configuration includes the translated source file",
            "use find_target_method/find_target_class before rewriting code",
        ])
    elif "linker multiple definition" in cause:
        actions.extend([
            "find duplicate function or global variable definitions",
            "move non-inline definitions out of headers or mark small header definitions inline",
            "check build configuration for duplicate source inclusion",
        ])
    elif "missing include" in cause:
        actions.extend([
            "locate the expected header or translated class file",
            "fix #include paths or create the missing header/source pair",
            "avoid editing unrelated logic until includes compile",
        ])
    elif "test assertion" in cause:
        actions.extend([
            "identify the failing test expectation from test_results",
            "compare edge cases and return values against the source implementation",
            "make the smallest behavioral fix, then rerun the same test",
        ])
    else:
        actions.extend([
            "read the failing target file and related source file",
            "inspect imports/includes and public method signatures",
            "fix the first concrete compile/test error before broad rewrites",
        ])

    if source_function.strip() and translated_code.strip():
        actions.append("compare source_function and translated_code directly before rewriting")
    elif not source_function.strip() and translated_code.strip():
        actions.append("read/provide the relevant source function before editing translated_code")
    elif source_function.strip() and not translated_code.strip():
        actions.append("read the current translated code before calling create_file")
    else:
        actions.append("read both the relevant source function and current translated code before editing")
    if not error_text.strip():
        actions.append("rerun tests or compile command to collect exact error output")

    return _numbered(actions)


def _format_reflection(root_cause: str, fix_strategy: str) -> str:
    return (
        "[Reflect Analysis]\n"
        f"{root_cause}\n"
        "Next actions:\n"
        f"{fix_strategy}"
    )


class ReflectTool(ToolDefinition):
    description: str = "分析翻译错误的根因并制定修复策略（不修改任何文件）"

    @classmethod
    def create(cls, conv_state=None, **kwargs):
        return [cls(
            action_type=ReflectAction,
            observation_type=ReflectObservation,
            executor=ReflectExecutor(),
        )]


register_tool("reflect", ReflectTool)
