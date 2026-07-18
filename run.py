"""OpenTransAgent 启动入口。"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from utils.logger import logger, suppress_sdk_logging, setup_log_dir, save_prompt_to
suppress_sdk_logging()

from config.sdk_path import ensure_openhands_importable
ensure_openhands_importable()

from config.settings import get_llm_config, get_max_iterations
from openhands.sdk import LLM, Conversation, ConversationExecutionStatus
from agent.translation_agent import ReActTranslationAgent
from workspace.manager import prepare_source_workspace, get_project_tree, extract_results, cleanup, get_topo_sort_order, compute_layers, LayerController
from workspace.precheck import run_precheck
from analysis.test_analyzer import TestAnalyzer

# 每次外循环允许 agent 执行的最大 step 数。
# 设太小 LLM 刚展开就暂停，设太大外循环失去意义。
# 修改此值会影响 max_iterations 的实际步数上限（max_iter × STEPS_PER_ROUND）。
STEPS_PER_ROUND = 30


def parse_args():
    p = argparse.ArgumentParser(description="OpenTransAgent - 仓库级代码翻译")
    p.add_argument("--project_name", required=True)
    p.add_argument("--source_language", required=True)
    p.add_argument("--target_language", required=True)
    p.add_argument("--source_path", required=True)
    p.add_argument("--target_path", default="")
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


def format_feedback(analysis) -> str:
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

    return "\n".join(lines)


def main():
    args = parse_args()
    model, api_key, base_url, timeout = get_llm_config(args)
    max_iter = get_max_iterations(args)
    persistence = args.persistence_dir or None
    target = args.target_path or str(Path(args.workspace_dir) / args.project_name)

    llm = LLM(model=model, api_key=api_key, timeout=timeout,
              **(dict(base_url=base_url) if base_url else {}))

    # ── 启动信息 ──────────────────────────────────────────────
    logger.info(f"🤖 Model: {model}")
    logger.info(f"📁 Project: {args.project_name}")
    logger.info(f"🔄 Translation: {args.source_language} -> {args.target_language}")
    logger.info(f"📂 Output: {target}  |  Max outer iterations: {max_iter}"
                f"  |  Steps per round: {STEPS_PER_ROUND}")
    logger.info("-" * 50)

    # ── 工作区准备 ────────────────────────────────────────────
    source_ws = prepare_source_workspace(target, args.source_path)
    for line in run_precheck(source_ws, args.target_language, args.project_name):
        logger.info(f"  {line}")

    tree = get_project_tree(source_ws)

    # ── 依赖分析（拓扑排序）────────────────────────────────────
    translation_order = None
    layer_ctrl = None
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
        else:
            logger.info(f"📋 Topo sort skipped")

    # ── Agent 初始化 ──────────────────────────────────────────
    agent = ReActTranslationAgent.create(
        llm=llm, workspace_root=source_ws, max_iterations=max_iter * STEPS_PER_ROUND,
        project_name=args.project_name, source_language=args.source_language,
        target_language=args.target_language, project_tree=tree,
        translation_order=translation_order, layer_ctrl=layer_ctrl)

    # ── 保存 System Prompt 到日志文件 ─────────────────────────
    log_dir = setup_log_dir(model, args.project_name,
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

    for layer_idx in range(layer_count):
        if layer_idx > 0:
            # 解锁下一层
            layer_ctrl.advance()
            conv.state.execution_status = ConversationExecutionStatus.RUNNING
            msg = (f"Layer {layer_idx - 1} passed. "
                   f"Now translating Layer {layer_idx}: "
                   f"{', '.join(layer_ctrl.current_files)}. "
                   f"These files depend on already-translated code.")
            conv.send_message(msg)

        for round_idx in range(1, max_iter + 1):
            if layer_count > 1:
                logger.info(f"")
                logger.info(f"=== Layer {layer_idx} — Round {round_idx}/{max_iter} ===")
            else:
                logger.info(f"")
                logger.info(f"=== Iteration {round_idx}/{max_iter} ===")

            # ① SDK 内部跑 STEPS_PER_ROUND 步
            round_start = datetime.now()
            conv.run()
            round_elapsed = (datetime.now() - round_start).total_seconds()
            logger.info(f"  ⏱️ Round time: {round_elapsed:.0f}s")

            # ② 跑测试，分析结果
            logger.info(f"  🧪 Analyzing test results...")
            try:
                analyzer = TestAnalyzer(working_dir=source_ws, timeout=60)
                analysis = analyzer.run_and_analyze()
                final_analysis = analysis

                if analysis:
                    logger.info(f"  🔧 Compilation: "
                                f"{'SUCCESS' if analysis.compilation.success else 'FAILED'}")
                    logger.info(f"  📊 Tests: {analysis.passed_tests}/{analysis.total_tests} "
                                f"({analysis.overall_pass_rate:.1f}%)")
                    if analysis.modules:
                        for name, mod in analysis.modules.items():
                            icon = "✅" if mod.is_module_passed else "❌"
                            logger.info(f"    {icon} {name}: "
                                        f"{mod.passed_tests}/{mod.total_tests}")

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

            # ③ 检查 agent 状态
            status = conv.state.execution_status
            if status == ConversationExecutionStatus.STUCK:
                logger.info(f"  ⚠️ Agent stuck, ending loop")
                exit_reason = "stuck"
                break

            if status == ConversationExecutionStatus.FINISHED:
                if final_analysis is None or final_analysis.total_tests == 0:
                    no_tests = True
                    exit_reason = "no_tests"
                    logger.info(f"  ✅ Agent finished (no tests to verify)")
                    break
                # 有测试但没全过 → 重置状态让 agent 继续修
                conv.state.execution_status = ConversationExecutionStatus.RUNNING

            # ④ 还有下次迭代 → 构造反馈发给 LLM
            if round_idx < max_iter:
                feedback = format_feedback(final_analysis)
                conv.send_message(feedback)
                logger.info(f"  📨 Feedback sent to agent ({len(feedback)} chars)")

        if exit_reason:
            break

    total_elapsed = (datetime.now() - total_start).total_seconds()

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
