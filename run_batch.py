"""
批量翻译入口。
遍历数据集中的所有项目，逐一执行翻译，生成汇总报告。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from utils.logger import logger

# 加载 .env 配置（供后续 os.environ.get 读取）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def parse_args():
    parser = argparse.ArgumentParser(description="OpenTransAgent - 批量仓库翻译")

    parser.add_argument("--source_root", required=True, help="源项目根目录（包含多个子项目）")
    parser.add_argument("--target_root", required=True, help="翻译结果输出根目录")
    parser.add_argument("--source_language", default="cpp", help="源语言")
    parser.add_argument("--target_language", default="python", help="目标语言")

    parser.add_argument("--llm_model", default="", help="LLM 模型（默认从 .env 读取）")
    parser.add_argument("--llm_api_key", default="", help="API 密钥（默认从 .env 读取）")
    parser.add_argument("--llm_base_url", default="", help="API 地址（默认从 .env 读取）")
    parser.add_argument("--llm_timeout", type=int, default=120, help="LLM 超时")

    parser.add_argument("--max_iterations", type=int, default=None,
                        help="每个项目的最大迭代次数（默认从 .env 读取 MAX_ITERATIONS，取不到则 120）")
    parser.add_argument("--max_projects", type=int, default=0, help="最大处理项目数（0=全部）")
    parser.add_argument("--resume", action="store_true", help="跳过已翻译的项目")
    parser.add_argument("--timeout_per_project", type=int, default=600, help="单个项目超时秒数")
    parser.add_argument("--no-topo-sort", action="store_true",
                        help="跳过文件依赖分析（拓扑排序）")

    return parser.parse_args()


def get_project_list(source_root: str) -> list[str]:
    """获取所有子项目目录列表"""
    root = Path(source_root)
    if not root.exists():
        logger.error(f"❌ Source directory not found: {source_root}")
        return []
    return sorted([
        p.name for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ])


def is_already_translated(project_name: str, target_root: str) -> bool:
    """检查项目是否已翻译完成（存在标记文件）"""
    marker = Path(target_root) / project_name / "_TRANSLATED"
    return marker.exists()


def mark_translated(project_name: str, target_root: str, status: str):
    """标记项目翻译状态"""
    marker = Path(target_root) / project_name / f"_{status}"
    marker.write_text(datetime.now().isoformat(), encoding="utf-8")


def run_single_project(
    project_name: str,
    source_path: str,
    target_path: str,
    source_language: str,
    target_language: str,
    model: str,
    api_key: str,
    base_url: str,
    llm_timeout: int,
    max_iterations: int,
    project_timeout: int,
    no_topo_sort: bool = False,
) -> dict:
    """调用 run.py 翻译单个项目"""
    start_time = time.time()
    result: dict = {
        "project": project_name,
        "status": "unknown",
        "duration": 0,
        "error": "",
    }

    persistence_dir = str(Path(target_path) / ".persist")
    cmd = [
        sys.executable, "run.py",
        "--project_name", project_name,
        "--source_language", source_language,
        "--target_language", target_language,
        "--source_path", source_path,
        "--target_path", target_path,
        "--max_iterations", str(max_iterations),
        "--llm_timeout", str(llm_timeout),
        "--persistence_dir", persistence_dir,
    ]
    # 只传用户显式指定的参数，未指定的让 run.py 从 .env 读取
    if model:
        cmd.extend(["--llm_model", model])
    if api_key:
        cmd.extend(["--llm_api_key", api_key])
    if base_url:
        cmd.extend(["--llm_base_url", base_url])
    if no_topo_sort:
        cmd.append("--no-topo-sort")

    try:
        env = os.environ.copy()
        env["OPENHANDS_SUPPRESS_BANNER"] = "1"
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=project_timeout,
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        result["duration"] = time.time() - start_time

        # 退出码: 0=成功 1=未通过 2=卡死
        if proc.returncode == 0:
            result["status"] = "success"
        elif proc.returncode == 2:
            result["status"] = "stuck"
            result["error"] = "Agent stuck"
        else:
            result["status"] = "failed"
            result["error"] = (proc.stderr or proc.stdout)[:500]
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["duration"] = project_timeout
        result["error"] = "项目翻译超时"
    except Exception as e:
        result["status"] = "error"
        result["duration"] = time.time() - start_time
        result["error"] = str(e)

    result["duration"] = round(result["duration"], 1)
    return result


def main():
    args = parse_args()

    projects = get_project_list(args.source_root)
    logger.info(f"📦 Found {len(projects)} projects")

    if args.max_projects > 0:
        projects = projects[:args.max_projects]
        logger.info(f"🎯 Limiting to first {args.max_projects} projects")

    # 从 .env 或环境变量读取配置（命令行参数优先）
    api_key = args.llm_api_key or os.environ.get("LLM_API_KEY", "")
    max_iterations = args.max_iterations or int(os.environ.get("MAX_ITERATIONS", 120))

    results = []
    summary: dict[str, int] = {"success": 0, "stuck": 0, "failed": 0, "timeout": 0, "error": 0, "skipped": 0}

    logger.info(f"🤖 Model: {args.llm_model}  |  Max iterations: {max_iterations}  |  Timeout: {args.timeout_per_project}s")
    logger.info("-" * 50)

    for i, project in enumerate(projects):
        logger.info(f"\n[{i+1}/{len(projects)}] 📁 {project}")
        logger.info(f"    🔄 {args.source_language} -> {args.target_language}")

        target_dir = str(Path(args.target_root) / project)
        source_dir = str(Path(args.source_root) / project)

        if args.resume and is_already_translated(project, args.target_root):
            logger.info(f"    ⏭️  Already translated, skipping")
            summary["skipped"] += 1
            continue

        # 确保目标目录存在
        Path(target_dir).mkdir(parents=True, exist_ok=True)

        # 执行翻译
        result = run_single_project(
            project_name=project,
            source_path=source_dir,
            target_path=target_dir,
            source_language=args.source_language,
            target_language=args.target_language,
            model=args.llm_model,
            api_key=api_key,
            base_url=args.llm_base_url,
            llm_timeout=args.llm_timeout,
            max_iterations=max_iterations,
            project_timeout=args.timeout_per_project,
            no_topo_sort=args.no_topo_sort,
        )

        results.append(result)
        summary[result["status"]] = summary.get(result["status"], 0) + 1

        # 标记翻译状态
        if result["status"] in ("success", "stuck"):
            mark_translated(project, args.target_root, "TRANSLATED")

        status_icon = {"success": "✅", "stuck": "⚠️", "failed": "❌", "timeout": "⏱️", "error": "💥"}
        logger.info(f"    {status_icon.get(result['status'], '❓')} {result['status'].upper()} ({result['duration']:.1f}s)")
        if result["error"]:
            logger.info(f"    Error: {result['error'][:200]}")

    # 生成汇总报告
    report = {
        "total": len(projects),
        "summary": summary,
        "results": results,
        "timestamp": datetime.now().isoformat(),
        "config": {
            "model": args.llm_model,
            "max_iterations": args.max_iterations,
        },
    }

    report_path = Path(args.target_root) / "batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 打印总结
    logger.info("\n" + "=" * 50)
    logger.info("BATCH TRANSLATION COMPLETE")
    logger.info(f"  Total:  {len(projects)}")
    status_labels = {"success": "✅ Success", "stuck": "⚠️ Stuck", "failed": "❌ Failed",
                     "timeout": "⏱️ Timeout", "error": "💥 Error", "skipped": "⏭️ Skipped"}
    for s, c in summary.items():
        if c > 0:
            logger.info(f"  {status_labels.get(s, s)}: {c}")
    logger.info(f"  📊 Report: {report_path}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
