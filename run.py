"""OpenTransAgent 启动入口。"""

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("PYTHONUTF8", "1")  # 强制 Python 子进程使用 UTF-8，避免 gbk 编码错误

from utils.logger import logger, suppress_sdk_logging, setup_log_dir, save_prompt_to, TranslationTraceLogger
suppress_sdk_logging()

from config.settings import (
    get_completeness_retry_limit,
    get_invalid_response_limit,
    get_llm_config,
    get_max_iterations,
    get_reflection_enabled,
    get_round_timeout,
    get_runtime_error_limit,
    get_search_max_results,
    get_steps_per_round,
    get_test_raw_output_limit,
    get_test_timeout,
    get_tool_command_timeout,
    get_trace_log_enabled,
    get_trace_log_max_field_chars,
    get_trace_log_redact_secrets,
)
from openhands.sdk import LLM, Conversation, ConversationExecutionStatus
from openhands.sdk.conversation.exceptions import ConversationRunError
from agent.translation_agent import ReActTranslationAgent
from workspace.manager import (prepare_source_workspace, get_project_tree,
                               extract_results, cleanup, get_topo_sort_order,
                               compute_layers, LayerController,
                               copy_workspace_files, copy_source_files,
                               assign_tests_to_layers, copy_test_layer)
from workspace.precheck import run_precheck, _cpp_test_target_name
from analysis.test_analyzer import TestAnalysis, TestAnalyzer
from config.languages import (
    get_target_extensions,
    get_test_extensions,
    get_test_unit_label,
    should_refresh_precheck_after_test_copy,
)
from config.router import get_effective_route, validate_pair

def parse_args():
    p = argparse.ArgumentParser(description="OpenTransAgent - 仓库级代码翻译")
    p.add_argument("--project_name", default="",
                   help="项目名称（默认从 --source_path 的上级目录名自动提取）")
    p.add_argument("--source_language", required=True)
    p.add_argument("--target_language", required=True)
    p.add_argument("--source_path", required=True)
    p.add_argument("--target_path", default="")
    p.add_argument("--target_project_path", default="",
                   help="预构建的目标测试目录（默认 source_path 同级 target_project）")
    p.add_argument("--llm_model", default="")
    p.add_argument("--llm_api_key", default="")
    p.add_argument("--llm_base_url", default="")
    p.add_argument("--llm_timeout", type=int, default=None)
    p.add_argument("--max_iterations", type=int, default=None,
                   help="最大外循环次数（每次外循环内 agent 可执行 steps_per_round 步）")
    p.add_argument("--steps_per_round", type=int, default=None,
                   help="每次外循环内 Agent 可执行的最大 step 数（默认 50）")
    p.add_argument("--tool_command_timeout", type=int, default=None,
                   help="execute_command 工具默认超时时间秒数（默认 60）")
    p.add_argument("--search_max_results", type=int, default=None,
                   help="search_content 工具默认最大结果数（默认 10）")
    p.add_argument("--round_timeout", type=int, default=None,
                   help="单轮 Conversation.run 超时时间秒数（默认 1800）")
    p.add_argument("--test_timeout", type=int, default=None,
                   help="测试分析超时时间秒数（默认 300）")
    p.add_argument("--test_raw_output_limit", type=int, default=None,
                   help="反馈给 LLM 的测试原始输出上限（默认 5000，0 表示不截断）")
    p.add_argument("--no-reflection", action="store_true",
                   help="禁用失败后的 reflect 反思纠错提示和工具")
    p.add_argument("--invalid_response_limit", type=int, default=None,
                   help="连续无效响应上限（默认 3）")
    p.add_argument("--runtime_error_limit", type=int, default=None,
                   help="连续可恢复运行时错误上限（默认 3）")
    p.add_argument("--completeness_retry_limit", type=int, default=None,
                   help="翻译完整性检查失败后的连续补齐重试上限（默认 3）")
    p.add_argument("--no_trace_log", action="store_true",
                   help="禁用功能调用追踪日志")
    p.add_argument("--trace_log_max_field_chars", type=int, default=None,
                   help="追踪日志字段最大字符数（默认 20000）")
    p.add_argument("--no_trace_redact", action="store_true",
                   help="禁用追踪日志敏感字段 redaction")
    p.add_argument("--workspace_dir", default="./workspace")
    p.add_argument("--persistence_dir", default="")
    p.add_argument("--no-topo-sort", action="store_true",
                   help="跳过文件依赖分析（拓扑排序）")
    return p.parse_args()


def format_feedback(
    analysis: TestAnalysis | None,
    reflection_enabled: bool = True,
) -> str:
    """将测试分析结果格式化为给 LLM 的反馈消息。"""
    if analysis is None:
        return ("[SYSTEM: ITERATION COMPLETE]\n"
                "Could not detect or run tests. If you believe the translation is "
                "complete, describe what you have done and call finish.")

    lines = ["[SYSTEM: ITERATION TEST RESULTS]"]
    if analysis.compilation.success:
        lines.append(f"Compilation: SUCCESS")
    else:
        lines.append(f"Compilation: FAILED")
        if analysis.compilation.errors:
            lines.append(f"  Errors: {analysis.compilation.errors[:500]}")

    lines.append(f"Tests: {analysis.passed_tests}/{analysis.total_tests} "
                 f"passed ({analysis.overall_pass_rate:.1f}%)")
    if not analysis.compilation.success and analysis.compilation.errors:
        lines.append(f"\nCompilation output:\n{analysis.compilation.errors}")

    if analysis.modules:
        for name, mod in analysis.modules.items():
            status = "PASS" if mod.is_module_passed else "FAIL"
            lines.append(f"  [{status}] {name}: {mod.passed_tests}/{mod.total_tests}")

    if analysis.total_tests > 0 and analysis.passed_tests == analysis.total_tests:
        lines.append("\nAll tests pass. Call finish to complete the task.")
    else:
        lines.append("\nSome tests are still failing. Continue fixing them. "
                     "Do NOT call finish until all tests pass.")
        if not analysis.compilation.success:
            lines.append("Fix compilation errors first, then logic errors.")
        if reflection_enabled:
            lines.append(
                "Before editing files, call reflect(source_function, translated_code, "
                "error_message, test_results) to analyze the failure."
            )
        if analysis.raw_output and analysis.passed_tests < analysis.total_tests:
            lines.append(f"\nTest output:\n{analysis.raw_output}")

    return "\n".join(lines)


