from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils import logger as logger_module
from utils.logger import (
    _SUPPRESS_FILTER_NAME,
    _safe_path_part,
    TranslationTraceLogger,
    configure_logger,
    save_prompt_to,
    setup_log_dir,
    suppress_sdk_logging,
)


def test_safe_path_part_replaces_invalid_chars() -> None:
    assert _safe_path_part('claude/model:v1') == "claude_model_v1"
    assert _safe_path_part(' bad project ') == "bad_project"
    assert _safe_path_part('', "fallback") == "fallback"


def test_configure_logger_is_idempotent() -> None:
    original_handlers = list(logger_module.logger.handlers)
    try:
        configure_logger(force=True)
        first_count = len(logger_module.logger.handlers)
        configure_logger()
        second_count = len(logger_module.logger.handlers)

        assert first_count == 1
        assert second_count == first_count
    finally:
        logger_module.logger.handlers.clear()
        logger_module.logger.handlers.extend(original_handlers)


def test_suppress_sdk_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = [f for f in root.filters if getattr(f, "opentrans_filter_name", None) == _SUPPRESS_FILTER_NAME]
    for f in before:
        root.removeFilter(f)
    try:
        suppress_sdk_logging()
        suppress_sdk_logging()
        filters = [f for f in root.filters if getattr(f, "opentrans_filter_name", None) == _SUPPRESS_FILTER_NAME]

        assert len(filters) == 1
        assert logging.getLogger("openhands").level == logging.WARNING
    finally:
        for f in list(root.filters):
            if getattr(f, "opentrans_filter_name", None) == _SUPPRESS_FILTER_NAME:
                root.removeFilter(f)
        for f in before:
            root.addFilter(f)


def test_setup_log_dir_sanitizes_parts_and_avoids_collision(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    first = setup_log_dir("model/name:v1", "bad project/name", "c++", "py")
    second = setup_log_dir("model/name:v1", "bad project/name", "c++", "py")

    assert first.exists()
    assert second.exists()
    assert first != second
    assert first.parts[1] == "model_name_v1"
    assert "bad_project_name" in first.name


def test_save_prompt_to_creates_directory(tmp_path: Path) -> None:
    log_dir = tmp_path / "missing"
    path = save_prompt_to(log_dir, "prompt text")

    assert path == log_dir / "system_prompt.txt"
    assert path.read_text(encoding="utf-8") == "prompt text"


# ── TranslationTraceLogger ──────────────────────────────────────

def test_trace_logger_writes_jsonl(tmp_path: Path) -> None:
    t = TranslationTraceLogger(tmp_path, run_id="test1")
    t.write("llm_request", payload={"key": "val"})
    t.close()

    assert t.path.exists()
    lines = t.path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "llm_request"
    assert record["run_id"] == "test1"
    assert record["payload"]["key"] == "val"


def test_trace_logger_redacts_secrets(tmp_path: Path) -> None:
    t = TranslationTraceLogger(tmp_path, run_id="t", redact_secrets=True)
    t.write("test", payload={
        "api_key": "sk-secret",
        "nested": {"authorization": "Bearer xyz"},
        "list": [{"token": "bad"}],
        "text_with_key": "use sk-abc123XYZ0000 here",
    })
    t.close()

    record = json.loads(t.path.read_text(encoding="utf-8").strip())
    p = record["payload"]
    assert p["api_key"] == "[REDACTED]"
    assert p["nested"]["authorization"] == "[REDACTED]"
    assert p["list"][0]["token"] == "[REDACTED]"
    assert "sk-" not in p["text_with_key"]


def test_trace_logger_truncates_large_fields(tmp_path: Path) -> None:
    t = TranslationTraceLogger(tmp_path, run_id="t", max_field_chars=10)
    t.write("test", payload={"long": "x" * 20})
    t.close()

    record = json.loads(t.path.read_text(encoding="utf-8").strip())
    p = record["payload"]
    assert isinstance(p["long"], dict)
    assert p["long"]["truncated"] is True
    assert p["long"]["original_chars"] == 20


def test_trace_logger_thread_safe_writes(tmp_path: Path) -> None:
    t = TranslationTraceLogger(tmp_path, run_id="ts")

    def writer(i: int) -> None:
        for _ in range(100):
            t.write("test", payload={"thread": i})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(writer, range(4)))
    t.close()

    lines = t.path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 400


