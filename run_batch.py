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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def parse_args():
    parser = argparse.ArgumentParser(description="OpenTransAgent - 批量仓库翻译")
    parser.add_argument("--source_root", required=True, help="源项目根目录")
    parser.add_argument("--target_root", required=True, help="翻译结果输出根目录")
    parser.add_argument("--source_language", default="cpp")
    parser.add_argument("--target_language", default="python")
    parser.add_argument("--llm_model", default="")
    parser.add_argument("--llm_api_key", default="")
    parser.add_argument("--llm_base_url", default="")
    parser.add_argument("--llm_timeout", type=int, default=120)
    parser.add_argument("--max_iterations", type=int, default=None)
    parser.add_argument("--max_projects", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="跳过已翻译的项目")
    parser.add_argument("--timeout_per_project", type=int, default=0)
    parser.add_argument("--no-topo-sort", action="store_true", help="跳过文件依赖分析")
    return parser.parse_args()


def get_project_list(source_root: str) -> list[str]:
    root = Path(source_root)
    if not root.exists():
        logger.error(f"❌ Source directory not found: {source_root}")
        return []
    return sorted([
        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    ], key=str.lower)


def is_already_translated(project_name: str, target_root: str) -> bool:
    return (Path(target_root) / project_name / "_TRANSLATED").exists()


def mark_translated(project_name: str, target_root: str):
    marker = Path(target_root) / project_name / "_TRANSLATED"
    marker.write_text(datetime.now().isoformat(), encoding="utf-8")


def count_source_files(project_path: str, source_language: str) -> int:
    exts = {".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx"} if source_language == "cpp" else {".py"}
    count = 0
    for f in Path(project_path).rglob("*"):
        if f.is_file() and f.suffix.lower() in exts:
            count += 1
            if count >= 500:
                break
    return count


def dynamic_timeout(file_count: int) -> int:
    if file_count < 10:
        return 900
    elif file_count < 30:
        return 1800
    elif file_count < 100:
        return 3600
    else:
        return min(7200, file_count * 60)


def run_single_project(project_name, source_path, target_path, **kwargs) -> dict:
    start_time = time.time()
    result: dict = {"project": project_name, "status": "unknown", "duration": 0, "error": ""}
    log_file = str(Path(target_path) / "run.log")

    cmd = [
        sys.executable, "run.py",
        "--project_name", project_name,
        "--source_language", kwargs["source_language"],
        "--target_language", kwargs["target_language"],
        "--source_path", source_path,
        "--target_path", target_path,
        "--max_iterations", str(kwargs["max_iterations"]),
        "--llm_timeout", str(kwargs["llm_timeout"]),
        "--persistence_dir", str(Path(target_path) / ".persist"),
    ]
    if kwargs.get("llm_model"):
        cmd.extend(["--llm_model", kwargs["llm_model"]])
    if kwargs.get("llm_api_key"):
        cmd.extend(["--llm_api_key", kwargs["llm_api_key"]])
    if kwargs.get("llm_base_url"):
        cmd.extend(["--llm_base_url", kwargs["llm_base_url"]])
    if kwargs.get("no_topo_sort"):
        cmd.append("--no-topo-sort")

    try:
        env = os.environ.copy()
        env["OPENHANDS_SUPPRESS_BANNER"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        for _p in (r"C:\Program Files\CMake\bin",
                   r"C:\Users\30146\AppData\Local\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.MCF.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe\mingw64\bin"):
            if os.path.isdir(_p) and _p not in env.get("PATH", ""):
                env["PATH"] = _p + os.pathsep + env.get("PATH", "")

        with open(log_file, "w", encoding="utf-8") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                  timeout=kwargs["project_timeout"], env=env,
                                  cwd=os.path.dirname(os.path.abspath(__file__)))
        result["duration"] = round(time.time() - start_time, 1)

        if proc.returncode == 0:
            result["status"] = "success"
        elif proc.returncode == 2:
            result["status"] = "stuck"
        else:
            result["status"] = "failed"
            try:
                log_text = Path(log_file).read_text(encoding="utf-8")
                result["error"] = "\n".join(
                    [l for l in log_text.split("\n") if "ERROR" in l or "error" in l.lower()][-3:]
                )[:500] or f"Exit code: {proc.returncode}"
            except Exception:
                result["error"] = f"Exit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["duration"] = kwargs["project_timeout"]
        result["error"] = "超时"
    except Exception as e:
        result["status"] = "error"
        result["duration"] = round(time.time() - start_time, 1)
        result["error"] = str(e)
    return result