def _quote_cmd_arg(value: str) -> str:
    """为 shell=True 命令安全引用路径参数。"""
    return subprocess.list2cmdline([value])


def _test_result_unit_label(source_language: str, target_language: str) -> str:
    """返回测试统计单位，避免 CTest target 被误读为单个 test case。"""
    _ = source_language
    return get_test_unit_label(target_language)


def _format_required_targets_for_layer(
    layer_files: list[str] | None,
    source_language: str,
    target_language: str,
) -> str:
    """生成层切换消息中的必需目标文件清单。"""
    if not layer_files:
        return ""
    targets = [
        _expected_target_for_source(src, source_language, target_language)
        for src in layer_files
    ]
    lines = [
        "",
        "Required target files for this layer:",
        *(f"- {target}" for target in targets),
        "",
        "Create all required target files before running build/tests.",
    ]
    return "\n".join(lines)


def _adaptive_steps_per_round(
    base_steps: int,
    layer_files: list[str] | None,
    layer_tests: list[str] | None,
) -> int:
    """按当前层复杂度调整单轮 step 上限，避免大层在一个 round 内无限膨胀上下文。"""
    file_count = len(layer_files or [])
    test_count = len(layer_tests or [])
    if file_count >= 10:
        return min(base_steps, 30)
    if file_count >= 6:
        return min(base_steps, 40)
    if file_count >= 4 or test_count >= 4:
        return max(base_steps, 50)
    if file_count >= 2 and test_count >= 3:
        return max(base_steps, 40)
    return base_steps


def _route_guidance_strength(source_language: str, target_language: str) -> str:
    """提取当前语言对的 route 策略强度，供运行时提示使用。"""
    route = get_effective_route(source_language, target_language)
    if route and route.prompt_route_guidance:
        return "api_contract_first"
    return ""


def _format_large_layer_guidance(
    layer_files: list[str] | None,
    route_strength: str = "",
) -> str:
    """生成大层批处理提示，减少一次性读写/测试导致的长耗时。
    可选 route_strength 用于追加语言对专属策略提示。
    """
    if len(layer_files or []) <= 5:
        return ""
    lines = [
        "",
        "Large layer batching rules:",
        "- Do not read every source file before writing; process at most 4 source files per batch.",
        "- Do not call more than 5 read_file/create_file/edit_file tools in one response.",
        "- Read at most 3 representative test files before generating target files; read more only after a concrete failure points to them.",
        "- Create missing target files before running broad tests.",
        "- Use one focused check while developing; run full regression only after all required target files exist.",
        "- execute_command already runs in the workspace; do not cd into guessed external project paths.",
        "- If a lower-layer file needs changes, use edit_file only; never full-rewrite it.",
    ]
    if route_strength == "api_contract_first":
        lines.extend([
            "",
            "This project has many visible C++ test files that define the expected API contract:",
            "- Before generating target files, read at most 3 representative visible tests to understand required class/function signatures, namespaces, and header includes.",
            "- Match the test-expected API exactly; do not invent different signatures or class names.",
            "- After creating a batch of target files, run a focused build (`cmake --build build --config Release --target translated_lib`) to catch API mismatches early.",
            "- Adapt translated C++ source/header files to the contract; never modify tests or CMakeLists.txt.",
        ])
    return "\n".join(lines) + "\n"


def _collect_visible_test_files(
    test_layers: list[list[str]] | None,
    layer_idx: int,
) -> list[str]:
    """收集当前 workspace 已解锁/已复制的累计测试文件。"""
    if not test_layers:
        return []
    visible: list[str] = []
    for layer_tests in test_layers[:layer_idx + 1]:
        visible.extend(layer_tests)
    return list(dict.fromkeys(visible))


def _build_layer_test_command(test_files: list[str], working_dir: str) -> str | None:
    """按累计可见测试文件构造测试命令。"""
    _ = working_dir
    py_subcmds: list[str] = []
    cpp_targets: list[str] = []
    python_test_exts = set(get_test_extensions("python"))
    cpp_test_exts = set(get_test_extensions("cpp"))

    for tf in test_files:
        suffix = Path(tf).suffix.lower()
        if suffix in python_test_exts:
            py_subcmds.append(f"python -m pytest {_quote_cmd_arg(tf)} -v")
        elif suffix in cpp_test_exts:
            cpp_targets.append(_cpp_test_target_name(tf))

    all_subcmds = py_subcmds
    if cpp_targets:
        pattern = "^(" + "|".join(re.escape(t) for t in dict.fromkeys(cpp_targets)) + ")$"
        all_subcmds.append(
            "ctest --test-dir build --output-on-failure -C Release "
            f'-R "{pattern}"'
        )

    return " && ".join(all_subcmds) if all_subcmds else None


@dataclass(frozen=True)
class MissingTranslation:
    source: str
    expected: str
    layer: int


@dataclass(frozen=True)
class CompletenessResult:
    expected_count: int
    present_count: int
    missing: list[MissingTranslation]

    @property
    def passed(self) -> bool:
        return not self.missing


def _expected_target_for_source(
    source_file: str,
    source_language: str,
    target_language: str,
) -> str:
    """用语言路由生成期望目标路径；无路由时退回源路径。"""
    route = get_effective_route(source_language, target_language)
    if route and route.file_extension_map:
        return route.file_extension_map(source_file)
    return source_file


