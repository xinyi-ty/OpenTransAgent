"""OpenTransAgent 启动入口。"""

import argparse
import concurrent.futures
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("PYTHONUTF8", "1")  # 强制 Python 子进程使用 UTF-8，避免 gbk 编码错误

from utils.logger import logger, suppress_sdk_logging, setup_log_dir, save_prompt_to
suppress_sdk_logging()

from config.sdk_path import ensure_openhands_importable
ensure_openhands_importable()

from config.settings import get_llm_config, get_max_iterations
from openhands.sdk import LLM, Conversation, ConversationExecutionStatus
from agent.translation_agent import ReActTranslationAgent
from workspace.manager import (prepare_source_workspace, get_project_tree,
                               extract_results, cleanup, get_topo_sort_order,
                               compute_layers, LayerController,
                               copy_workspace_files, copy_source_files,
                               assign_tests_to_layers, copy_test_layer)
from workspace.precheck import run_precheck
from analysis.test_analyzer import TestAnalysis, TestAnalyzer
from config.languages import get_target_extensions
from config.router import validate_pair

# 每次外循环允许 agent 执行的最大 step 数。
# 设太小 LLM 刚展开就暂停，设太大外循环失去意义。
# 修改此值会影响 max_iterations 的实际步数上限（max_iter × STEPS_PER_ROUND）。
STEPS_PER_ROUND = 50


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
    p.add_argument("--llm_timeout", type=int, default=0)
    p.add_argument("--max_iterations", type=int, default=0,
                   help="最大外循环次数（每次外循环内 agent 可执行约 STEPS_PER_ROUND 步）")
    p.add_argument("--workspace_dir", default="./workspace")
    p.add_argument("--persistence_dir", default="")
    p.add_argument("--no-topo-sort", action="store_true",
                   help="跳过文件依赖分析（拓扑排序）")
    return p.parse_args()


def format_feedback(analysis: TestAnalysis | None) -> str:
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
        if analysis.raw_output and analysis.passed_tests == 0 and analysis.total_tests > 0:
            lines.append(f"\nTest output:\n{analysis.raw_output}")

    return "\n".join(lines)