def main():
    args = parse_args()
    projects = get_project_list(args.source_root)
    logger.info(f"📦 Found {len(projects)} projects")

    if args.max_projects > 0:
        projects = projects[:args.max_projects]
        logger.info(f"🎯 Limiting to first {args.max_projects} projects")

    api_key = args.llm_api_key or os.environ.get("LLM_API_KEY", "")
    max_iterations = args.max_iterations or int(os.environ.get("MAX_ITERATIONS", 120))

    results = []
    summary = {"success": 0, "stuck": 0, "failed": 0, "timeout": 0, "error": 0, "skipped": 0}
    total_start = time.time()

    logger.info(f"🤖 Model: {args.llm_model or '(from .env)'}  |  Max iterations: {max_iterations}")
    logger.info("-" * 50)

    for i, project in enumerate(projects):
        logger.info(f"\n[{i+1}/{len(projects)}] 📁 {project}")

        target_dir = str(Path(args.target_root) / project)
        source_dir = str(Path(args.source_root) / project / "source_project")

        if args.resume and is_already_translated(project, args.target_root):
            logger.info(f"    ⏭️  Already translated, skipping")
            summary["skipped"] += 1
            continue

        file_count = count_source_files(source_dir, args.source_language)
        timeout = args.timeout_per_project or dynamic_timeout(file_count)
        logger.info(f"    🔄 {args.source_language} -> {args.target_language}  |  {file_count} files, {timeout}s timeout")
        Path(target_dir).mkdir(parents=True, exist_ok=True)

        result = run_single_project(
            project_name=project, source_path=source_dir, target_path=target_dir,
            source_language=args.source_language, target_language=args.target_language,
            llm_model=args.llm_model, llm_api_key=api_key, llm_base_url=args.llm_base_url,
            llm_timeout=args.llm_timeout, max_iterations=max_iterations,
            project_timeout=timeout, no_topo_sort=args.no_topo_sort,
        )

        if result["status"] in ("success", "stuck"):
            mark_translated(project, args.target_root)

        status_icon = {"success": "✅", "stuck": "⚠️", "failed": "❌", "timeout": "⏱️", "error": "💥"}
        logger.info(f"  {status_icon.get(result['status'], '❓')} {result['status'].upper()} ({result['duration']:.1f}s)")
        if result["error"]:
            logger.info(f"    Error: {result['error'][:300]}")

        results.append(result)
        summary[result["status"]] = summary.get(result["status"], 0) + 1

    elapsed = round(time.time() - total_start, 1)
    report = {"total": len(projects), "summary": summary, "elapsed": f"{elapsed}s",
              "results": results, "timestamp": datetime.now().isoformat()}

    report_path = Path(args.target_root) / "batch_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("\n" + "=" * 50)
    logger.info("BATCH TRANSLATION COMPLETE")
    logger.info(f"  Total: {len(projects)}  |  Elapsed: {elapsed:.0f}s")
    for s, label in [("success", "✅ Success"), ("stuck", "⚠️ Stuck"), ("failed", "❌ Failed"),
                     ("timeout", "⏱️ Timeout"), ("error", "💥 Error"), ("skipped", "⏭️ Skipped")]:
        if summary.get(s, 0) > 0:
            logger.info(f"  {label}: {summary[s]}")
    logger.info(f"  📊 Report: {report_path}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