def _expected_translations_for_layers(
    layers: list[list[str]] | None,
    translation_order: list[str] | None,
    layer_idx: int,
    source_language: str,
    target_language: str,
) -> list[MissingTranslation]:
    """生成当前累计层应存在的目标文件清单（用 MissingTranslation 复用字段结构）。"""
    expected: list[MissingTranslation] = []
    seen: set[str] = set()
    if layers:
        source_with_layers = [
            (src, idx)
            for idx, layer in enumerate(layers[:layer_idx + 1])
            for src in layer
        ]
    else:
        source_with_layers = [(src, 0) for src in (translation_order or [])]
    for src, src_layer in source_with_layers:
        target = _expected_target_for_source(src, source_language, target_language)
        if target in seen:
            continue
        seen.add(target)
        expected.append(MissingTranslation(source=src, expected=target, layer=src_layer))
    return expected


def _check_translation_completeness(
    workspace_path: str,
    layers: list[list[str]] | None,
    translation_order: list[str] | None,
    layer_idx: int,
    source_language: str,
    target_language: str,
) -> CompletenessResult:
    """检查当前累计层的期望目标文件是否全部存在。"""
    expected = _expected_translations_for_layers(
        layers, translation_order, layer_idx, source_language, target_language,
    )
    ws = Path(workspace_path)
    missing = [item for item in expected if not (ws / item.expected).is_file()]
    return CompletenessResult(
        expected_count=len(expected),
        present_count=len(expected) - len(missing),
        missing=missing,
    )


def _format_completeness_feedback(
    result: CompletenessResult,
    attempt: int,
    retry_limit: int,
) -> str:
    """把缺失文件清单格式化为强约束补齐消息，避免模型空转。"""
    lines = [
        "[SYSTEM: TRANSLATION COMPLETENESS CHECK FAILED]",
        f"Attempt {attempt}/{retry_limit}.",
        f"Expected translated files: {result.expected_count}; present: {result.present_count}; missing: {len(result.missing)}.",
        "",
        "Missing translations:",
    ]
    for i, item in enumerate(result.missing[:20], 1):
        lines.append(f"{i}. Source: {item.source} -> Expected target: {item.expected} (Layer {item.layer})")
    if len(result.missing) > 20:
        lines.append(f"... and {len(result.missing) - 20} more")
    lines.extend([
        "",
        "Rules:",
        "- Work only on the missing target files listed above.",
        "- For each item, read the corresponding source file, then create_file the expected target path.",
        "- If a target file exists but is incomplete, use edit_file for precise fixes.",
        "- Do NOT run tests while files are missing.",
        "- Do NOT explain that files are unnecessary unless you create a small stub target file that preserves compatibility.",
        "- Do NOT call finish until all expected target files exist.",
    ])
    if attempt >= max(2, retry_limit - 1):
        lines.insert(1, "SYSTEM: COMPLETENESS RECOVERY MODE — previous attempts did not finish all required files.")
    return "\n".join(lines)


def _completeness_payload(
    result: CompletenessResult,
    *,
    layer_idx: int,
    attempt: int,
    retry_limit: int,
    passed: bool | None = None,
) -> dict[str, Any]:
    return {
        "layer": layer_idx,
        "attempt": attempt,
        "retry_limit": retry_limit,
        "passed": result.passed if passed is None else passed,
        "expected_count": result.expected_count,
        "present_count": result.present_count,
        "missing_count": len(result.missing),
        "missing": [item.__dict__ for item in result.missing[:50]],
    }


def _is_retryable_runtime_error(exc: BaseException) -> bool:
    """粗略判断 provider/网络错误是否适合在下一轮恢复重试。"""
    text = str(exc).lower()
    retryable = (
        "rate limit", "ratelimit", "429", "overloaded", "timeout",
        "timed out", "connection", "network", "temporar", "server error",
        " 500", " 502", " 503", " 504",
    )
    fatal = (
        "authentication", "unauthorized", "forbidden", "invalid api key",
        "permission", "bad request", "invalid_request", "model not found",
        "not found", "unsupported",
    )
    if any(marker in text for marker in fatal):
        return False
    return any(marker in text for marker in retryable)


def _runtime_error_message(exc: BaseException) -> str:
    if isinstance(exc, ConversationRunError):
        return str(exc.original_exception)
    return str(exc)


def _request_conversation_stop(conv: Any) -> None:
    """请求 SDK 停止当前 run；兼容没有 interrupt 的 Conversation 实现。"""
    try:
        interrupt = getattr(conv, "interrupt", None)
        if callable(interrupt):
            interrupt()
            return
        pause = getattr(conv, "pause", None)
        if callable(pause):
            pause()
            return
    except Exception as exc:
        logger.warning(f"  ⚠️  Failed to interrupt conversation: {str(exc)[:300]}")
    try:
        conv.state.execution_status = ConversationExecutionStatus.STUCK
    except Exception:
        pass


def _run_conversation_with_timeout(
    conv: Any,
    timeout: float,
    stop_wait_timeout: float = 10,
) -> tuple[bool, bool]:
    """运行一次 Conversation.run，返回 (是否完成, 超时后线程是否仍未停止)。"""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(conv.run)
    try:
        future.result(timeout=timeout)
        return True, False
    except concurrent.futures.TimeoutError:
        logger.warning(f"  ⏰ Round timed out after {timeout}s")
        _request_conversation_stop(conv)
        future.cancel()
        try:
            future.result(timeout=stop_wait_timeout)
            return False, False
        except concurrent.futures.TimeoutError:
            logger.warning("  ⚠️  Conversation thread did not stop after interrupt; preserving workspace")
            return False, True
        except Exception:
            return False, False
    finally:
        pool.shutdown(wait=False)