def main():
    args = parse_args()
    if not validate_pair(args.source_language, args.target_language):
        logger.error(f"❌ Unsupported translation pair: {args.source_language} -> {args.target_language}")
        return 1
    model, api_key, base_url, timeout = get_llm_config(args)
    max_iter = get_max_iterations(args)
    persistence = args.persistence_dir or None
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
                f"  |  Steps per round: {STEPS_PER_ROUND}")
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
            logger.info(f"📋 Initialized workspace with {len(layers[0])} source "
                        f"file(s) + infrastructure")
        else:
            logger.info(f"📋 Topo sort skipped")

    # 刷新文件树（Layer 0 文件加入后）
    tree = get_project_tree(source_ws)

    # ── Agent 初始化 ──────────────────────────────────────────
    agent = ReActTranslationAgent.create(
        llm=llm, workspace_root=source_ws, max_iterations=max_iter * STEPS_PER_ROUND,
        project_name=project_name, source_language=args.source_language,
        target_language=args.target_language, project_tree=tree,
        translation_order=translation_order, layer_ctrl=layer_ctrl)

    # ── 保存 System Prompt 到日志文件 ─────────────────────────
    log_dir = setup_log_dir(model, project_name,
                            args.source_language, args.target_language)
    prompt_path = save_prompt_to(log_dir, agent.system_prompt)
    logger.info(f"💾 System prompt saved to: {prompt_path}")

    # visualizer=None: 屏蔽 SDK 的 Rich 可视化输出
    conv = Conversation(
        agent=agent, workspace=source_ws,
        max_iteration_per_run=STEPS_PER_ROUND,
        persistence_dir=persistence, stuck_detection=True,
        stuck_detection_thresholds={"action_observation": 5, "action_error": 3,
                                     "monologue": 15, "alternating_pattern": 4},
        visualizer=None)

    # ── 外循环：按依赖层推进 ───────────────────────────────────
    total_start = datetime.now()
    layer_count = layer_ctrl.total_layers if layer_ctrl and layer_ctrl.active else 1

    conv.send_message(f"Translate this {args.source_language} project "
                      f"to {args.target_language}.")

    final_analysis = None
    all_passed = False
    no_tests = False
    exit_reason = None  # "passed" | "stuck" | "no_tests" | None
    layer_errors: list[str] = []  # 每层的测试报错信息，用于最后汇总

    for layer_idx in range(layer_count):
        prev_file_count = 0  # 记录上一轮的文件数，判断 LLM 是否在产出
        if layer_idx > 0:
            # 解锁下一层：源文件 + 测试文件
            layer_ctrl.advance()
            copy_source_files(layers[layer_idx], staging, source_ws)
            logger.info(f"📋 Copied {len(layers[layer_idx])} source file(s) (Layer {layer_idx})")
            if test_layers and layer_idx < len(test_layers):
                copy_test_layer(test_layers[layer_idx], target_project, source_ws)
                logger.info(f"📋 Copied {len(test_layers[layer_idx])} test file(s) (Layer {layer_idx})")
            conv.state.execution_status = ConversationExecutionStatus.RUNNING
            msg = (f"Layer {layer_idx - 1} passed. "
                   f"Now translating Layer {layer_idx}: "
                   f"{', '.join(layers[layer_idx])}. "
                   f"These files depend on already-translated code.")
            conv.send_message(msg)

        for round_idx in range(1, max_iter + 1):
            logger.info("")
            logger.info(f"=== Layer {layer_idx} — Round {round_idx}/{max_iter} ===")

            # ① SDK 内部跑 STEPS_PER_ROUND 步
            round_start = datetime.now()
            round_timeout = 1800  # 30 分钟单轮安全上限（LLM_TIMEOUT=60 时足够）
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(conv.run)
            try:
                future.result(timeout=round_timeout)
            except concurrent.futures.TimeoutError:
                logger.warning(f"  ⏰ Round timed out after {round_timeout}s")
                conv.state.execution_status = ConversationExecutionStatus.STUCK
                exit_reason = "stuck"
            finally:
                pool.shutdown(wait=False)  # 不等待可能卡住的线程
            round_elapsed = (datetime.now() - round_start).total_seconds()
            logger.info(f"  ⏱️ Round time: {round_elapsed:.0f}s")

            # ② LLM 还在工作中 → 不测试，直接下一轮
            status = conv.state.execution_status
            if status == ConversationExecutionStatus.STUCK:
                logger.info(f"  ⚠️ Agent stuck")
                exit_reason = "stuck"
                break
            if status != ConversationExecutionStatus.FINISHED:
                # 还没 finish → 检查是否有产出，避免空转
                target_exts = get_target_extensions(args.target_language)
                new_count = sum(
                    len(list(Path(source_ws).rglob(f"*{ext}")))
                    for ext in target_exts
                )
                if new_count > 0 and new_count <= prev_file_count and round_idx > 1:
                    msg = ("Files have been created. Call finish now to run tests and verify your translation. "
                           "If tests fail, the results will show you what to fix.")
                elif new_count <= 0 and round_idx > 1:
                    msg = ("You have not created any files. Read a source file and immediately write its "
                           "translation with create_file. Do not spend steps exploring.")
                else:
                    msg = "Continue working on the current layer. Call finish when all files are translated."
                prev_file_count = new_count
                if round_idx < max_iter and exit_reason is None:
                    conv.send_message(msg)
                continue

            # ③ 检查当前层文件是否全部创建（每层只提醒一次）
            if layers and round_idx == 1:
                expected = set()
                for lyr in layers[:layer_idx + 1]:
                    for sf in lyr:
                        expected.add(Path(sf).stem)
                actual = set()
                for ext in get_target_extensions(args.target_language):
                    for p in Path(source_ws).rglob(f"*{ext}"):
                        actual.add(p.stem)
                missing = expected - actual
                if missing:
                    logger.info(f"  ⏩ {len(missing)} file(s) not translated (will be skipped)")
                    conv.send_message(
                        f"Translate these files before calling finish: "
                        f"{', '.join(sorted(missing)[:5])}. "
                        f"If you believe they are unnecessary, explain why."
                    )
                    conv.state.execution_status = ConversationExecutionStatus.RUNNING
                    conv.run()  # 再给一轮机会
                    continue

            # ④ LLM 调用了 finish → 跑测试验证这一层
            logger.info(f"  ✅ LLM finished layer. Running tests...")
            logger.info(f"  🧪 Analyzing test results...")
            try:
                # 按层构建测试命令（支持 C++ 二进制 + Python pytest）
                test_cmd = None
                if test_layers and layer_idx < len(test_layers):
                    py_files = []
                    exe_files = []
                    for tf in test_layers[layer_idx]:
                        if tf.endswith(".py"):
                            py_files.append(tf)
                        else:
                            exe_files.append(tf)

                    all_subcmds = []
                    for tf in py_files:
                        all_subcmds.append(f"python -m pytest {tf} -v 2>&1")
                    for tf in exe_files:
                        stem = Path(tf).stem
                        paths = [f"build\\{stem}.exe",
                                 f"build\\Debug\\{stem}.exe",
                                 f"build\\Release\\{stem}.exe"]
                        all_subcmds.append(" || ".join(
                            f'if exist "{p}" "{p}"' for p in paths
                        ))

                    if all_subcmds:
                        test_cmd = " & ".join(all_subcmds)
                analyzer = TestAnalyzer(working_dir=source_ws, timeout=300,
                                        test_command=test_cmd or None)
                analysis = analyzer.run_and_analyze()
                final_analysis = analysis

                if analysis:
                    logger.info(f"  🔧 Compilation: "
                                f"{'SUCCESS' if analysis.compilation.success else 'FAILED'}")
                    layer_label = f"Layer {layer_idx}" if layer_count > 1 else "All"
                    logger.info(f"  📊 {layer_label} tests: "
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

            # ④ 测试没全过 → 重置状态让 agent 继续修
            if final_analysis and final_analysis.total_tests == 0:
                no_tests = True
                exit_reason = "no_tests"
                logger.info(f"  ✅ Agent finished (no tests to verify)")
                break
            conv.state.execution_status = ConversationExecutionStatus.RUNNING

            # ⑤ 构造反馈发给 LLM
            if round_idx < max_iter:
                feedback = format_feedback(final_analysis)
                conv.send_message(feedback)
                logger.info(f"  📨 Feedback sent to agent ({len(feedback)} chars)")

        if exit_reason:
            break

    total_elapsed = (datetime.now() - total_start).total_seconds()

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
        logger.info(f"  📊 Tests: {final_analysis.passed_tests}/{final_analysis.total_tests} "
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
    files = extract_results(source_ws, target, args.target_language)
    cleanup(source_ws)

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