def test_trace_logger_writes_live_markdown_during_run(tmp_path: Path) -> None:
    t = TranslationTraceLogger(
        tmp_path,
        run_id="live",
        project_name="demo",
        model="model-x",
        source_language="cpp",
        target_language="python",
    )
    t.set_context(layer_idx=1, round_idx=2)
    t.write("action_event", tool_name="read_file", action_data={"filepath": "a.cpp"})
    t.write("completeness_check", payload={
        "passed": False,
        "expected_count": 2,
        "present_count": 1,
        "missing_count": 1,
        "missing": [{"source": "b.cpp", "expected": "b.py"}],
    })

    assert t.live_path.exists()
    live = t.live_path.read_text(encoding="utf-8")
    assert "# 实时翻译进度" in live
    assert "当前 Layer：1" in live
    assert "当前 Round：2" in live
    assert "最近完整性检查" in live
    assert "`b.cpp` → `b.py`" in live
    assert "`action_event`" in live
    assert "调用工具：`read_file`" in live
    assert "完整性检查失败：1/2 已生成，缺失 1" in live
    t.close()


def test_trace_logger_context_sets_layer_and_round(tmp_path: Path) -> None:
    t = TranslationTraceLogger(tmp_path, run_id="ctx")
    t.set_context(layer_idx=2, round_idx=5)
    t.write("event1")
    t.set_context(round_idx=7)
    t.write("event2")
    t.close()

    records = [json.loads(line) for line in t.path.read_text(encoding="utf-8").strip().split("\n")]
    assert records[0]["layer_idx"] == 2
    assert records[0]["round_idx"] == 5
    assert records[1]["round_idx"] == 7
    assert records[1]["layer_idx"] == 2  # preserved from before


def test_trace_logger_generates_chinese_summary_and_index(tmp_path: Path) -> None:
    t = TranslationTraceLogger(
        tmp_path,
        run_id="summary",
        project_name="demo",
        model="model-x",
        source_language="python",
        target_language="cpp",
    )
    t.write("run_start")
    t.set_context(layer_idx=0, round_idx=1, step=1)
    t.write("layer_start", payload={"file_count": 2})
    t.write("round_start")
    t.write("llm_request", payload={"message_count": 2, "tools": [{"name": "read_file"}]})
    t.write("llm_response", payload={"response_type": "tool_calls", "tool_calls": [{"name": "read_file"}]})
    t.write("action_event", tool_name="create_file", action_data={"filepath": "main.cpp"})
    t.write("action_event", tool_name="create_file", action_data={"filepath": "main.cpp"})
    t.write("action_event", tool_name="read_file", action_data={"filepath": "source.py"})
    t.write("idle_nudge", payload={"reason": "no_files_created", "new_file_count": 0})
    t.write("completeness_check", payload={
        "layer": 0,
        "attempt": 1,
        "retry_limit": 3,
        "passed": False,
        "expected_count": 2,
        "present_count": 1,
        "missing_count": 1,
        "missing": [{"source": "missing.py", "expected": "missing.cpp", "layer": 0}],
    })
    t.write("completeness_feedback_sent", payload={"layer": 0, "attempt": 1, "missing_count": 1})
    t.write("completeness_check", payload={
        "layer": 0,
        "attempt": 2,
        "retry_limit": 3,
        "passed": True,
        "expected_count": 2,
        "present_count": 2,
        "missing_count": 0,
        "missing": [],
    })
    t.write("test_analysis_start", payload={
        "test_scope": "cumulative_regression",
        "new_test_files": [],
        "visible_test_files": ["tests/test_main.py"],
        "test_command": "python -m pytest tests/test_main.py -v",
    })
    t.write("test_analysis_result", payload={
        "passed_tests": 1,
        "total_tests": 1,
        "compilation_success": True,
    })
    t.write("round_end", payload={"elapsed_s": 65})
    t.write("run_end", payload={"elapsed_s": 70, "all_passed": True})
    t.close()

    assert t.summary_path.exists()
    assert t.index_path.exists()
    summary = t.summary_path.read_text(encoding="utf-8")
    index = json.loads(t.index_path.read_text(encoding="utf-8"))

    assert "# 翻译运行分析报告" in summary
    assert "## 2. 分层执行结果" in summary
    assert "## 6. 翻译完整性检查" in summary
    assert "完整性补齐反馈" in summary
    assert "累计回归测试" in summary
    assert "本层没有新增测试文件" in summary
    assert "`create_file`" in summary
    assert "`main.cpp`（2 次）" in summary
    assert "重复写入文件" in summary
    assert "本次翻译成功完成" in summary
    assert index["event_counts"]["idle_nudge"] == 1
    assert index["summary_file"] == "translation_trace_summary.md"