def _safe_close_conversation(conv: Any | None) -> None:
    """释放 SDK Conversation 资源，避免退出时遗留 file store / observability 资源。"""
    if conv is None:
        return
    close = getattr(conv, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        logger.warning(f"  ⚠️  Failed to close conversation: {str(exc)[:300]}")


def main():
    args = parse_args()
    if not validate_pair(args.source_language, args.target_language):
        logger.error(f"❌ Unsupported translation pair: {args.source_language} -> {args.target_language}")
        return 1
    model, api_key, base_url, timeout = get_llm_config(args)
    max_iter = get_max_iterations(args)
    steps_per_round = get_steps_per_round(args)
    tool_command_timeout = get_tool_command_timeout(args)
    search_max_results = get_search_max_results(args)
    round_timeout = get_round_timeout(args)
    test_timeout = get_test_timeout(args)
    test_raw_output_limit = get_test_raw_output_limit(args)
    reflection_enabled = get_reflection_enabled(args)
    invalid_response_limit = get_invalid_response_limit(args)
    runtime_error_limit = get_runtime_error_limit(args)
    completeness_retry_limit = get_completeness_retry_limit(args)
    persistence = args.persistence_dir or None
    trace_log_enabled = get_trace_log_enabled(args)
    trace_log_max_field_chars = get_trace_log_max_field_chars(args)
    trace_log_redact = get_trace_log_redact_secrets(args)
    source_dir = Path(args.source_path)
    # 自动提取 project_name（未指定时取 source_path 的上级目录名）
    project_name = args.project_name or source_dir.parent.name
    target = str(Path(args.target_path or args.workspace_dir) / project_name)

    llm = LLM(model=model, api_key=api_key, timeout=timeout,
              **(dict(base_url=base_url) if base_url else {}))

    # ── 启动信息 ──────────────────────────────────────────────
    logger.info(f"🤖 Model: {model}")
    logger.info(f"📁 Project: {project_name}")
    logger.info(f"🔄 Translation: {args.source_language} -> {args.target_language}")
    logger.info(f"📂 Output: {target}  |  Max outer iterations: {max_iter}"
                f"  |  Steps per round: {steps_per_round}"
                f"  |  Total step budget: {max_iter * steps_per_round}")
    logger.info(f"⚙️  Tool timeout: {tool_command_timeout}s"
                f"  |  Search max results: {search_max_results}"
                f"  |  Round timeout: {round_timeout}s"
                f"  |  Test timeout: {test_timeout}s"
                f"  |  Raw output limit: {test_raw_output_limit}"
                f"  |  Completeness retries: {completeness_retry_limit}"
                f"  |  Reflection: {'on' if reflection_enabled else 'off'}")
    logger.info("-" * 50)

    # ── 自动检测 target_project_path ──────────────────────────
    target_project = args.target_project_path
    if not target_project:
        guess = source_dir.parent / "target_project"
        if guess.is_dir():
            target_project = str(guess)
            logger.info(f"📋 Auto-detected target: {target_project}")

    # ── 工作区准备 ────────────────────────────────────────────
    target_project = target_project or None
    staging, source_ws = prepare_source_workspace(
        target, args.source_path, target_project, args.target_language)
    for line in run_precheck(source_ws, args.target_language, project_name):
        logger.info(f"  {line}")

    # ── 依赖分析（拓扑排序）────────────────────────────────────
    translation_order = None
    layer_ctrl = None
    layers: list[list[str]] | None = None
    test_layers: list[list[str]] | None = None
    if not args.no_topo_sort:
        logger.info(f"📋 Analyzing dependency order...")
        topo_result = get_topo_sort_order(args.source_path, args.source_language)
        if topo_result and topo_result.get("translation_order"):
            translation_order = topo_result["translation_order"]
            layers = compute_layers(translation_order, topo_result.get("dependencies", []))
            layer_ctrl = LayerController(layers)
            logger.info(f"📋 Suggested order: {len(translation_order)} files, "
                        f"{len(topo_result.get('dependencies', []))} dependencies"
                        + (f", {len(layers)} layers, "
                           f"starting with {len(layers[0])} files"
                           if topo_result.get("cycles") else f", {len(layers)} layers"))

            # 将测试文件分配到对应层（支持 C++/Python 两种测试文件）
            if target_project:
                test_layers = assign_tests_to_layers(target_project, layers)
                logger.info(f"📋 Test files assigned to layers: "
                            f"{[len(tl) for tl in test_layers]}")

            # 初始化工作区：Layer 0 源文件 + 所有非源码文件
            all_source = set(translation_order)
            copy_workspace_files(layers[0], all_source, staging, source_ws)
            # 复制 Layer 0 的测试文件（仅 Python → C++）
            if test_layers:
                copy_test_layer(test_layers[0], target_project, source_ws)
                # 测试文件到位后，重新生成 CMakeLists.txt 使其包含 test executable 目标。
                # Python 源项目可能自带 Makefile；它不应阻止 C++/CTest 脚手架更新。
                if test_layers[0] and should_refresh_precheck_after_test_copy(args.target_language):
                    cmake_path = Path(source_ws) / "CMakeLists.txt"
                    if cmake_path.exists():
                        cmake_path.unlink()
                    for line in run_precheck(source_ws, args.target_language, project_name):
                        logger.info(f"  {line}")
            logger.info(f"📋 Initialized workspace with {len(layers[0])} source "
                        f"file(s) + infrastructure")
        else:
            logger.info(f"📋 Topo sort skipped")

    # 刷新文件树（Layer 0 文件加入后）
    tree = get_project_tree(source_ws)

    # ── 日志目录与功能调用追踪日志 ─────────────────────────────
    log_dir = setup_log_dir(model, project_name,
                            args.source_language, args.target_language)
    trace_logger = None
    if trace_log_enabled:
        trace_logger = TranslationTraceLogger(
            log_dir,
            run_id=log_dir.name,
            project_name=project_name,
            model=model,
            source_language=args.source_language,
            target_language=args.target_language,
            max_field_chars=trace_log_max_field_chars,
            redact_secrets=trace_log_redact,
        )
        logger.info(f"📝 Translation trace: {trace_logger.path}")

    # ── Agent 初始化 ──────────────────────────────────────────
    initial_source_files = layers[0] if layers else translation_order
    agent = ReActTranslationAgent.create(
        llm=llm, workspace_root=source_ws,
        max_iterations=max_iter * steps_per_round,
        project_name=project_name, source_language=args.source_language,
        target_language=args.target_language, project_tree=tree,
        translation_order=translation_order, layer_ctrl=layer_ctrl,
        command_timeout=tool_command_timeout,
        search_max_results=search_max_results,
        reflection_enabled=reflection_enabled,
        invalid_response_limit=invalid_response_limit,
        source_files=initial_source_files,
        trace_logger=trace_logger)

    # ── 保存 System Prompt 到日志文件 ─────────────────────────
    prompt_path = save_prompt_to(log_dir, agent.system_prompt)
    logger.info(f"💾 System prompt saved to: {prompt_path}")

    # visualizer=None: 屏蔽 SDK 的 Rich 可视化输出
    conv_args: dict[str, Any] = {
        "agent": agent, "workspace": source_ws,
        "max_iteration_per_run": steps_per_round,
        "persistence_dir": persistence, "stuck_detection": True,
        "stuck_detection_thresholds": {"action_observation": 5, "action_error": 3,
                                        "monologue": 15, "alternating_pattern": 4},
        "visualizer": None,
    }
    if trace_logger:
        conv_args["callbacks"] = [trace_logger.on_event]
    conv = Conversation(**conv_args)

    # ── 外循环：按依赖层推进 ───────────────────────────────────
    total_start = datetime.now()
    layer_count = layer_ctrl.total_layers if layer_ctrl and layer_ctrl.active else 1

    conv.send_message(f"Translate this {args.source_language} project "
                      f"to {args.target_language}.")

    if trace_logger:
        trace_logger.write("run_start", payload={
            "source_language": args.source_language,
            "target_language": args.target_language,
            "max_iter": max_iter,
            "steps_per_round": steps_per_round,
            "reflection_enabled": reflection_enabled,
            "completeness_retry_limit": completeness_retry_limit,
        })

    final_analysis = None
    all_passed = False
    no_tests = False
    exit_reason = None  # "passed" | "stuck" | "no_tests" | None
    layer_errors: list[str] = []  # 每层的测试报错信息，用于最后汇总
    consecutive_runtime_errors = 0
    conversation_thread_leaked = False
    final_completeness: CompletenessResult | None = None

    for layer_idx in range(layer_count):
        consecutive_runtime_errors = 0
        completeness_attempts = 0
        last_missing_targets: tuple[str, ...] = ()
        prev_file_count = 0  # 记录上一轮的文件数，判断 LLM 是否在产出
        layer_files = layers[layer_idx] if layers and layer_idx < len(layers) else []
        layer_tests_for_budget = test_layers[layer_idx] if test_layers and layer_idx < len(test_layers) else []
        effective_steps_per_round = _adaptive_steps_per_round(
            steps_per_round,
            layer_files,
            layer_tests_for_budget,
        )
        try:
            conv.max_iteration_per_run = effective_steps_per_round
        except Exception:
            pass
        if effective_steps_per_round != steps_per_round:
            logger.info(
                f"📋 Adaptive step budget for Layer {layer_idx}: "
                f"{effective_steps_per_round} steps/round "
                f"({len(layer_files)} source file(s), {len(layer_tests_for_budget)} new test file(s))"
            )
        if trace_logger:
            trace_logger.set_context(layer_idx=layer_idx)
            trace_logger.write("layer_start", payload={
                "layer": layer_idx,
                "total_layers": layer_count,
                "file_count": len(layer_files),
                "test_file_count": len(layer_tests_for_budget),
                "steps_per_round": effective_steps_per_round,
            })
        if layer_idx > 0:
            # 解锁下一层：源文件 + 测试文件
            layer_ctrl.advance()
            copy_source_files(layers[layer_idx], staging, source_ws)
            logger.info(f"📋 Copied {len(layers[layer_idx])} source file(s) (Layer {layer_idx})")
            if test_layers and layer_idx < len(test_layers):
                copy_test_layer(test_layers[layer_idx], target_project, source_ws)
                logger.info(f"📋 Copied {len(test_layers[layer_idx])} test file(s) (Layer {layer_idx})")
                if test_layers[layer_idx] and should_refresh_precheck_after_test_copy(args.target_language):
                    cmake_path = Path(source_ws) / "CMakeLists.txt"
                    if cmake_path.exists():
                        cmake_path.unlink()
                    for line in run_precheck(source_ws, args.target_language, project_name):
                        logger.info(f"  {line}")
            conv.state.execution_status = ConversationExecutionStatus.RUNNING
            msg = (f"Layer {layer_idx - 1} passed. "
                   f"Now translating Layer {layer_idx}: "
                   f"{', '.join(layers[layer_idx])}. "
                   f"These files depend on already-translated code."
                   f"{_format_required_targets_for_layer(layers[layer_idx], args.source_language, args.target_language)}"
                   f"{_format_large_layer_guidance(layers[layer_idx], _route_guidance_strength(args.source_language, args.target_language))}")
            conv.send_message(msg)

        for round_idx in range(1, max_iter + 1):
            logger.info("")
            logger.info(f"=== Layer {layer_idx} — Round {round_idx}/{max_iter} ===")

            if trace_logger:
                trace_logger.set_context(round_idx=round_idx)
                trace_logger.write("round_start", payload={
                    "layer": layer_idx, "round": round_idx, "max_rounds": max_iter,
                })

            # ① SDK 内部跑 steps_per_round 步
            round_start = datetime.now()
            try:
                run_completed, thread_leaked = _run_conversation_with_timeout(conv, round_timeout)
                if thread_leaked:
                    conversation_thread_leaked = True
                    exit_reason = "stuck"
                    break
                if not run_completed:
                    conv.state.execution_status = ConversationExecutionStatus.STUCK
                    exit_reason = "stuck"
                else:
                    consecutive_runtime_errors = 0
            except ConversationRunError as e:
                err = _runtime_error_message(e)
                consecutive_runtime_errors += 1
                logger.warning(f"  ⚠️  Conversation runtime error: {err[:300]}")
                if trace_logger:
                    trace_logger.write("conversation_error", payload={
                        "error": err[:2000],
                        "consecutive_errors": consecutive_runtime_errors,
                        "retryable": _is_retryable_runtime_error(e.original_exception),
                    })
                if (_is_retryable_runtime_error(e.original_exception)
                        and consecutive_runtime_errors < runtime_error_limit):
                    conv.state.execution_status = ConversationExecutionStatus.RUNNING
                    conv.send_message(
                        "The previous LLM/provider call failed due to a transient "
                        "runtime error. Continue from the last successful action."
                    )
                    continue
                exit_reason = "stuck" if _is_retryable_runtime_error(e.original_exception) else "error"
                break
            except Exception as e:
                err = _runtime_error_message(e)
                consecutive_runtime_errors += 1
                logger.warning(f"  ⚠️  Runtime error: {err[:300]}")
                if _is_retryable_runtime_error(e) and consecutive_runtime_errors < runtime_error_limit:
                    conv.state.execution_status = ConversationExecutionStatus.RUNNING
                    conv.send_message(
                        "The previous runtime step failed due to a transient error. "
                        "Continue from the last successful action."
                    )
                    continue
                exit_reason = "stuck" if _is_retryable_runtime_error(e) else "error"
                break
            round_elapsed = (datetime.now() - round_start).total_seconds()
            logger.info(f"  ⏱️ Round time: {round_elapsed:.0f}s")

            if trace_logger:
                trace_logger.write("round_end", payload={
                    "elapsed_s": round_elapsed,
                    "exit_reason": exit_reason,
                    "status": str(conv.state.execution_status) if conv.state else None,
                })

            # ② LLM 还在工作中 → 不测试，直接下一轮
            status = conv.state.execution_status
            if status == ConversationExecutionStatus.STUCK:
                logger.info(f"  ⚠️ Agent stuck")
                exit_reason = "stuck"
                break
            if status == ConversationExecutionStatus.ERROR:
                # SDK 层内部 ERROR 常见原因是本轮 max_iteration_per_run 用尽。
                # 这不是致命错误；下一轮继续推进，并用更准确的日志避免误导。
                logger.info(
                    f"  ⏭️ Round step budget exhausted "
                    f"({effective_steps_per_round} step(s)); continuing in next round"
                )
                budget_completeness = _check_translation_completeness(
                    source_ws,
                    layers,
                    translation_order,
                    layer_idx,
                    args.source_language,
                    args.target_language,
                )
                conv.state.execution_status = ConversationExecutionStatus.RUNNING
                if budget_completeness.missing:
                    missing_lines = "\n".join(
                        f"- {item.source} -> {item.expected}"
                        for item in budget_completeness.missing[:10]
                    )
                    conv.send_message(
                        "Round step budget was exhausted before this layer was complete. "
                        "Continue in small batches and create the missing required target files before running build/tests:\n"
                        f"{missing_lines}"
                        f"{_format_large_layer_guidance(layer_files, _route_guidance_strength(args.source_language, args.target_language))}"
                    )
                    if trace_logger:
                        trace_logger.write("step_budget_completeness_check", payload={
                            "layer": layer_idx,
                            "missing_count": len(budget_completeness.missing),
                            "missing": [item.__dict__ for item in budget_completeness.missing[:20]],
                        })
                else:
                    conv.send_message(
                        "Round step budget was exhausted, but all required target files appear to exist. "
                        "Do NOT continue broad self-testing or exploratory edits. Your next response should either "
                        "call finish so the runtime can run completeness and cumulative regression, or make one "
                        "minimal source edit only if the immediately previous build/test output identified a concrete error."
                        f"{_format_large_layer_guidance(layer_files, _route_guidance_strength(args.source_language, args.target_language))}"
                    )
                continue
            if status == ConversationExecutionStatus.PAUSED:
                # SDK 内部暂停（例如被 interrupt 后未完全恢复），
                # 尝试在下一轮继续，不直接终止。
                logger.info(f"  ⚠️ Agent paused (will retry in next round)")
                conv.state.execution_status = ConversationExecutionStatus.RUNNING
                conv.send_message(
                    "Continue translating. Do NOT call finish until all files are done."
                )
                continue
            if status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION:
                # 当前配置使用 NeverConfirm，不应出现此状态；
                # 若出现，尝试在下一轮恢复。
                logger.info(f"  ⚠️ Agent is waiting for confirmation (will retry in next round)")
                conv.state.execution_status = ConversationExecutionStatus.RUNNING
                conv.send_message(
                    "Continue translating. Do NOT call finish until all files are done."
                )
                continue
            if status != ConversationExecutionStatus.FINISHED:
                # 还没 finish → 检查是否有产出，避免空转
                target_exts = get_target_extensions(args.target_language)
                new_count = sum(
                    len(list(Path(source_ws).rglob(f"*{ext}")))
                    for ext in target_exts
                )
                idle_reason = None
                if new_count > 0 and new_count <= prev_file_count and round_idx > 1:
                    idle_reason = "no_new_files"
                    msg = ("Files have been created. Call finish now to run tests and verify your translation. "
                           "If tests fail, the results will show you what to fix.")
                elif new_count <= 0 and round_idx > 1:
                    idle_reason = "no_files_created"
                    msg = ("You have not created any files. Read a source file and immediately write its "
                           "translation with create_file. Do not spend steps exploring.")
                else:
                    msg = "Continue working on the current layer. Call finish when all files are translated."
                    msg += _format_large_layer_guidance(layer_files, _route_guidance_strength(args.source_language, args.target_language))
                if trace_logger and idle_reason:
                    trace_logger.write("idle_nudge", payload={
                        "reason": idle_reason,
                        "new_file_count": new_count,
                        "prev_file_count": prev_file_count,
                        "message": msg[:500],
                    })
                prev_file_count = new_count
                if round_idx < max_iter and exit_reason is None:
                    conv.send_message(msg)
                continue

            # ③ LLM 调用了 finish → 先做翻译完整性检查，防止跳文件或缺失被测试掩盖
            completeness = _check_translation_completeness(
                source_ws,
                layers,
                translation_order,
                layer_idx,
                args.source_language,
                args.target_language,
            )
            final_completeness = completeness
            missing_targets = tuple(item.expected for item in completeness.missing)
            if missing_targets:
                if missing_targets == last_missing_targets:
                    completeness_attempts += 1
                else:
                    completeness_attempts = 1
                    last_missing_targets = missing_targets
                logger.info(
                    f"  ⚠️ Completeness check failed: "
                    f"{completeness.present_count}/{completeness.expected_count} expected files present; "
                    f"missing {len(completeness.missing)}"
                )
                for item in completeness.missing[:5]:
                    logger.info(f"    missing: {item.source} -> {item.expected}")
                if trace_logger:
                    trace_logger.write("completeness_check", payload=_completeness_payload(
                        completeness,
                        layer_idx=layer_idx,
                        attempt=completeness_attempts,
                        retry_limit=completeness_retry_limit,
                    ))
                if completeness_attempts >= completeness_retry_limit:
                    logger.warning(
                        f"  ❌ Completeness check failed after {completeness_attempts} attempt(s); "
                        "stopping to avoid infinite completion loop"
                    )
                    exit_reason = "incomplete"
                    break
                conv.send_message(_format_completeness_feedback(
                    completeness,
                    completeness_attempts,
                    completeness_retry_limit,
                ))
                if trace_logger:
                    trace_logger.write("completeness_feedback_sent", payload={
                        "layer": layer_idx,
                        "attempt": completeness_attempts,
                        "missing_count": len(completeness.missing),
                    })
                conv.state.execution_status = ConversationExecutionStatus.RUNNING
                continue
            completeness_attempts = 0
            last_missing_targets = ()
            logger.info(
                f"  🧾 Completeness check passed: "
                f"{completeness.present_count}/{completeness.expected_count} expected files present"
            )
            if trace_logger:
                trace_logger.write("completeness_check", payload=_completeness_payload(
                    completeness,
                    layer_idx=layer_idx,
                    attempt=0,
                    retry_limit=completeness_retry_limit,
                ))

            # ④ 完整性通过 → 跑当前 workspace 的累计回归测试
            logger.info(f"  ✅ LLM finished layer. Running tests...")
            logger.info(f"  🧪 Analyzing test results...")
            # 每层执行当前 workspace 的累计回归测试；测试文件仍按层逐步复制。
            new_layer_tests = test_layers[layer_idx] if test_layers and layer_idx < len(test_layers) else []
            visible_tests = _collect_visible_test_files(test_layers, layer_idx)
            test_cmd = _build_layer_test_command(visible_tests, source_ws)
            test_scope = "cumulative_regression" if visible_tests else "auto_detected_regression"
            if new_layer_tests:
                logger.info(
                    f"  🧪 Running cumulative regression tests with "
                    f"{len(new_layer_tests)} newly assigned test file(s) "
                    f"({len(visible_tests)} visible total)"
                )
            elif layers:
                logger.info(
                    "  🧪 No newly assigned tests for this layer; "
                    "running cumulative regression tests"
                )
            if trace_logger:
                trace_logger.write("test_analysis_start", payload={
                    "layer": layer_idx,
                    "test_scope": test_scope,
                    "new_test_files": new_layer_tests,
                    "visible_test_files": visible_tests,
                    "test_command": test_cmd if test_cmd else "auto-detected",
                })
            try:
                analyzer = TestAnalyzer(
                    working_dir=source_ws,
                    timeout=test_timeout,
                    test_command=test_cmd or None,
                    raw_output_limit=test_raw_output_limit,
                )
                analysis = analyzer.run_and_analyze()
                final_analysis = analysis

                if analysis:
                    logger.info(f"  🔧 Compilation: "
                                f"{'SUCCESS' if analysis.compilation.success else 'FAILED'}")
                    layer_label = f"Layer {layer_idx} regression" if layer_count > 1 else "All"
                    unit_label = _test_result_unit_label(args.source_language, args.target_language)
                    logger.info(f"  📊 {layer_label} {unit_label}: "
                                f"{analysis.passed_tests}/{analysis.total_tests} "
                                f"({analysis.overall_pass_rate:.1f}%)")
                    if analysis.modules:
                        for name, mod in analysis.modules.items():
                            icon = "✅" if mod.is_module_passed else "❌"
                            logger.info(f"    {icon} {name}: "
                                        f"{mod.passed_tests}/{mod.total_tests}")

                    # 收集测试失败信息，留到最后汇总
                    # 收集测试失败信息（中间层过滤模块不存在错误，最后一层全量展示）
                    if analysis.total_tests > 0 and analysis.passed_tests < analysis.total_tests:
                        if analysis.raw_output:
                            is_last = (layer_idx == layer_count - 1)
                            err_lines = []
                            for l in analysis.raw_output.split("\n"):
                                keywords = ["FAILED", "fatal", "compilation",
                                            "error:", "ModuleNotFound", "ImportError"]
                                if any(k in l for k in keywords):
                                    if is_last:
                                        err_lines.append(l)
                                    elif "ModuleNotFound" not in l and "ImportError" not in l:
                                        err_lines.append(l)
                            if err_lines:
                                layer_errors.extend(err_lines[:3])

                    if trace_logger:
                        trace_logger.write("test_analysis_result", payload={
                            "compilation_success": analysis.compilation.success,
                            "passed_tests": analysis.passed_tests,
                            "total_tests": analysis.total_tests,
                            "overall_pass_rate": analysis.overall_pass_rate,
                            "modules": {n: {"passed": m.passed_tests, "total": m.total_tests}
                                       for n, m in analysis.modules.items()},
                        })

                    # 判断是否全部通过
                    if analysis.total_tests > 0 and analysis.passed_tests == analysis.total_tests:
                        if layer_idx == layer_count - 1:
                            all_passed = True
                            exit_reason = "passed"
                            logger.info(f"")
                            logger.info(f"🎉 All tests passed!")
                        break
                else:
                    logger.info(f"  ⏭️  No test framework detected")

            except Exception as e:
                logger.info(f"  ⚠️  Test analysis error: {e}")

            if exit_reason:
                break

            # ④ 测试没全过 → 重置状态让 agent 继续修。
            # 注意：编译失败会导致 total_tests=0，但这不是“无测试”，必须反馈给 LLM 修复。
            if final_analysis and final_analysis.total_tests == 0 and final_analysis.compilation.success:
                if layer_idx == layer_count - 1:
                    no_tests = True
                    exit_reason = "no_tests"
                logger.info(f"  ✅ Agent finished (no tests to verify at this layer)")
                break
            conv.state.execution_status = ConversationExecutionStatus.RUNNING

            # ⑤ 构造反馈发给 LLM
            if round_idx < max_iter:
                feedback = format_feedback(final_analysis, reflection_enabled)
                conv.send_message(feedback)
                logger.info(f"  📨 Feedback sent to agent ({len(feedback)} chars)")
                if trace_logger:
                    trace_logger.write("feedback_sent", payload={
                        "length": len(feedback),
                        "reflection_enabled": reflection_enabled,
                        "compilation_success": final_analysis.compilation.success if final_analysis else None,
                        "total_tests": final_analysis.total_tests if final_analysis else None,
                    })

        if exit_reason:
            if trace_logger:
                trace_logger.write("layer_end", payload={
                    "layer": layer_idx, "exit_reason": exit_reason,
                    "all_passed": all_passed,
                })
            break

    total_elapsed = (datetime.now() - total_start).total_seconds()

    if not conversation_thread_leaked and (layers or translation_order):
        final_layer_idx = layer_count - 1
        final_completeness = _check_translation_completeness(
            source_ws,
            layers,
            translation_order,
            final_layer_idx,
            args.source_language,
            args.target_language,
        )
        logger.info(
            f"  🧾 Final completeness check: "
            f"{final_completeness.present_count}/{final_completeness.expected_count} expected files present"
        )
        if trace_logger:
            trace_logger.write("completeness_check", payload={
                **_completeness_payload(
                    final_completeness,
                    layer_idx=final_layer_idx,
                    attempt=0,
                    retry_limit=completeness_retry_limit,
                ),
                "phase": "final",
            })
        if final_completeness.missing:
            all_passed = False
            exit_reason = "incomplete"
            logger.warning("  ❌ Final completeness check failed; generated results are partial")
            for item in final_completeness.missing[:10]:
                logger.warning(f"    missing: {item.source} -> {item.expected}")

    if trace_logger:
        trace_logger.write("run_end", payload={
            "elapsed_s": total_elapsed,
            "exit_reason": exit_reason,
            "all_passed": all_passed,
            "trace_records": trace_logger.written_count,
        })
        trace_logger.close()

    # ── 测试失败原因汇总 ────────────────────────────────────
    if layer_errors and not all_passed:
        logger.info("")
        logger.info("=" * 50)
        logger.info("TEST FAILURES SUMMARY")
        logger.info("=" * 50)
        for err in layer_errors[:8]:
            logger.info(f"  ❌ {err[:200]}")
        logger.info("=" * 50)

    # ── 测试分析总览 ──────────────────────────────────────────
    logger.info("")
    logger.info("=" * 50)
    logger.info("FINAL TEST RESULTS")
    logger.info("=" * 50)
    if final_analysis:
        logger.info(f"  🔧 Compilation: "
                    f"{'SUCCESS' if final_analysis.compilation.success else 'FAILED'}")
        unit_label = _test_result_unit_label(args.source_language, args.target_language)
        logger.info(f"  📊 {unit_label}: {final_analysis.passed_tests}/{final_analysis.total_tests} "
                    f"({final_analysis.overall_pass_rate:.1f}%)")
        if final_analysis.modules:
            for name, mod in final_analysis.modules.items():
                icon = "✅" if mod.is_module_passed else "❌"
                logger.info(f"    {icon} {name}: "
                            f"{mod.passed_tests}/{mod.total_tests}")
    else:
        logger.info("  ⏭️  No test results")
    logger.info("=" * 50)

    # ── 结果提取 ──────────────────────────────────────────────
    files = []
    if conversation_thread_leaked:
        logger.warning(
            "  ⚠️  Skipped result extraction, cleanup, and conversation close "
            "because conversation thread is still running"
        )
    else:
        files = extract_results(source_ws, target, args.target_language)
        cleanup(source_ws)
        _safe_close_conversation(conv)

    logger.info("-" * 50)
    if all_passed:
        logger.info(f"✅ Translation completed in {total_elapsed:.0f}s — "
                    f"{len(files)} file(s) generated")
    elif no_tests:
        logger.info(f"⏹️  Finished in {total_elapsed:.0f}s — "
                    f"{len(files)} file(s) generated (no test suite to verify)")
    else:
        logger.info(f"⏹️  Finished in {total_elapsed:.0f}s — "
                    f"{len(files)} file(s) generated "
                    f"(tests not fully passing)")
    for f in files:
        logger.info(f"   📄 {f}")
    logger.info(f"📁 Logs: {log_dir}")
    logger.info("-" * 50)

    # 退出码: 0=成功 1=未通过 2=卡死 3=异常
    if all_passed:
        return 0
    elif exit_reason == "stuck":
        return 2
    else:
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}")
        sys.exit(3)
